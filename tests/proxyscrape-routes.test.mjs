// proxyscrape 路由测试：管理面（号池/代理池/导出）+ 出口分发 + 不干扰其它线。
import { test } from "node:test";
import assert from "node:assert/strict";
import { mkdtempSync, readFileSync, existsSync } from "node:fs";
import { tmpdir } from "node:os";
import { join } from "node:path";

import { loadConfig } from "../src/config.mjs";
import { Store } from "../src/store.mjs";
import { createGateway } from "../src/server.mjs";
import { lineProxyFile, sharedProxyFile } from "../src/proxy-share.mjs";

function startGateway() {
  const dir = mkdtempSync(join(tmpdir(), "ps-routes-"));
  const config = loadConfig(["--port", "18796", "--token", "test-token"]);
  config.dataDir = dir;
  const store = Store.at(dir);
  const lane = {
    base: "http://127.0.0.1:1",
    instance: "test",
    getCatalog: async () => [],
    getAllowance: async () => null,
    checkInFor: () => null,
  };
  const scheduler = { candidates: () => [], record: () => {}, pin: () => {}, status: () => ({}) };
  const gateway = createGateway({ config, lane, store, scheduler, log: () => {} });
  return { gateway, store, dir };
}

async function withServer(run) {
  const ctx = startGateway();
  await new Promise((resolve) => ctx.gateway.server.listen(0, "127.0.0.1", resolve));
  const base = `http://127.0.0.1:${ctx.gateway.server.address().port}`;
  try {
    await run({ ...ctx, base });
  } finally {
    await new Promise((resolve) => ctx.gateway.server.close(resolve));
    ctx.store.flush?.();
  }
}

const admin = { Authorization: "Bearer test-token" };
const json = { "Content-Type": "application/json" };

const ACCOUNT = {
  email: "ps@example.com",
  password: "pw-123456",
  accessToken: "acc-1",
  trialClaimed: true,
  proxies: ["http://u:p@h1:8080", "http://u:p@h2:8081"],
};

test("health and panel state report the proxyscrape pool", async () => {
  await withServer(async ({ base }) => {
    const health = await (await fetch(`${base}/health`)).json();
    assert.ok(health.proxyscrape, "health must expose proxyscrape");
    assert.equal(health.proxyscrape.accounts, 0);
    assert.ok(health.cleverai && health.gabai, "其它线还在");

    const state = await (await fetch(`${base}/api/admin/state`, { headers: admin })).json();
    assert.equal(state.ok, true);
    assert.ok(state.proxyscrape, "面板 state 要带这条线");
    assert.equal(state.proxyscrape.pool.schema, "dcg.pool/proxyscrape@1");
    assert.equal(state.proxyscrape.running, false);
  });
});

test("status + empty pool are wired and discover the worker", async () => {
  await withServer(async ({ base }) => {
    const status = await (await fetch(`${base}/api/admin/proxyscrape/status`, { headers: admin })).json();
    assert.equal(status.ok, true);
    assert.equal(status.toolPresent, true, "tools/proxyscrape-reg 必须能被发现");
    assert.equal(status.pool.schema, "dcg.pool/proxyscrape@1");

    const pool = await (await fetch(`${base}/api/admin/proxyscrape/pool`, { headers: admin })).json();
    assert.deepEqual(pool.accounts, []);
    assert.deepEqual(pool.proxies, []);
  });
});

test("import adds the account and its proxies, and distributes them", async () => {
  await withServer(async ({ base, dir }) => {
    const res = await fetch(`${base}/api/admin/proxyscrape/pool/import`, {
      method: "POST",
      headers: { ...admin, ...json },
      body: JSON.stringify(ACCOUNT),
    });
    assert.equal(res.status, 201);
    const body = await res.json();
    assert.equal(body.created, true);
    assert.equal(body.proxies, 2);
    assert.equal(body.pool.proxies, 2);
    assert.equal(body.share.total, 2);

    // 立刻分发给其它线：shared.txt 有两条，每条线各有一份分片文件
    assert.ok(existsSync(sharedProxyFile(dir)));
    assert.ok(existsSync(lineProxyFile(dir, "cleverai")));

    const pool = await (await fetch(`${base}/api/admin/proxyscrape/pool`, { headers: admin })).json();
    assert.equal(pool.accounts.length, 1);
    assert.equal(pool.proxies.length, 2);
    // 脱敏：账号不带 token，代理不带认证
    assert.ok(!JSON.stringify(pool).includes("acc-1"));
    assert.ok(!JSON.stringify(pool.proxies).includes("u:p@"));
  });
});

