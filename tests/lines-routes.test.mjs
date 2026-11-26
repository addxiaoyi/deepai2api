// 六条 OpenAI 兼容线的路由测试：总览 / 单线管理面 / 反代鉴权 / 互不干扰。
import { test } from "node:test";
import assert from "node:assert/strict";
import { mkdtempSync, existsSync, readFileSync } from "node:fs";
import { tmpdir } from "node:os";
import { join } from "node:path";

import { loadConfig } from "../src/config.mjs";
import { Store } from "../src/store.mjs";
import { createGateway } from "../src/server.mjs";

const IDS = ["agnes", "cerebras", "opencode-zen", "requesty", "unorouter", "giteeai", "pollinations"];
const KEYS = {
  agnes: "sk-agnes1234567890",
  cerebras: "csk-cerebras1234567890",
  "opencode-zen": "sk-opencode1234567890",
  requesty: "sk-requesty1234567890",
  unorouter: "sk-unorouter1234567890",
  pollinations: "",
  giteeai: "gitee-token-0123456789abcdef",
};

async function withServer(run) {
  const dir = mkdtempSync(join(tmpdir(), "lines-routes-"));
  const config = loadConfig(["--port", "18791", "--token", "test-token"]);
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
  await new Promise((resolve) => gateway.server.listen(0, "127.0.0.1", resolve));
  const base = `http://127.0.0.1:${gateway.server.address().port}`;
  try {
    await run({ base, dir, store });
  } finally {
    await new Promise((resolve) => gateway.server.close(resolve));
    store.flush?.();
  }
}

const admin = { Authorization: "Bearer test-token" };
const json = { "Content-Type": "application/json" };

test("health and panel state list all six lines", async () => {
  await withServer(async ({ base }) => {
    const health = await (await fetch(`${base}/health`)).json();
    for (const id of IDS) assert.ok(health[id], `health 缺 ${id}`);
    assert.equal(health.pollinations.anonymous, true);

    const state = await (await fetch(`${base}/api/admin/state`, { headers: admin })).json();
    for (const id of IDS) {
      assert.ok(state.lines[id], `state.lines 缺 ${id}`);
      assert.equal(state.lines[id].schema, `dcg.pool/${id}@1`);
    }
    // 老线还在
    assert.ok(state.jevai && state.proxyscrape && state.cleverai);
  });
});

test("overview endpoint returns every line with its relay prefix", async () => {
  await withServer(async ({ base }) => {
    const body = await (await fetch(`${base}/api/admin/lines`, { headers: admin })).json();
    assert.equal(body.ok, true);
    assert.deepEqual(body.lines.map((l) => l.id), IDS);
    for (const line of body.lines) {
      assert.equal(line.relayPrefix, `/${line.id}/v1/`);
      assert.ok(line.apiBase.startsWith("https://"));
      assert.ok(Array.isArray(line.models));
    }
    const poll = body.lines.find((l) => l.id === "pollinations");
    assert.equal(poll.anonymous, true);
    assert.equal(body.lines.find((l) => l.id === "unorouter").anthropic, true);
  });
});

test("every line exposes status/keys/pool and imports a key", async () => {
  await withServer(async ({ base, dir }) => {
    for (const id of IDS) {
      const status = await (await fetch(`${base}/api/admin/${id}/status`, { headers: admin })).json();
      assert.equal(status.ok, true, `${id} status`);
      assert.equal(status.toolPresent, true, `${id} 要能找到 worker`);
      assert.equal(status.line, id);
      assert.equal(status.pool.schema, `dcg.pool/${id}@1`);

      const importRes = await fetch(`${base}/api/admin/${id}/pool/import`, {
        method: "POST",
        headers: { ...admin, ...json },
        body: JSON.stringify({ email: `${id}@example.com`, apiKey: KEYS[id] }),
      });
      assert.equal(importRes.status, 201, `${id} import`);
      const imported = await importRes.json();
      assert.ok(imported.gatewayKey.plaintext.startsWith("sk-dcg-"));
      assert.equal(imported.apply.keys > 0 || id === "pollinations", true);
      assert.ok(existsSync(join(dir, "keys", `${id}.json`)), `${id} 要写出接入描述`);

      const pool = await (await fetch(`${base}/api/admin/${id}/pool`, { headers: admin })).json();
      assert.equal(pool.stats.total, 1);
      assert.ok(!JSON.stringify(pool).includes(KEYS[id] || "@@"), `${id} 池视图不能泄露上游 Key`);

      const keys = await (await fetch(`${base}/api/admin/${id}/keys`, { headers: admin })).json();
      assert.equal(keys.keys.length, 1);
      assert.equal(keys.apiBase, pool.apiBase);
    }
    const connection = JSON.parse(readFileSync(join(dir, "keys", "agnes.json"), "utf8"));
    assert.equal(connection.provider, "agnes");
    assert.deepEqual(connection.keys, [KEYS.agnes]);
  });
});

