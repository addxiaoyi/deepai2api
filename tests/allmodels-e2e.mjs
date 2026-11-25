// End-to-end: batch-register against a mock dashboard, land the accounts in the
// gateway pool, mint gateway keys, export accounts + keys.
//
//   node tests/allmodels-e2e.mjs
//
// Uses the real Python worker (tools/allmodels-reg) and the real pool/controller
// path; only the dashboard itself is mocked.
import assert from "node:assert/strict";
import { createServer } from "node:net";
import { existsSync, mkdtempSync, readFileSync, rmSync } from "node:fs";
import { tmpdir } from "node:os";
import { join } from "node:path";

import { loadConfig } from "../src/config.mjs";
import { makeMachineHeader } from "../src/machine.mjs";
import { makeDispatcher } from "../src/dispatcher.mjs";
import { makeLane } from "../src/lane.mjs";
import { Store } from "../src/store.mjs";
import { createGateway } from "../src/server.mjs";
import { makeScheduler } from "../src/scheduler.mjs";
import { createMockAllmodels } from "./mock-allmodels.mjs";

const adminToken = "allmodels-test-token";
const dataDir = mkdtempSync(join(tmpdir(), "dcg-allmodels-"));
const mock = await createMockAllmodels({});

const freePort = () =>
  new Promise((done) => {
    const probe = createServer();
    probe.listen(0, "127.0.0.1", () => {
      const { port } = probe.address();
      probe.close(() => done(port));
    });
  });

const port = await freePort();
const config = {
  ...loadConfig([]),
  port,
  host: "127.0.0.1",
  token: adminToken,
  dataDir,
  apiOrigin: mock.url,
  signInOrigin: mock.url,
  webuiUrl: null,
  log: "warn",
};

const store = Store.at(dataDir);
const { dispatcher } = makeDispatcher();
const lane = makeLane({
  config,
  getAccount: () => store.account,
  machineHeader: makeMachineHeader("11111111-2222-3333-4444-555555555555"),
  dispatcher,
});
const scheduler = makeScheduler({ store });
const { server, allmodels: gatewayAllmodels } = createGateway({ config, lane, store, scheduler });
const gateway = { allmodels: gatewayAllmodels };
await new Promise((done) => server.listen(port, "127.0.0.1", done));
const base = `http://127.0.0.1:${port}`;

const results = [];
const check = async (name, fn) => {
  const started = Date.now();
  try {
    await fn();
    results.push({ name, ok: true });
    console.log(`PASS ${name} (${Date.now() - started}ms)`);
  } catch (error) {
    results.push({ name, ok: false, error: error.message });
    console.log(`FAIL ${name}: ${error.message}`);
  }
};

const call = async (path, { method = "GET", body, token = adminToken } = {}) => {
  const res = await fetch(base + path, {
    method,
    headers: {
      ...(body ? { "Content-Type": "application/json" } : {}),
      ...(token ? { Authorization: `Bearer ${token}` } : {}),
    },
    body: body ? JSON.stringify(body) : undefined,
  });
  const text = await res.text();
  let json = null;
  try {
    json = text ? JSON.parse(text) : null;
  } catch {
    json = null;
  }
  return { status: res.status, body: json, text, headers: res.headers };
};


await check("batch register via the admin API creates 3 accounts through the python worker", async () => {
  const started = await call("/api/admin/allmodels/register", {
    method: "POST",
    body: {
      count: 3,
      concurrency: 3,
      baseUrl: mock.url,
      mailProvider: "fixed",
      email: "ops@example.com",
      captcha: "manual",
      captchaKey: "probe-token",
      verifyEmail: false,
      export: "jsonl",
    },
  });
  assert.equal(started.status, 202, started.text.slice(0, 400));
  assert.equal(started.body.running, true);

  const deadline = Date.now() + 120_000;
  let status = started.body;
  while (status.running && Date.now() < deadline) {
    await new Promise((done) => setTimeout(done, 500));
    status = (await call("/api/admin/allmodels/status")).body;
  }
  assert.equal(status.running, false, "job did not finish in time");
  assert.equal(status.job.status, "done", JSON.stringify(status.job?.lines?.slice(-6) ?? status).slice(0, 800));
  assert.equal(status.job.created, 3);
  assert.equal(status.job.failed, 0);
  assert.ok(status.job.export?.file, 'the finished job must export the pool');
  assert.equal(status.job.export.rows, 3);
  assert.ok(existsSync(status.job.export.file));
  // A retried unit can leave one extra address behind in the mock (e.g. a
  // persona collision), so only the floor is asserted here.
  assert.ok(mock.state.accounts.size >= 3, `mock accounts: ${mock.state.accounts.size}`);
  assert.ok(
    mock.state.calls.filter((path) => path === "POST /api/auth/sign-up/email").length >= 3,
    "the worker must actually hit the sign-up endpoint",
  );
});

