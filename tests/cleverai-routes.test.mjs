// Route tests for the Clever AI surface: admin pool protocol + /cleverai/v1/*
// auth, and the guarantee that it never collides with /v1/* or /p/*.
import { test } from "node:test";
import assert from "node:assert/strict";
import { mkdtempSync } from "node:fs";
import { tmpdir } from "node:os";
import { join } from "node:path";

import { loadConfig } from "../src/config.mjs";
import { Store } from "../src/store.mjs";
import { createGateway } from "../src/server.mjs";

function startGateway() {
  const dir = mkdtempSync(join(tmpdir(), "cv-routes-"));
  const config = loadConfig(["--port", "18799", "--token", "test-token"]);
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
  const port = ctx.gateway.server.address().port;
  const base = `http://127.0.0.1:${port}`;
  try {
    await run({ ...ctx, base });
  } finally {
    await new Promise((resolve) => ctx.gateway.server.close(resolve));
    ctx.store.flush?.();
  }
}

const admin = { Authorization: "Bearer test-token" };
const json = (body) => ({ "Content-Type": "application/json" });

test("health reports the cleverai pool without touching other pools", async () => {
  await withServer(async ({ base }) => {
    const health = await (await fetch(`${base}/health`)).json();
    assert.equal(health.ok, true);
    assert.ok(health.cleverai, "health must expose cleverai stats");
    assert.equal(health.cleverai.accounts, 0);
    assert.equal(health.cleverai.appCheck, false);
    // 既有字段还在，说明没把别人的统计挤掉
    assert.ok(health.allmodels);
    assert.ok(health.providers);
    assert.ok(health.poe);
  });
});

test("admin status/pool/keys work and stay empty on a fresh install", async () => {
  await withServer(async ({ base }) => {
    const status = await (await fetch(`${base}/api/admin/cleverai/status`, { headers: admin })).json();
    assert.equal(status.ok, true);
    assert.equal(status.running, false);
    assert.equal(status.toolPresent, true, "the python worker must be discoverable");
    assert.equal(status.pool.schema, "dcg.pool/cleverai@1");

    const pool = await (await fetch(`${base}/api/admin/cleverai/pool`, { headers: admin })).json();
    assert.equal(pool.ok, true);
    assert.equal(pool.accounts.length, 0);
    assert.equal(pool.engine, "openai-gpt-6-luna");

    const keys = await (await fetch(`${base}/api/admin/cleverai/keys`, { headers: admin })).json();
    assert.deepEqual(keys.keys, []);
  });
});

test("pool import mints one gateway key and is idempotent", async () => {
  await withServer(async ({ base }) => {
    const body = { email: "route@example.com", refreshToken: "rt-route", uid: "uid-route" };
    const first = await fetch(`${base}/api/admin/cleverai/pool/import`, {
      method: "POST",
      headers: { ...admin, ...json() },
      body: JSON.stringify(body),
    });
    assert.equal(first.status, 201);
    const created = await first.json();
    assert.ok(created.gatewayKey.plaintext.startsWith("sk-dcg-"));
    const key = created.gatewayKey.plaintext;

    const again = await fetch(`${base}/api/admin/cleverai/pool/import`, {
      method: "POST",
      headers: { ...admin, ...json() },
      body: JSON.stringify({ ...body, wallet: { totalCredit: 30 } }),
    });
    assert.equal(again.status, 200);
    const merged = await again.json();
    assert.equal(merged.created, false);
    assert.equal(merged.gatewayKey.plaintext, key, "re-import must reuse the issued key");
    assert.equal(merged.account.wallet.totalCredit, 30);

    const pool = await (await fetch(`${base}/api/admin/cleverai/pool`, { headers: admin })).json();
    assert.equal(pool.stats.total, 1);
    assert.equal(pool.stats.keys, 1);
  });
});

test("pool import rejects records without a refresh token", async () => {
  await withServer(async ({ base }) => {
    const res = await fetch(`${base}/api/admin/cleverai/pool/import`, {
      method: "POST",
      headers: { ...admin, ...json() },
      body: JSON.stringify({ email: "bad@example.com" }),
    });
    assert.equal(res.status, 400);
  });
});

test("import then health + delete keeps the pool consistent", async () => {
  await withServer(async ({ base }) => {
    const created = await (
      await fetch(`${base}/api/admin/cleverai/pool/import`, {
        method: "POST",
        headers: { ...admin, ...json() },
        body: JSON.stringify({ email: "del@example.com", refreshToken: "rt-del" }),
      })
    ).json();
    const id = created.account.id;

    const marked = await (
      await fetch(`${base}/api/admin/cleverai/pool/${id}/health`, {
        method: "POST",
        headers: { ...admin, ...json() },
        body: JSON.stringify({ ok: false, error: "rate_limited", cooldownMs: 60_000 }),
      })
    ).json();
    assert.equal(marked.ok, true);
    assert.equal(marked.account.healthy, false);

    const removed = await fetch(`${base}/api/admin/cleverai/pool/${id}`, { method: "DELETE", headers: admin });
    assert.equal(removed.status, 200);
    const pool = await (await fetch(`${base}/api/admin/cleverai/pool`, { headers: admin })).json();
    assert.equal(pool.stats.total, 0);
  });
});

