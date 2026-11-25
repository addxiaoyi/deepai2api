// gab.ai 线的闭环接口测试：管理面（注册/入池/导出）+ 反代面（鉴权/未知路径/无账号）。
// 不打真实上游：所有用例都在「没有可用账号」或「参数校验」这一层返回。
import { test } from "node:test";
import assert from "node:assert/strict";
import { mkdtempSync } from "node:fs";
import { tmpdir } from "node:os";
import { join } from "node:path";

// 把上游指到本机一个关闭的端口：故障切换会立刻失败，测试不依赖网络、也不会等 3 次超时。
process.env.GABAI_UPSTREAM = "http://127.0.0.1:1";
process.env.GABAI_API_BASE = "http://127.0.0.1:1/v1";
process.env.GABAI_BASE_URL = "http://127.0.0.1:1";

import { loadConfig } from "../src/config.mjs";
import { Store } from "../src/store.mjs";
import { createGateway } from "../src/server.mjs";

function startGateway() {
  const dir = mkdtempSync(join(tmpdir(), "gb-routes-"));
  const config = loadConfig(["--port", "18797", "--token", "test-token"]);
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
  return { gateway, store };
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

test("health and panel state both report the gabai pool", async () => {
  await withServer(async ({ base }) => {
    const health = await (await fetch(`${base}/health`)).json();
    assert.equal(health.ok, true);
    assert.ok(health.gabai, "health must expose gabai stats");
    assert.equal(health.gabai.accounts, 0);
    assert.ok(health.cleverai && health.allmodels, "other lines untouched");

    const state = await (await fetch(`${base}/api/admin/state`, { headers: admin })).json();
    assert.equal(state.ok, true);
    assert.ok(state.gabai, "panel state must carry gabai");
    assert.equal(state.gabai.pool.schema, "dcg.pool/gabai@1");
    assert.equal(state.gabai.running, false);
  });
});

test("gabai admin status/pool are empty but wired", async () => {
  await withServer(async ({ base }) => {
    const status = await (await fetch(`${base}/api/admin/gabai/status`, { headers: admin })).json();
    assert.equal(status.ok, true);
    assert.equal(status.running, false);
    assert.equal(status.pool.schema, "dcg.pool/gabai@1");
    assert.equal(status.toolPresent, true, "tools/gabai-reg must be discoverable");

    const pool = await (await fetch(`${base}/api/admin/gabai/pool`, { headers: admin })).json();
    assert.equal(pool.ok, true);
    assert.deepEqual(pool.accounts, []);
  });
});

test("gabai pool import mints a gateway key and is idempotent", async () => {
  await withServer(async ({ base }) => {
    const record = { email: "gab@example.com", apiKey: "gab_live_abcdef0123456789", name: "Gab" };
    const first = await fetch(`${base}/api/admin/gabai/pool/import`, {
      method: "POST",
      headers: { ...admin, ...json },
      body: JSON.stringify(record),
    });
    assert.equal(first.status, 201);
    const created = await first.json();
    assert.ok(created.gatewayKey.plaintext.startsWith("sk-dcg-"));
    assert.ok(!JSON.stringify(created.account).includes("gab_live_abcdef0123456789"), "pool view must not leak the key");

    const again = await fetch(`${base}/api/admin/gabai/pool/import`, {
      method: "POST",
      headers: { ...admin, ...json },
      body: JSON.stringify({ ...record, name: "Gab 2" }),
    });
    assert.equal(again.status, 200);
    const merged = await again.json();
    assert.equal(merged.created, false);
    assert.equal(merged.gatewayKey.plaintext, created.gatewayKey.plaintext, "re-import must reuse the key");

    const pool = await (await fetch(`${base}/api/admin/gabai/pool`, { headers: admin })).json();
    assert.equal(pool.stats.total, 1);
  });
});

test("gabai pool import rejects records without an apiKey", async () => {
  await withServer(async ({ base }) => {
    const res = await fetch(`${base}/api/admin/gabai/pool/import`, {
      method: "POST",
      headers: { ...admin, ...json },
      body: JSON.stringify({ email: "nokey@example.com" }),
    });
    assert.equal(res.status, 400);
  });
});

test("gabai relay needs a gateway key and rejects unknown paths", async () => {
  await withServer(async ({ base }) => {
    const anonymous = await fetch(`${base}/gab/v1/models`);
    assert.equal(anonymous.status, 401, "relay surface must require a gateway key");

    const imported = await (
      await fetch(`${base}/api/admin/gabai/pool/import`, {
        method: "POST",
        headers: { ...admin, ...json },
        body: JSON.stringify({ email: "relay@example.com", apiKey: "gab_live_relay000111222333" }),
      })
    ).json();
    const key = imported.gatewayKey.plaintext;

    const unknown = await fetch(`${base}/gab/v1/not-a-route`, {
      method: "POST",
      headers: { Authorization: `Bearer ${key}`, ...json },
      body: "{}",
    });
    assert.equal(unknown.status, 404);
    const body = await unknown.json();
    assert.match(body.error.message, /gab route/i);

    // 号池里只有一个账号，且这台机器打不到 gab.ai：应是我们自己的 upstream 错误，
    // 而不是"没鉴权"或"路由不存在"。
    const chat = await fetch(`${base}/gab/v1/chat/completions`, {
      method: "POST",
      headers: { Authorization: `Bearer ${key}`, ...json },
      body: JSON.stringify({ model: "gab-1", messages: [{ role: "user", content: "hi" }] }),
    });
    assert.ok(chat.status >= 400 && chat.status !== 401 && chat.status !== 404, `unexpected status ${chat.status}`);
  });
});

test("gabai pool export downloads all three formats", async () => {
  await withServer(async ({ base }) => {
    await fetch(`${base}/api/admin/gabai/pool/import`, {
      method: "POST",
      headers: { ...admin, ...json },
      body: JSON.stringify({ email: "exp@example.com", apiKey: "gab_live_exp000111222333444" }),
    });
    for (const format of ["jsonl", "csv", "pool"]) {
      const res = await fetch(`${base}/api/admin/gabai/pool/export?format=${format}&download=1`, {
        method: "POST",
        headers: admin,
      });
      assert.equal(res.status, 200, `export ${format}`);
      const text = await res.text();
      assert.ok(text.includes("exp@example.com"), `export ${format} must contain the account`);
    }
  });
});

test("gabai routes do not shadow /v1 or /cleverai", async () => {
  await withServer(async ({ base }) => {
    const deepai = await fetch(`${base}/v1/models`, { headers: { Authorization: "Bearer sk-dcg-bogus-key-value-123456" } });
    assert.equal(deepai.status, 401, "/v1 still resolves to the DeepAI lane");

    const cleverai = await fetch(`${base}/cleverai/v1/models`);
    assert.equal(cleverai.status, 401, "cleverai surface still guarded");

    const gabai = await fetch(`${base}/gab/v1/models`);
    assert.equal(gabai.status, 401, "gabai surface guarded independently");
  });
});
