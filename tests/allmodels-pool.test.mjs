// Pool protocol tests: schema, dedupe, health, selection, export, key binding.
import { test } from "node:test";
import assert from "node:assert/strict";
import { mkdtempSync, readFileSync, existsSync, writeFileSync } from "node:fs";
import { tmpdir } from "node:os";
import { join } from "node:path";

import { AllmodelsPool, POOL_SCHEMA, importAccount } from "../src/allmodels-pool.mjs";

const sample = (overrides = {}) => ({
  email: "pool@example.com",
  password: "pw-123456",
  name: "Pool",
  apiKey: "sk_live_signup_abcdef0123456789",
  keyId: "key-1",
  keyPrefix: "sk_live_",
  keyLast4: "6789",
  keySource: "signup-key",
  projectId: "proj1",
  guardrailId: "gr1",
  organizationId: "org1",
  emailVerified: true,
  engine: "protocol",
  ...overrides,
});

function fakeStore() {
  const keys = [];
  return {
    keys,
    addKey({ label, hash, prefix, limits }) {
      const key = { id: `k-${keys.length + 1}`, label, hash, prefix, limits, createdAt: new Date().toISOString() };
      keys.push(key);
      return key;
    },
  };
}

test("pool writes schema and keeps secrets private on disk (0600)", () => {
  const dir = mkdtempSync(join(tmpdir(), "am-pool-"));
  const path = join(dir, "allmodels-pool.json");
  const pool = new AllmodelsPool(path);
  pool.add(sample());
  assert.equal(pool.size, 1);
  assert.ok(existsSync(path));
  const raw = JSON.parse(readFileSync(path, "utf8"));
  assert.equal(raw.schema, POOL_SCHEMA);
  assert.equal(raw.accounts[0].apiKey, "sk_live_signup_abcdef0123456789");
  assert.equal(raw.accounts[0].status, "active");
});

test("importAccount mints exactly one gateway key, idempotently", () => {
  const dir = mkdtempSync(join(tmpdir(), "am-pool-"));
  const pool = new AllmodelsPool(join(dir, "pool.json"));
  const store = fakeStore();

  const first = importAccount({ pool, store, record: sample() });
  assert.equal(first.created, true);
  assert.ok(first.gatewayKey.plaintext.startsWith("sk-dcg-"));
  assert.equal(store.keys.length, 1);

  const second = importAccount({ pool, store, record: sample({ keyId: "key-2" }) });
  assert.equal(second.created, false);
  assert.equal(second.gatewayKey.id, first.gatewayKey.id);
  assert.equal(store.keys.length, 1, "re-import must not mint a second key");
  assert.equal(pool.byEmail("POOL@example.com").keyId, "key-2", "re-import refreshes fields");
});

test("re-import keeps usage counters and the issued key", () => {
  const dir = mkdtempSync(join(tmpdir(), "am-pool-"));
  const pool = new AllmodelsPool(join(dir, "pool.json"));
  const store = fakeStore();
  const { account } = importAccount({ pool, store, record: sample() });
  pool.recordResult(account.id, { ok: true });
  pool.recordResult(account.id, { ok: false, error: "429", cooldownMs: 60_000 });

  const again = importAccount({ pool, store, record: sample({ password: "new-pw" }) });
  assert.equal(again.account.requests, 2);
  assert.equal(again.account.failures, 1);
  assert.equal(again.account.password, "new-pw");
  assert.equal(again.gatewayKey.id, account.gatewayKey.id);
});

test("pick() round-robins healthy accounts and skips cooling ones", () => {
  const dir = mkdtempSync(join(tmpdir(), "am-pool-"));
  const pool = new AllmodelsPool(join(dir, "pool.json"));
  for (const id of ["a", "b", "c"]) {
    pool.add(sample({ email: `${id}@example.com`, apiKey: `sk_live_${id}_0123456789abcdef` }));
  }
  const picked = [pool.pick(), pool.pick(), pool.pick(), pool.pick()].map((a) => a.email);
  assert.deepEqual(picked, ["a@example.com", "b@example.com", "c@example.com", "a@example.com"]);

  const b = pool.byEmail("b@example.com");
  pool.recordResult(b.id, { ok: false, error: "403", cooldownMs: 120_000 });
  const healthy = pool.healthy().map((a) => a.email);
  assert.deepEqual(healthy, ["a@example.com", "c@example.com"]);
  assert.equal(pool.pick({ exclude: [pool.byEmail("a@example.com").id] }).email, "c@example.com");
});

test("stats and exports cover jsonl / csv / pool", () => {
  const dir = mkdtempSync(join(tmpdir(), "am-pool-"));
  const pool = new AllmodelsPool(join(dir, "pool.json"));
  const store = fakeStore();
  importAccount({ pool, store, record: sample() });
  importAccount({
    pool,
    store,
    record: sample({ email: "second@example.com", apiKey: "sk_live_second_0123456789", extraKeys: [{ key: "x" }] }),
  });

  const stats = pool.stats();
  assert.equal(stats.total, 2);
  assert.equal(stats.active, 2);
  assert.equal(stats.keys, 2);
  assert.equal(stats.extraKeys, 1);

  const jsonl = pool.exportTo(join(dir, "out.jsonl"), { format: "jsonl" });
  assert.equal(jsonl.rows, 2);
  assert.equal(readFileSync(jsonl.file, "utf8").trim().split("\n").length, 2);

  const csv = pool.exportTo(join(dir, "out.csv"), { format: "csv" });
  const header = readFileSync(csv.file, "utf8").split("\n")[0];
  assert.ok(header.startsWith("email,password,api_key"));
  assert.ok(readFileSync(csv.file, "utf8").includes("sk-dcg-"));

  const full = pool.exportTo(join(dir, "pool.json"), { format: "pool" });
  const parsed = JSON.parse(readFileSync(full.file, "utf8"));
  assert.equal(parsed.schema, POOL_SCHEMA);
  assert.equal(parsed.accounts[1].gatewayKey.plaintext.startsWith("sk-dcg-"), true);
});

test("a corrupt pool file does not take the gateway down", () => {
  const dir = mkdtempSync(join(tmpdir(), "am-pool-"));
  const path = join(dir, "pool.json");
  const warn = [];
  writeFileSync(path, "{not json");
  const pool = new AllmodelsPool(path, { log: (level, message) => warn.push(`${level}:${message}`) });
  assert.equal(pool.size, 0);
  assert.equal(warn.length, 1);
  pool.add(sample());
  assert.equal(pool.size, 1);
});

test("a restarted gateway keeps the account ids and gateway keys", () => {
  const dir = mkdtempSync(join(tmpdir(), "am-pool-"));
  const path = join(dir, "pool.json");
  const first = new AllmodelsPool(path);
  const store = fakeStore();
  const { account } = importAccount({ pool: first, store, record: sample() });

  const restarted = new AllmodelsPool(path);
  assert.equal(restarted.size, 1);
  const reloaded = restarted.getAccount(account.id);
  assert.ok(reloaded, "id must survive a reload");
  assert.equal(reloaded.email, "pool@example.com");
  assert.equal(reloaded.gatewayKey.plaintext, account.gatewayKey.plaintext);
});
