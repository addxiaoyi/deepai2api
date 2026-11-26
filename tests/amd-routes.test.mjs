// AMD 号池与路由测试：GPU 线没有 /v1 反代，只有管理面 + 实例概览。
import { test } from "node:test";
import assert from "node:assert/strict";
import { mkdtempSync } from "node:fs";
import { tmpdir } from "node:os";
import { join } from "node:path";

import { loadConfig } from "../src/config.mjs";
import { Store } from "../src/store.mjs";
import { createGateway } from "../src/server.mjs";
import { LINE, POOL_SCHEMA, at as openPool, instanceSummary } from "../src/amd-pool.mjs";

const admin = { Authorization: "Bearer test-token" };
const json = { "Content-Type": "application/json" };

async function withServer(run) {
  const dir = mkdtempSync(join(tmpdir(), "amd-routes-"));
  const config = loadConfig(["--port", "18789", "--token", "test-token"]);
  config.dataDir = dir;
  const store = Store.at(dir);
  const lane = { base: "http://127.0.0.1:1", instance: "test", getCatalog: async () => [], getAllowance: async () => null, checkInFor: () => null };
  const scheduler = { candidates: () => [], record: () => {}, pin: () => {}, status: () => ({}) };
  const gateway = createGateway({ config, lane, store, scheduler, log: () => {} });
  await new Promise((resolve) => gateway.server.listen(0, "127.0.0.1", resolve));
  const base = `http://127.0.0.1:${gateway.server.address().port}`;
  try {
    await run({ base, dir });
  } finally {
    await new Promise((resolve) => gateway.server.close(resolve));
    store.flush?.();
  }
}

test("amd line descriptor has no model surface", () => {
  assert.equal(POOL_SCHEMA, "dcg.pool/amd@1");
  assert.equal(LINE.kind, "gpu-pool");
  assert.equal(LINE.apiBase, "https://developer.amd.com.cn/api");
  assert.deepEqual(LINE.models, [], "GPU 线没有模型列表");
  assert.equal(LINE.relayPrefix, "/amd/v1/"); // 描述里有，但 server 不为它挂反代
});

test("instanceSummary aggregates points and instances", () => {
  const dir = mkdtempSync(join(tmpdir(), "amd-pool-"));
  const pool = openPool(dir);
  pool.add({ email: "a@example.com", apiKey: "session=a", credits: { points: 100, hasInstance: true } });
  pool.add({ email: "b@example.com", apiKey: "session=b", credits: { points: 50, hasInstance: false } });
  const summary = instanceSummary(pool);
  assert.equal(summary.total, 2);
  assert.equal(summary.withInstance, 1);
  assert.equal(summary.totalPoints, 150);
  assert.ok(summary.rows[0].cookiePreview.includes("…"), "cookie 只给预览");
});

test("health and state expose the amd pool", async () => {
  await withServer(async ({ base }) => {
    const health = await (await fetch(`${base}/health`)).json();
    assert.ok(health.amd, "health 要带 amd");
    assert.equal(health.amd.accounts, 0);

    const state = await (await fetch(`${base}/api/admin/state`, { headers: admin })).json();
    assert.equal(state.amd.pool.schema, "dcg.pool/amd@1");
    assert.equal(state.amd.apiBase, "https://developer.amd.com.cn/api");
  });
});

test("import stores the cookie and reports points", async () => {
  await withServer(async ({ base }) => {
    const res = await fetch(`${base}/api/admin/amd/pool/import`, {
      method: "POST",
      headers: { ...admin, ...json },
      body: JSON.stringify({ email: "dev@example.com", apiKey: "session=cookie-value", credits: { points: 100 } }),
    });
    assert.equal(res.status, 201);
    const body = await res.json();
    assert.equal(body.created, true);
    assert.equal(body.instances.totalPoints, 100);
    assert.ok(body.gatewayKey.plaintext.startsWith("sk-dcg-"));

    const pool = await (await fetch(`${base}/api/admin/amd/pool`, { headers: admin })).json();
    assert.equal(pool.stats.total, 1);
    assert.ok(!JSON.stringify(pool).includes("session=cookie-value"), "池视图不能回显完整 cookie");
  });
});

test("import requires both email and cookie", async () => {
  await withServer(async ({ base }) => {
    const noCookie = await fetch(`${base}/api/admin/amd/pool/import`, {
      method: "POST",
      headers: { ...admin, ...json },
      body: JSON.stringify({ email: "a@b.c" }),
    });
    assert.equal(noCookie.status, 400);
    const noEmail = await fetch(`${base}/api/admin/amd/pool/import`, {
      method: "POST",
      headers: { ...admin, ...json },
      body: JSON.stringify({ apiKey: "session=x" }),
    });
    assert.equal(noEmail.status, 400);
  });
});

test("amd has admin routes but no /v1 relay", async () => {
  await withServer(async ({ base }) => {
    const status = await (await fetch(`${base}/api/admin/amd/status`, { headers: admin })).json();
    assert.equal(status.toolPresent, true);
    assert.equal(status.line, "amd");

    // 没有模型面：未带 Key 先被鉴权挡住（401），带合法 Key 后是 404（未挂反代）
    assert.equal((await fetch(`${base}/amd/v1/models`)).status, 401);
    const imported = await (
      await fetch(`${base}/api/admin/amd/pool/import`, {
        method: "POST",
        headers: { ...admin, ...json },
        body: JSON.stringify({ email: "relay@example.com", apiKey: "session=x" }),
      })
    ).json();
    const relay = await fetch(`${base}/amd/v1/models`, {
      headers: { Authorization: `Bearer ${imported.gatewayKey.plaintext}` },
    });
    assert.equal(relay.status, 404, "GPU 线不挂 /amd/v1 反代");
  });
});