await check("accounts, dashboard keys and gateway keys are all in the pool", async () => {
  const res = await call("/api/admin/allmodels/pool");
  assert.equal(res.status, 200);
  assert.equal(res.body.accounts.length, 3);
  assert.equal(res.body.stats.active, 3);
  for (const account of res.body.accounts) {
    assert.match(account.email, /^ops\+[a-z0-9._-]+@example\.com$/);
    assert.ok(account.apiKeyPreview.includes("…"), "apiKey must be masked in the API view");
    assert.ok(account.gatewayKey?.prefix?.startsWith("sk-dcg-"), "each account needs a gateway key");
    assert.equal(account.projectId, "proj1");
    assert.equal(account.guardrailId, "gr1");
    assert.equal(account.emailVerified, true);
  }

  const poolFile = join(dataDir, "allmodels-pool.json");
  assert.ok(existsSync(poolFile), "pool file must exist");
  const raw = JSON.parse(readFileSync(poolFile, "utf8"));
  assert.equal(raw.schema, "dcg.pool/allmodels@1");
  assert.equal(raw.accounts.length, 3);
  for (const account of raw.accounts) {
    assert.ok(account.apiKey.startsWith("sk_live_"), "dashboard key must be stored");
    assert.ok(account.gatewayKey.plaintext.startsWith("sk-dcg-"), "gateway key plaintext must be stored");
  }

  const keys = store.listKeys().filter((key) => key.label.startsWith("allmodels:"));
  assert.equal(keys.length, 3, "one gateway key per account");
});

await check("the pool exposes key metadata without leaking secrets", async () => {
  const res = await call("/api/admin/allmodels/keys");
  assert.equal(res.status, 200);
  assert.equal(res.body.keys.length, 3);
  for (const row of res.body.keys) {
    assert.ok(row.keyPrefix);
    assert.ok(row.gatewayKey.prefix.startsWith("sk-dcg-"));
    assert.equal(row.gatewayKey.plaintext, undefined);
  }
});

await check("export writes accounts + keys in one jsonl file", async () => {
  const res = await call("/api/admin/allmodels/pool/export?format=jsonl&download=1");
  assert.equal(res.status, 200);
  const lines = res.text.trim().split("\n");
  assert.equal(lines.length, 3);
  const record = JSON.parse(lines[0]);
  assert.ok(record.apiKey.startsWith("sk_live_"));
  assert.ok(record.gatewayKey.plaintext.startsWith("sk-dcg-"));
  assert.ok(res.headers.get("content-disposition")?.includes("attachment"));
});

await check("import is idempotent on apiKey and mints one gateway key", async () => {
  const record = {
    email: "manual@example.com",
    password: "pw-123456",
    apiKey: "sk_live_manual_0123456789abcdef",
    keyId: "manual-key",
    keyPrefix: "sk_live_",
    keyLast4: "cdef",
    projectId: "proj1",
    guardrailId: "gr1",
    emailVerified: true,
  };
  const first = await call("/api/admin/allmodels/pool/import", { method: "POST", body: record });
  assert.equal(first.status, 201);
  const second = await call("/api/admin/allmodels/pool/import", { method: "POST", body: record });
  assert.equal(second.status, 201);
  assert.equal(second.body.created, false);
  assert.equal(second.body.gatewayKey.id, first.body.gatewayKey.id);

  const pool = await call("/api/admin/allmodels/pool");
  assert.equal(pool.body.stats.total, 4);
  assert.equal(store.listKeys().filter((key) => key.label.startsWith("allmodels:")).length, 4);
});

await check("health counters mark a failing account and skip it", async () => {
  const pool = await call("/api/admin/allmodels/pool");
  const target = pool.body.accounts[0];
  const marked = await call(`/api/admin/allmodels/pool/${target.id}/health`, {
    method: "POST",
    body: { ok: false, error: "429 too many requests", cooldownMs: 60_000 },
  });
  assert.equal(marked.status, 200);
  assert.equal(marked.body.account.healthy, false);
  assert.equal(marked.body.pool.cooling, 1);

  const health = await call("/health", { token: "" });
  assert.equal(health.status, 200);
  assert.equal(health.body.allmodels.accounts, 4);
});

await check("removing an account drops it from the pool", async () => {
  const pool = await call("/api/admin/allmodels/pool");
  const target = pool.body.accounts.find((account) => account.email === "manual@example.com");
  const removed = await call(`/api/admin/allmodels/pool/${target.id}`, { method: "DELETE" });
  assert.equal(removed.status, 200);
  assert.equal(removed.body.pool.total, 3);
});

await check("status endpoint reports the finished job", async () => {
  const res = await call("/api/admin/allmodels/status");
  assert.equal(res.status, 200);
  assert.equal(res.body.job.status, "done");
  assert.equal(res.body.job.created, 3);
  assert.equal(res.body.toolPresent, true);
});