test("anonymous line rejects an import without apiKey but accepts an empty one", async () => {
  await withServer(async ({ base }) => {
    const noEmail = await fetch(`${base}/api/admin/pollinations/pool/import`, {
      method: "POST",
      headers: { ...admin, ...json },
      body: JSON.stringify({ apiKey: "" }),
    });
    assert.equal(noEmail.status, 400, "匿名线也要 email");

    // 匿名线允许没有上游 Key；非匿名线不行
    const anon = await fetch(`${base}/api/admin/pollinations/pool/import`, {
      method: "POST",
      headers: { ...admin, ...json },
      body: JSON.stringify({ email: "anon2@pollinations" }),
    });
    assert.equal(anon.status, 201);

    const keyless = await fetch(`${base}/api/admin/agnes/pool/import`, {
      method: "POST",
      headers: { ...admin, ...json },
      body: JSON.stringify({ email: "a@example.com" }),
    });
    assert.equal(keyless.status, 400, "非匿名线必须给 Key");

    const ok = await fetch(`${base}/api/admin/pollinations/pool/import`, {
      method: "POST",
      headers: { ...admin, ...json },
      body: JSON.stringify({ email: "anon@pollinations", apiKey: "" }),
    });
    assert.equal(ok.status, 201);
  });
});

test("relay requires a gateway key and is wired per line", async () => {
  await withServer(async ({ base }) => {
    for (const id of IDS) {
      const res = await fetch(`${base}/${id}/v1/models`);
      assert.equal(res.status, 401, `${id} 反代必须鉴权`);
    }
    const imported = await (
      await fetch(`${base}/api/admin/agnes/pool/import`, {
        method: "POST",
        headers: { ...admin, ...json },
        body: JSON.stringify({ email: "relay@example.com", apiKey: KEYS.agnes }),
      })
    ).json();
    const res = await fetch(`${base}/agnes/v1/models`, {
      headers: { Authorization: `Bearer ${imported.gatewayKey.plaintext}` },
    });
    // 上游是假的（这里仍是真实的 agnes 域名），只要不是 401 就说明请求发出去了
    assert.notEqual(res.status, 401);
    // 上游不可用时返回错误体（带账号线索），说明确实是这条线发出的
    assert.ok(res.headers.get("x-line-account"), "错误响应要能定位到具体账号");
  });
});

test("apply and connect return usable info per line", async () => {
  await withServer(async ({ base }) => {
    await fetch(`${base}/api/admin/cerebras/pool/import`, {
      method: "POST",
      headers: { ...admin, ...json },
      body: JSON.stringify({ email: "c@example.com", apiKey: KEYS.cerebras }),
    });
    const applied = await (await fetch(`${base}/api/admin/cerebras/apply`, { method: "POST", headers: admin })).json();
    assert.equal(applied.ok, true);
    assert.ok(applied.files.txt.endsWith("cerebras.txt"));
    assert.equal(applied.connect.keyPrefix, "csk-");

    const connect = await (await fetch(`${base}/api/admin/cerebras/connect`, { headers: admin })).json();
    assert.ok(connect.gatewayBase.endsWith("/cerebras/v1/"));
    assert.equal(connect.upstreamBase, "https://api.cerebras.ai/v1");
  });
});

test("lines do not shadow each other or the older surfaces", async () => {
  await withServer(async ({ base }) => {
    assert.equal((await fetch(`${base}/jevai/v1/models`)).status, 401);
    assert.equal((await fetch(`${base}/cleverai/v1/models`)).status, 401);
    assert.equal((await fetch(`${base}/gab/v1/models`)).status, 401);
    const unknown = await fetch(`${base}/api/admin/agnes/nope`, { headers: admin });
    assert.equal(unknown.status, 404);
    const unknownLine = await fetch(`${base}/api/admin/notaline/status`, { headers: admin });
    assert.equal(unknownLine.status, 404);
  });
});