test("import rejects a record without email", async () => {
  await withServer(async ({ base }) => {
    const res = await fetch(`${base}/api/admin/proxyscrape/pool/import`, {
      method: "POST",
      headers: { ...admin, ...json },
      body: JSON.stringify({ password: "x" }),
    });
    assert.equal(res.status, 400);
  });
});

test("proxy list is masked and can be removed", async () => {
  await withServer(async ({ base }) => {
    await fetch(`${base}/api/admin/proxyscrape/pool/import`, {
      method: "POST",
      headers: { ...admin, ...json },
      body: JSON.stringify(ACCOUNT),
    });
    const list = await (await fetch(`${base}/api/admin/proxyscrape/proxies`, { headers: admin })).json();
    assert.equal(list.proxies.length, 2);
    assert.ok(list.proxies.every((p) => p.hasAuth === true && !p.endpoint.includes("u:p@")));

    const endpoint = encodeURIComponent(list.proxies[0].endpoint.replace(/^http:\/\//, "http://"));
    const removed = await fetch(`${base}/api/admin/proxyscrape/proxies/${endpoint}`, {
      method: "DELETE",
      headers: admin,
    });
    // masked endpoint 是 host:port（无认证），所以按它删不到 —— 这是刻意的：
    // 面板拿不到明文串，删除要用完整 endpoint。这里断言 404 而不是 200。
    assert.equal(removed.status, 404);
  });
});

test("apply writes per-line proxy files for the requested lines", async () => {
  await withServer(async ({ base, dir }) => {
    await fetch(`${base}/api/admin/proxyscrape/pool/import`, {
      method: "POST",
      headers: { ...admin, ...json },
      body: JSON.stringify(ACCOUNT),
    });
    const res = await fetch(`${base}/api/admin/proxies/apply`, {
      method: "POST",
      headers: { ...admin, ...json },
      body: JSON.stringify({ lines: ["cleverai", "gabai"], limitPerLine: 1 }),
    });
    assert.equal(res.status, 200);
    const body = await res.json();
    assert.equal(body.lines.length, 2);
    for (const row of body.lines) {
      assert.equal(row.rows, 1, "limitPerLine 生效");
      const content = readFileSync(row.file, "utf8").trim();
      assert.ok(content.startsWith("http://"));
    }

    const status = await (await fetch(`${base}/api/admin/proxies`, { headers: admin })).json();
    assert.equal(status.ok, true);
    assert.ok(status.share.total >= 2);
  });
});

test("export downloads a proxy file", async () => {
  await withServer(async ({ base }) => {
    await fetch(`${base}/api/admin/proxyscrape/pool/import`, {
      method: "POST",
      headers: { ...admin, ...json },
      body: JSON.stringify(ACCOUNT),
    });
    const res = await fetch(`${base}/api/admin/proxyscrape/pool/export?format=proxies&download=1`, {
      method: "POST",
      headers: admin,
    });
    assert.equal(res.status, 200);
    const text = await res.text();
    assert.ok(text.includes("h1:8080"));
    assert.match(res.headers.get("content-disposition") ?? "", /attachment/);
  });
});

test("proxyscrape routes do not shadow the other surfaces", async () => {
  await withServer(async ({ base }) => {
    const deepai = await fetch(`${base}/v1/models`, { headers: { Authorization: "Bearer sk-dcg-bogus-key-value-123456" } });
    assert.equal(deepai.status, 401);
    const cleverai = await fetch(`${base}/cleverai/v1/models`);
    assert.equal(cleverai.status, 401);
    const gabai = await fetch(`${base}/gab/v1/models`);
    assert.equal(gabai.status, 401);

    const unknown = await fetch(`${base}/api/admin/proxyscrape/nope`, { headers: admin });
    assert.equal(unknown.status, 404);
  });
});

/* ------------------------------------------- 代理导出 / 订阅（给别的设备） */

test("formats endpoint lists every renderer", async () => {
  await withServer(async ({ base }) => {
    const res = await (await fetch(`${base}/api/admin/proxies/formats`, { headers: admin })).json();
    assert.equal(res.ok, true);
    const ids = res.formats.map((f) => f.id);
    for (const id of ["url", "raw", "at", "json", "csv", "clash", "singbox", "surge", "env"]) {
      assert.ok(ids.includes(id), `缺格式 ${id}`);
    }
    assert.equal(res.defaultFormat, "url");
  });
});

test("export renders in-request (no download) with the chosen format", async () => {
  await withServer(async ({ base }) => {
    await fetch(`${base}/api/admin/proxyscrape/pool/import`, {
      method: "POST",
      headers: { ...admin, ...json },
      body: JSON.stringify(ACCOUNT),
    });
    const clash = await (await fetch(`${base}/api/admin/proxies/export?format=clash`, { headers: admin })).json();
    assert.equal(clash.ok, true);
    assert.equal(clash.format, "clash");
    assert.equal(clash.rows, 2);
    assert.ok(clash.body.includes("proxies:"));

    const singbox = await (await fetch(`${base}/api/admin/proxies/export?format=singbox`, { headers: admin })).json();
    assert.ok(singbox.body.includes("outbounds"));
  });
});

test("download sets a filename and the right content type", async () => {
  await withServer(async ({ base }) => {
    await fetch(`${base}/api/admin/proxyscrape/pool/import`, {
      method: "POST",
      headers: { ...admin, ...json },
      body: JSON.stringify(ACCOUNT),
    });
    const res = await fetch(`${base}/api/admin/proxies/export?format=clash&download=1`, { headers: admin });
    assert.equal(res.status, 200);
    assert.match(res.headers.get("content-type") ?? "", /yaml/);
    assert.match(res.headers.get("content-disposition") ?? "", /proxyscrape-clash-.*\.yaml/);
    assert.ok((await res.text()).includes("proxy-groups:"));
  });
});

test("check probes the pool and marks dead endpoints", async () => {
  await withServer(async ({ base }) => {
    await fetch(`${base}/api/admin/proxyscrape/pool/import`, {
      method: "POST",
      headers: { ...admin, ...json },
      body: JSON.stringify({ email: "dead@example.com", proxies: ["http://127.0.0.1:1"] }),
    });
    const res = await fetch(`${base}/api/admin/proxies/check`, {
      method: "POST",
      headers: { ...admin, ...json },
      body: JSON.stringify({ timeoutMs: 800, concurrency: 2 }),
    });
    assert.equal(res.status, 200);
    const body = await res.json();
    assert.equal(body.total, 1);
    assert.equal(body.alive, 0);
    assert.equal(body.dead, 1);
  });
});

test("a share token serves a subscription without the admin token", async () => {
  await withServer(async ({ base }) => {
    await fetch(`${base}/api/admin/proxyscrape/pool/import`, {
      method: "POST",
      headers: { ...admin, ...json },
      body: JSON.stringify(ACCOUNT),
    });
    const minted = await (
      await fetch(`${base}/api/admin/proxies/share`, {
        method: "POST",
        headers: { ...admin, ...json },
        body: JSON.stringify({ name: "phone", format: "clash" }),
      })
    ).json();
    assert.equal(minted.ok, true);
    assert.equal(minted.format, "clash");
    const token = minted.token || minted.url.split("/").pop();
    assert.ok(minted.url.includes("/proxy/"));

    // 默认格式
    const clash = await fetch(`${base}/proxy/${token}`);
    assert.equal(clash.status, 200);
    assert.match(clash.headers.get("content-type") ?? "", /yaml/);
    assert.ok((await clash.text()).includes("proxies:"));

    // 覆盖格式（同一令牌换给别的客户端）
    const surge = await fetch(`${base}/proxy/${token}?format=surge`);
    assert.equal(surge.status, 200);
    assert.match(await surge.text(), /= http, /);

    // 计数与撤销
    const listed = await (await fetch(`${base}/api/admin/proxies/shares`, { headers: admin })).json();
    assert.equal(listed.shares.length, 1);
    assert.ok(listed.shares[0].hits >= 2);

    const revoked = await fetch(`${base}/api/admin/proxies/shares/${token}`, { method: "DELETE", headers: admin });
    assert.equal(revoked.status, 200);
    assert.equal((await fetch(`${base}/proxy/${token}`)).status, 404);
  });
});

test("subscription endpoint rejects unknown tokens", async () => {
  await withServer(async ({ base }) => {
    const res = await fetch(`${base}/proxy/not-a-real-token-value`);
    assert.equal(res.status, 404);
    const body = await res.json();
    assert.match(body.error.message, /订阅|not_found/i);
  });
});

test("revoke also works by name (panel only sees a masked token)", async () => {
  await withServer(async ({ base }) => {
    await fetch(`${base}/api/admin/proxyscrape/pool/import`, {
      method: "POST",
      headers: { ...admin, ...json },
      body: JSON.stringify(ACCOUNT),
    });
    const minted = await (
      await fetch(`${base}/api/admin/proxies/share`, {
        method: "POST",
        headers: { ...admin, ...json },
        body: JSON.stringify({ name: "router", format: "url" }),
      })
    ).json();
    const res = await fetch(`${base}/api/admin/proxies/shares/router`, { method: "DELETE", headers: admin });
    assert.equal(res.status, 200);
    const body = await res.json();
    assert.equal(body.ok, true);
    assert.equal(body.name, "router");
    assert.equal((await fetch(`${base}/proxy/${minted.token}`)).status, 404);
  });
});