await check("fingerprint browser engine fills the real form and lands the account", async () => {
  const started = await call("/api/admin/allmodels/register", {
    method: "POST",
    body: {
      count: 1,
      concurrency: 1,
      engine: "browser",
      browserStack: "auto",
      baseUrl: mock.url,
      mailProvider: "fixed",
      email: "ops@example.com",
      captcha: "none",
      verifyEmail: false,
      export: "jsonl",
    },
  });
  assert.equal(started.status, 202, started.text.slice(0, 400));
  assert.equal(started.body.job.engine, "browser");

  const deadline = Date.now() + 240_000;
  let status = started.body;
  while (status.running && Date.now() < deadline) {
    await new Promise((done) => setTimeout(done, 800));
    status = (await call("/api/admin/allmodels/status")).body;
  }
  assert.equal(status.running, false, "browser job did not finish in time");
  const lines = JSON.stringify(status.job?.lines?.slice(-8) ?? []);
  assert.equal(status.job.status, "done", lines.slice(0, 900));
  assert.equal(status.job.created, 1, lines.slice(0, 900));
  assert.equal(status.job.browserStack, "auto");

  const pool = await call("/api/admin/allmodels/pool");
  assert.equal(pool.body.stats.total, 4, "the browser account must be pooled");
  const entry = pool.body.accounts.find((account) => account.engine === "browser");
  assert.ok(entry, "no account with engine=browser in the pool");
  assert.ok(entry.gatewayKey?.prefix?.startsWith("sk-dcg-"));
  assert.equal(entry.emailVerified, true);
});

await check("tool bridge exposes the same registry over HTTP", async () => {
  const listed = await call("/api/admin/allmodels/tools");
  assert.equal(listed.status, 200);
  assert.ok(listed.body.tools.some((tool) => tool.name === "allmodels_register"));
  assert.equal(listed.body.openai[0].type, "function");
  assert.equal(listed.body.openai[0].function.name, listed.body.tools[0].name);

  const imported = await call("/api/admin/allmodels/tools/allmodels_import", {
    method: "POST",
    body: { arguments: { email: "tool@example.com", apiKey: "sk_live_tool_bridge_0123456789" } },
  });
  assert.equal(imported.status, 200, imported.text.slice(0, 400));
  assert.ok(imported.body.result.gatewayKey.prefix.startsWith("sk-dcg-"));

  const pool = await call("/api/admin/allmodels/tools/allmodels_pool", {
    method: "POST",
    body: { arguments: { email: "tool@example.com" } },
  });
  assert.equal(pool.body.result.accounts.length, 1);
  assert.ok(pool.body.result.accounts[0].apiKeyPreview.includes("…"));

  const unknown = await call("/api/admin/allmodels/tools/nope", { method: "POST", body: { arguments: {} } });
  assert.equal(unknown.status, 400);
  assert.match(unknown.text, /unknown_tool|未知工具/);

  // The tool layer can drive a batch itself (push=false: events feed the pool).
  const registered = await call("/api/admin/allmodels/tools/allmodels_register", {
    method: "POST",
    body: {
      arguments: {
        count: 1,
        concurrency: 1,
        engine: "protocol",
        baseUrl: mock.url,
        mailProvider: "fixed",
        email: "ops@example.com",
        captcha: "manual",
        captchaKey: "probe-token",
        verifyEmail: false,
        wait: true,
        timeoutMs: 90_000,
      },
    },
  });
  assert.equal(registered.status, 200, registered.text.slice(0, 600));
  assert.equal(registered.body.result.ok, true, JSON.stringify(registered.body.result).slice(0, 600));
  assert.equal(registered.body.result.job.created, 1);
  assert.equal(registered.body.result.job.status, "done");
});

await check("register validates its options and refuses a missing worker", async () => {
  const realToolDir = gateway.allmodels.toolDir;
  gateway.allmodels.toolDir = join(dataDir, "missing-worker");
  try {
    const res = await call("/api/admin/allmodels/register", {
      method: "POST",
      body: { count: 1, captcha: "none" },
    });
    assert.equal(res.status, 400);
    assert.match(res.text, /找不到注册工具/);
  } finally {
    gateway.allmodels.toolDir = realToolDir;
  }
  const stopped = await call("/api/admin/allmodels/register/stop", { method: "POST" });
  assert.equal(stopped.status, 200);
  assert.equal(stopped.body.stopped, false, "nothing was running");
});

const failed = results.filter((row) => !row.ok);
server.close();
await mock.close();
if (!failed.length) rmSync(dataDir, { recursive: true, force: true });

console.log(`\n${results.length - failed.length}/${results.length} checks passed`);
if (failed.length) {
  console.log(JSON.stringify(failed, null, 2));
  process.exitCode = 1;
}