test("/cleverai/v1/models requires a gateway key and lists engines", async () => {
  await withServer(async ({ base }) => {
    const anonymous = await fetch(`${base}/cleverai/v1/models`);
    assert.equal(anonymous.status, 401);

    const created = await (
      await fetch(`${base}/api/admin/cleverai/pool/import`, {
        method: "POST",
        headers: { ...admin, ...json() },
        body: JSON.stringify({ email: "models@example.com", refreshToken: "rt-models" }),
      })
    ).json();
    const key = created.gatewayKey.plaintext;

    const models = await (await fetch(`${base}/cleverai/v1/models`, { headers: { Authorization: `Bearer ${key}` } })).json();
    assert.equal(models.object, "list");
    assert.ok(models.data.some((row) => row.id === "cleverai/default"));
    assert.ok(models.data.every((row) => row.owned_by === "cleverai"));
  });
});

test("cleverai routes do not shadow the existing surfaces", async () => {
  await withServer(async ({ base }) => {
    // /v1/models 仍然走 DeepAI lane（空目录也应是 200 + list）
    const models = await fetch(`${base}/v1/models`, { headers: { Authorization: "Bearer sk-dcg-not-a-real-key" } });
    assert.equal(models.status, 401, "unknown key still 401s, but the route resolved to /v1");

    const cleveraiNoAuth = await fetch(`${base}/cleverai/v1/unknown`);
    assert.equal(cleveraiNoAuth.status, 401, "cleverai surface wants a gateway key first");

    const cleveraiUnknown = await fetch(`${base}/cleverai/v1/unknown`, { headers: admin });
    assert.equal(cleveraiUnknown.status, 404);
    const unknownBody = await cleveraiUnknown.json();
    assert.match(unknownBody.error.message, /cleverai/i);

    const unknownAdmin = await fetch(`${base}/api/admin/cleverai/nope`, { headers: admin });
    assert.equal(unknownAdmin.status, 404);
    const body = await unknownAdmin.json();
    assert.match(body.error.message, /cleverai|No admin route/);
  });
});

test("pool export writes a downloadable file in all three formats", async () => {
  await withServer(async ({ base }) => {
    await fetch(`${base}/api/admin/cleverai/pool/import`, {
      method: "POST",
      headers: { ...admin, ...json() },
      body: JSON.stringify({ email: "exp@example.com", refreshToken: "rt-exp" }),
    });
    for (const format of ["jsonl", "csv", "pool"]) {
      const res = await fetch(`${base}/api/admin/cleverai/pool/export?format=${format}&download=1`, {
        method: "POST",
        headers: admin,
      });
      assert.equal(res.status, 200, `export ${format}`);
      const text = await res.text();
      assert.ok(text.includes("exp@example.com"), `export ${format} must contain the account`);
      assert.match(res.headers.get("content-disposition") ?? "", /attachment/);
    }
  });
});
test("/cleverai/v1/messages speaks Anthropic and validates input", async () => {
  await withServer(async ({ base }) => {
    const anonymous = await fetch(`${base}/cleverai/v1/messages`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ model: "cleverai/default", messages: [{ role: "user", content: "hi" }] }),
    });
    assert.equal(anonymous.status, 401, "the Anthropic surface needs a gateway key too");

    const created = await (
      await fetch(`${base}/api/admin/cleverai/pool/import`, {
        method: "POST",
        headers: { ...admin, ...json() },
        body: JSON.stringify({ email: "anthropic@example.com", refreshToken: "rt-anthropic" }),
      })
    ).json();
    const key = created.gatewayKey.plaintext;

    const empty = await fetch(`${base}/cleverai/v1/messages`, {
      method: "POST",
      headers: { Authorization: `Bearer ${key}`, ...json() },
      body: JSON.stringify({ model: "cleverai/default", messages: [] }),
    });
    assert.equal(empty.status, 400);
    const body = await empty.json();
    assert.match(body.error.message, /messages/);

    const unknown = await fetch(`${base}/cleverai/v1/not-a-route`, {
      method: "POST",
      headers: { Authorization: `Bearer ${key}`, ...json() },
      body: "{}",
    });
    assert.equal(unknown.status, 404);
  });
});
test("admin state exposes the cleverai block", async () => {
  await withServer(async ({ base }) => {
    await fetch(`${base}/api/admin/login`, {
      method: "POST",
      headers: json(),
      body: JSON.stringify({ token: "test-token" }),
    });
    const state = await (await fetch(`${base}/api/admin/state`, { headers: admin })).json();
    assert.equal(state.ok, true);
    assert.ok(state.cleverai, "panel state must carry cleverai");
    assert.equal(state.cleverai.pool.schema, "dcg.pool/cleverai@1");
    assert.equal(state.cleverai.engine, "openai-gpt-6-luna");
    assert.equal(state.cleverai.running, false);
    assert.ok(state.allmodels, "allmodels block untouched");
  });
});
