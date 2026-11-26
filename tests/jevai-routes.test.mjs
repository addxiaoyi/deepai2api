// jevai 路由测试：管理面（状态/号池/导入/导出/接入）+ 反代面鉴权 + 不干扰其它线。
import { test } from "node:test";
import assert from "node:assert/strict";
import { mkdtempSync, existsSync, mkdirSync, readFileSync, writeFileSync } from "node:fs";
import { tmpdir } from "node:os";
import { join } from "node:path";

import { loadConfig } from "../src/config.mjs";
import { Store } from "../src/store.mjs";
import { createGateway } from "../src/server.mjs";

function startGateway() {
  const dir = mkdtempSync(join(tmpdir(), "jev-routes-"));
  const config = loadConfig(["--port", "18793", "--token", "test-token"]);
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
const KEY = "sk-glm5-abcdefghijklmnop123456";

test("health and panel state report the jevai pool", async () => {
  await withServer(async ({ base }) => {
    const health = await (await fetch(`${base}/health`)).json();
    assert.ok(health.jevai, "health 必须带 jevai");
    assert.equal(health.jevai.keys, 0);
    assert.ok(health.proxyscrape && health.cleverai, "其它线还在");

    const state = await (await fetch(`${base}/api/admin/state`, { headers: admin })).json();
    assert.equal(state.ok, true);
    assert.equal(state.jevai.pool.schema, "dcg.pool/jevai@1");
    assert.equal(state.jevai.running, false);
    assert.ok(state.jevai.apiBase.endsWith("/api/v1"));
  });
});

test("status discovers the worker and reports the relay upstream", async () => {
  await withServer(async ({ base }) => {
    const status = await (await fetch(`${base}/api/admin/jevai/status`, { headers: admin })).json();
    assert.equal(status.ok, true);
    assert.equal(status.toolPresent, true);
    assert.equal(status.pool.schema, "dcg.pool/jevai@1");
    assert.ok(status.relay.upstream.startsWith("https://jev-ai.org"));
  });
});

test("import stores the key, mints one gateway key and applies the export", async () => {
  await withServer(async ({ base, dir }) => {
    const res = await fetch(`${base}/api/admin/jevai/pool/import`, {
      method: "POST",
      headers: { ...admin, ...json },
      body: JSON.stringify({ email: "ada@example.com", apiKey: KEY, emailVerified: true, credits: { credits: 5 } }),
    });
    assert.equal(res.status, 201);
    const body = await res.json();
    assert.equal(body.created, true);
    assert.equal(body.pool.keys, 1);
    assert.ok(body.gatewayKey.plaintext.startsWith("sk-dcg-"), "要发一把网关 Key");
    assert.equal(body.apply.keys, 1);

    // 自动接入：两个文件都写出来了
    assert.ok(existsSync(join(dir, "keys", "jevai.txt")));
    const connection = JSON.parse(readFileSync(join(dir, "keys", "jevai.json"), "utf8"));
    assert.equal(connection.openai_base, "https://jev-ai.org/api/v1");
    assert.deepEqual(connection.keys, [KEY]);
    assert.equal(readFileSync(join(dir, "keys", "jevai.txt"), "utf8").trim(), KEY);

    // 重复导入不重复发 Key
    const again = await fetch(`${base}/api/admin/jevai/pool/import`, {
      method: "POST",
      headers: { ...admin, ...json },
      body: JSON.stringify({ email: "ada@example.com", apiKey: KEY }),
    });
    const againBody = await again.json();
    assert.equal(againBody.created, false);
  });
});

test("import validates the required fields", async () => {
  await withServer(async ({ base }) => {
    const noKey = await fetch(`${base}/api/admin/jevai/pool/import`, {
      method: "POST",
      headers: { ...admin, ...json },
      body: JSON.stringify({ email: "a@b.c" }),
    });
    assert.equal(noKey.status, 400);
    const noEmail = await fetch(`${base}/api/admin/jevai/pool/import`, {
      method: "POST",
      headers: { ...admin, ...json },
      body: JSON.stringify({ apiKey: KEY }),
    });
    assert.equal(noEmail.status, 400);
  });
});

test("pool listing never leaks the upstream key", async () => {
  await withServer(async ({ base }) => {
    await fetch(`${base}/api/admin/jevai/pool/import`, {
      method: "POST",
      headers: { ...admin, ...json },
      body: JSON.stringify({ email: "leak@example.com", apiKey: KEY }),
    });
    const pool = await (await fetch(`${base}/api/admin/jevai/pool`, { headers: admin })).json();
    assert.equal(pool.accounts.length, 1);
    assert.ok(!JSON.stringify(pool).includes(KEY), "池视图只给 preview");
    assert.ok(pool.accounts[0].apiKeyPreview.startsWith("sk-glm5-ab"));
  });
});

test("keys endpoint exposes previews + the api base", async () => {
  await withServer(async ({ base }) => {
    await fetch(`${base}/api/admin/jevai/pool/import`, {
      method: "POST",
      headers: { ...admin, ...json },
      body: JSON.stringify({ email: "k@example.com", apiKey: KEY }),
    });
    const body = await (await fetch(`${base}/api/admin/jevai/keys`, { headers: admin })).json();
    assert.equal(body.apiBase, "https://jev-ai.org/api/v1");
    assert.equal(body.keys.length, 1);
    assert.ok(!JSON.stringify(body).includes(KEY));
  });
});

test("apply and connect hand back what other tools need", async () => {
  await withServer(async ({ base }) => {
    await fetch(`${base}/api/admin/jevai/pool/import`, {
      method: "POST",
      headers: { ...admin, ...json },
      body: JSON.stringify({ email: "c@example.com", apiKey: KEY }),
    });
    const applied = await (await fetch(`${base}/api/admin/jevai/apply`, { method: "POST", headers: admin })).json();
    assert.equal(applied.ok, true);
    assert.equal(applied.keys, 1);
    assert.ok(applied.files.txt.endsWith("jevai.txt"));
    assert.equal(applied.connect.openai_base, "/jevai/v1");

    const connect = await (await fetch(`${base}/api/admin/jevai/connect`, { headers: admin })).json();
    assert.equal(connect.upstreamBase, "https://jev-ai.org/api/v1");
    assert.ok(connect.gatewayBase.endsWith("/jevai/v1"));
    assert.equal(connect.key_prefix, "sk-glm5-");
  });
});

test("export downloads the key list", async () => {
  await withServer(async ({ base }) => {
    await fetch(`${base}/api/admin/jevai/pool/import`, {
      method: "POST",
      headers: { ...admin, ...json },
      body: JSON.stringify({ email: "d@example.com", apiKey: KEY }),
    });
    const res = await fetch(`${base}/api/admin/jevai/pool/export?format=keys&download=1`, {
      method: "POST",
      headers: admin,
    });
    assert.equal(res.status, 200);
    assert.match(res.headers.get("content-disposition") ?? "", /jevai.*\.txt/);
    assert.equal((await res.text()).trim(), KEY);
  });
});

test("relay requires a gateway key and refuses unknown paths", async () => {
  await withServer(async ({ base }) => {
    assert.equal((await fetch(`${base}/jevai/v1/models`)).status, 401);

    const imported = await (
      await fetch(`${base}/api/admin/jevai/pool/import`, {
        method: "POST",
        headers: { ...admin, ...json },
        body: JSON.stringify({ email: "relay@example.com", apiKey: KEY }),
      })
    ).json();
    const gatewayKey = imported.gatewayKey.plaintext;

    const unknown = await fetch(`${base}/jevai/v2/whatever`, { headers: { Authorization: `Bearer ${gatewayKey}` } });
    assert.equal(unknown.status, 404);
    const body = await unknown.json();
    assert.match(body.error.message, /jevai/i);
  });
});

test("relay surfaces a clear error when the pool has no usable key", async () => {
  await withServer(async ({ base }) => {
    const imported = await (
      await fetch(`${base}/api/admin/jevai/pool/import`, {
        method: "POST",
        headers: { ...admin, ...json },
        body: JSON.stringify({ email: "empty@example.com", apiKey: KEY }),
      })
    ).json();
    const pool = await (await fetch(`${base}/api/admin/jevai/pool`, { headers: admin })).json();
    const id = pool.accounts[0].id;
    await fetch(`${base}/api/admin/jevai/pool/${id}`, { method: "DELETE", headers: admin });

    const res = await fetch(`${base}/jevai/v1/models`, {
      headers: { Authorization: `Bearer ${imported.gatewayKey.plaintext}` },
    });
    assert.equal(res.status, 503);
    const body = await res.json();
    assert.equal(body.error.code, "no_account");
  });
});

test("jevai routes do not shadow the other surfaces", async () => {
  await withServer(async ({ base }) => {
    assert.equal((await fetch(`${base}/cleverai/v1/models`)).status, 401);
    assert.equal((await fetch(`${base}/gab/v1/models`)).status, 401);
    assert.equal((await fetch(`${base}/jevai/v9/x`)).status, 401);
    const unknown = await fetch(`${base}/api/admin/jevai/nope`, { headers: admin });
    assert.equal(unknown.status, 404);
  });
});

test("checkin starts a job and reports gains", async () => {
  const dir = mkdtempSync(join(tmpdir(), "jev-chk-"));
  // 假 worker：在临时目录里放一个 jevai_reg 包，`python -m jevai_reg` 直接吐 NDJSON
  const tool = join(dir, "tool");
  mkdirSync(join(tool, "jevai_reg"), { recursive: true });
  writeFileSync(join(tool, "jevai_reg", "cli.py"), "");
  writeFileSync(join(tool, "jevai_reg", "__init__.py"), "");
  writeFileSync(
    join(tool, "jevai_reg", "__main__.py"),
    [
      "import json, sys",
      'print(json.dumps({"type": "checkin", "email": "a@example.com", "gained": 12, "after": 112}), flush=True)',
      'print(json.dumps({"type": "checkin", "email": "b@example.com", "gained": 8, "after": 108}), flush=True)',
      'print(json.dumps({"type": "done", "summary": {"checked": 2, "gained": 20, "results": []}}), flush=True)',
      "sys.exit(0)",
    ].join("\n"),
  );

  const prevTool = process.env.JEVAIS_REGISTER_DIR;
  const prevPy = process.env.JEVAIS_PYTHON;
  process.env.JEVAIS_REGISTER_DIR = tool;
  process.env.JEVAIS_PYTHON = prevPy || "python";
  try {
    await withServer(async ({ base }) => {
      const res = await fetch(`${base}/api/admin/jevai/checkin`, {
        method: "POST",
        headers: { ...admin, ...json },
        body: JSON.stringify({ onlyToday: false, pause: 0 }),
      });
      assert.equal(res.status, 202);
      const body = await res.json();
      assert.equal(body.ok, true);
      assert.equal(body.job.mode, "checkin");

      for (let i = 0; i < 100; i++) {
        const status = await (await fetch(`${base}/api/admin/jevai/status`, { headers: admin })).json();
        if (!status.running) {
          assert.equal(status.job.status, "done", `job 状态 ${status.job.status}`);
          assert.equal(status.job.checked, 2);
          assert.equal(status.job.gained, 20);
          return;
        }
        await new Promise((r) => setTimeout(r, 100));
      }
      assert.fail("checkin job never finished");
    });
  } finally {
    if (prevTool === undefined) delete process.env.JEVAIS_REGISTER_DIR;
    else process.env.JEVAIS_REGISTER_DIR = prevTool;
    if (prevPy === undefined) delete process.env.JEVAIS_PYTHON;
    else process.env.JEVAIS_PYTHON = prevPy;
  }
});