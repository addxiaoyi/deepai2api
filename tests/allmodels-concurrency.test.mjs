// 多进程安全：网关 / MCP server / CLI 同时持有同一份 state 时不互相覆盖。
import { test } from "node:test";
import assert from "node:assert/strict";
import { spawn } from "node:child_process";
import { mkdtempSync, readFileSync, rmSync, writeFileSync } from "node:fs";
import { tmpdir } from "node:os";
import { dirname, join, resolve } from "node:path";
import { pathToFileURL } from "node:url";
import { fileURLToPath } from "node:url";

import { Store, mergeStoreState } from "../src/store.mjs";
import { AllmodelsPool, mergePoolAccounts } from "../src/allmodels-pool.mjs";
import { AllmodelsRegistrations } from "../src/allmodels-register.mjs";
import { isProcessAlive, lockPathFor, withFileLock } from "../src/lock.mjs";

const repoRoot = resolve(dirname(fileURLToPath(import.meta.url)), "..");

const slack = (ms = 5) => Atomics.wait(new Int32Array(new SharedArrayBuffer(4)), 0, 0, ms);

const mint = (store, label) =>
  store.addKey({ label, hash: `hash-${label}`, prefix: `sk-dcg-${label}`, limits: {} });

test("two Stores juggling the same file keep each other's keys", () => {
  const dir = mkdtempSync(join(tmpdir(), "dcg-merge-"));
  const path = join(dir, "gateway-state.json");
  const a = new Store(path);
  const b = new Store(path);

  mint(a, "a1");
  slack();
  mint(b, "b1");
  slack();
  mint(a, "a2");

  const disk = JSON.parse(readFileSync(path, "utf8"));
  const labels = disk.keys.map((key) => key.label).sort();
  assert.deepEqual(labels, ["a1", "a2", "b1"]);
  // `a` also sees b's key after its own merge.
  assert.ok(a.listKeys().some((key) => key.label === "b1"));
  rmSync(dir, { recursive: true, force: true });
});

test("an account linked in one process survives the other's flush", () => {
  const dir = mkdtempSync(join(tmpdir(), "dcg-merge-"));
  const path = join(dir, "gateway-state.json");
  const gateway = new Store(path);
  const mcp = new Store(path);

  gateway.upsertAccount({ apiKey: "deepai-key-1", username: "linked", isPro: false });
  slack();
  mint(mcp, "mcp-key");

  const disk = JSON.parse(readFileSync(path, "utf8"));
  assert.equal(disk.accounts.length, 1);
  assert.equal(disk.keys.length, 1);
  assert.equal(disk.accounts[0].apiKey, "deepai-key-1");
  rmSync(dir, { recursive: true, force: true });
});

test("a revocation in one process is not undone by a stale writer", () => {
  const dir = mkdtempSync(join(tmpdir(), "dcg-merge-"));
  const path = join(dir, "gateway-state.json");
  const a = new Store(path);
  const b = new Store(path);

  const key = mint(a, "shared");
  slack();
  a.revokeKey(key.id);
  slack();
  mint(b, "late");

  const disk = JSON.parse(readFileSync(path, "utf8"));
  const shared = disk.keys.find((row) => row.id === key.id);
  assert.equal(shared.revoked, true);
  assert.ok(disk.keys.some((row) => row.label === "late"));
  rmSync(dir, { recursive: true, force: true });
});

test("deletion in one process is not resurrected by a stale writer", () => {
  const dir = mkdtempSync(join(tmpdir(), "dcg-merge-"));
  const path = join(dir, "gateway-state.json");
  const a = new Store(path);
  a.upsertAccount({ apiKey: "doomed-key", username: "doomed" });
  const id = a.accounts[0].id;
  const b = new Store(path);
  assert.equal(b.accounts.length, 1, "b loaded the account before deletion");

  a.removeAccount(id);
  slack();
  mint(b, "noise");

  const disk = JSON.parse(readFileSync(path, "utf8"));
  assert.equal(disk.accounts.length, 0, "deleted account must stay deleted");
  rmSync(dir, { recursive: true, force: true });
});

test("mergeStoreState unions counters and keeps revoked sticky", () => {
  const local = {
    version: 2,
    accounts: [{ id: "a", apiKey: "k", requests: 7, promptTokens: 3, healthy: true, lastUsedAt: "2026-01-02T00:00:00Z" }],
    keys: [{ id: "k1", label: "x", revoked: false, requests: 2 }],
    sessions: { s1: { createdAt: "2026-01-01T00:00:00Z" } },
  };
  const disk = {
    version: 2,
    accounts: [{ id: "a", apiKey: "k", requests: 5, promptTokens: 9, healthy: false, lastUsedAt: "2026-01-01T00:00:00Z" }],
    keys: [{ id: "k1", label: "x", revoked: true, requests: 4 }],
    sessions: { s2: { createdAt: "2026-01-02T00:00:00Z" } },
  };
  const merged = mergeStoreState(local, disk);
  assert.equal(merged.accounts[0].requests, 7);
  assert.equal(merged.accounts[0].promptTokens, 9);
  assert.equal(merged.accounts[0].healthy, true, "newer lastUsedAt wins the health flag");
  assert.equal(merged.keys[0].revoked, true);
  assert.equal(merged.keys[0].requests, 4);
  assert.deepEqual(Object.keys(merged.sessions).sort(), ["s1", "s2"]);
});

test("two pools importing different accounts end up with both", () => {
  const dir = mkdtempSync(join(tmpdir(), "am-merge-"));
  const path = join(dir, "allmodels-pool.json");
  const gateway = new AllmodelsPool(path);
  const mcp = new AllmodelsPool(path);

  gateway.add({ email: "gate@example.com", apiKey: "sk_live_gate_0123456789", gatewayKey: { id: "g1", prefix: "sk-dcg-A", plaintext: "sk-dcg-A" } });
  slack();
  mcp.add({ email: "mcp@example.com", apiKey: "sk_live_mcp_0123456789" });
  slack();
  gateway.add({ email: "gate2@example.com", apiKey: "sk_live_gate2_0123456789" });

  const disk = JSON.parse(readFileSync(path, "utf8"));
  const emails = disk.accounts.map((account) => account.email).sort();
  assert.deepEqual(emails, ["gate2@example.com", "gate@example.com", "mcp@example.com"]);
  const gate = disk.accounts.find((account) => account.email === "gate@example.com");
  assert.equal(gate.gatewayKey.id, "g1", "the issued gateway key survives another writer");
  rmSync(dir, { recursive: true, force: true });
});

test("the same address created concurrently collapses into one entry", () => {
  const merged = mergePoolAccounts(
    [{ id: "a", email: "dup@example.com", apiKey: "sk_live_1", requests: 3, updatedAt: "2026-01-02T00:00:00Z" }],
    [{ id: "b", email: "dup@example.com", apiKey: "sk_live_1", requests: 1, updatedAt: "2026-01-01T00:00:00Z" }],
  );
  assert.equal(merged.length, 1);
  assert.equal(merged[0].requests, 3);
});

test("pool deletion survives a stale writer", () => {
  const dir = mkdtempSync(join(tmpdir(), "am-merge-"));
  const path = join(dir, "allmodels-pool.json");
  const a = new AllmodelsPool(path);
  const { account } = a.add({ email: "gone@example.com", apiKey: "sk_live_gone_0123456789" });
  const b = new AllmodelsPool(path);

  a.remove(account.id);
  slack();
  b.add({ email: "kept@example.com", apiKey: "sk_live_kept_0123456789" });

  const disk = JSON.parse(readFileSync(path, "utf8"));
  assert.deepEqual(disk.accounts.map((row) => row.email), ["kept@example.com"]);
  rmSync(dir, { recursive: true, force: true });
});

test("the job lock blocks a second registrant and reclaims a dead one", (t) => {
  const dir = mkdtempSync(join(tmpdir(), "am-lock-"));
  // Point the controller at a non-existent worker so the stale-lock path returns
  // before it could ever spawn python (this test must stay offline).
  const previousWorker = process.env.ALLMODELS_REGISTER_DIR;
  process.env.ALLMODELS_REGISTER_DIR = join(dir, "no-worker");
  t.after(() => {
    if (previousWorker === undefined) delete process.env.ALLMODELS_REGISTER_DIR;
    else process.env.ALLMODELS_REGISTER_DIR = previousWorker;
    rmSync(dir, { recursive: true, force: true });
  });

  const make = () =>
    new AllmodelsRegistrations({
      dataDir: dir,
      port: 0,
      token: "t",
      pool: new AllmodelsPool(join(dir, "allmodels-pool.json")),
      store: new Store(join(dir, "gateway-state.json")),
      log: () => {},
    });

  const first = make();
  writeFileSync(join(dir, "allmodels-register.lock"), JSON.stringify({ pid: process.pid, at: Date.now() }));
  assert.throws(() => first.start({ count: 1 }), (error) => error.status === 409);

  writeFileSync(join(dir, "allmodels-register.lock"), JSON.stringify({ pid: 999_999, at: Date.now() }));
  assert.throws(() => first.start({ count: 1 }), (error) => error.status === 400);
});

test("withFileLock serialises writers and reclaims stale locks", () => {
  const dir = mkdtempSync(join(tmpdir(), "dcg-lock-"));
  const target = join(dir, "state.json");
  writeFileSync(target, "{}");

  const outer = withFileLock(target, () => "outer", { timeoutMs: 100 });
  assert.equal(outer.ok, true);
  assert.equal(outer.value, "outer");

  writeFileSync(lockPathFor(target), JSON.stringify({ pid: 999_999, at: 0 }));
  const reclaimed = withFileLock(target, () => "reclaimed", { timeoutMs: 1_000 });
  assert.equal(reclaimed.ok, true);
  assert.equal(reclaimed.value, "reclaimed");

  writeFileSync(lockPathFor(target), JSON.stringify({ pid: process.pid, at: Date.now() }));
  const busy = withFileLock(target, () => "never", { timeoutMs: 80 });
  assert.equal(busy.ok, false);
  assert.equal(busy.reason, "busy");
  rmSync(dir, { recursive: true, force: true });
});

test("isProcessAlive is sane for self and nonsense pids", () => {
  assert.equal(isProcessAlive(process.pid), true);
  assert.equal(isProcessAlive(0), false);
  assert.equal(isProcessAlive(999_999), false);
});

test("a key minted by another process is usable after a reload", () => {
  const dir = mkdtempSync(join(tmpdir(), "dcg-reload-"));
  const path = join(dir, "gateway-state.json");
  const gateway = new Store(path);
  const mcp = new Store(path);

  const key = mint(mcp, "minted-elsewhere");
  const hash = readFileSync(path, "utf8");
  assert.match(hash, /minted-elsewhere/);

  assert.equal(gateway.findByHash("hash-minted-elsewhere"), null, "not visible before reload");
  assert.equal(gateway.reloadFromDisk(), true);
  assert.equal(gateway.findByHash("hash-minted-elsewhere")?.id, key.id);
  rmSync(dir, { recursive: true, force: true });
});

test("pool.reload() surfaces another process's import", () => {
  const dir = mkdtempSync(join(tmpdir(), "am-reload-"));
  const path = join(dir, "allmodels-pool.json");
  const gateway = new AllmodelsPool(path);
  const mcp = new AllmodelsPool(path);

  mcp.add({ email: "fresh@example.com", apiKey: "sk_live_fresh_0123456789" });
  assert.equal(gateway.byEmail("fresh@example.com"), null);
  gateway.reload();
  assert.equal(gateway.byEmail("fresh@example.com")?.apiKey, "sk_live_fresh_0123456789");
  rmSync(dir, { recursive: true, force: true });
});

test("two OS processes adding accounts concurrently keep all of them", async () => {
  const dir = mkdtempSync(join(tmpdir(), "am-proc-"));
  const path = join(dir, "allmodels-pool.json");
  const moduleUrl = pathToFileURL(join(repoRoot, "src", "allmodels-pool.mjs")).href;

  const script = (tag) => `
import { AllmodelsPool } from ${JSON.stringify(moduleUrl)};
const pool = new AllmodelsPool(${JSON.stringify(path)});
for (let i = 0; i < 10; i++) {
  pool.add({ email: \`${tag}\${i}@example.com\`, apiKey: \`sk_live_${tag}\${i}_0123456789\` });
}
`;
  const run = (tag) =>
    new Promise((done, fail) => {
      const child = spawn(process.execPath, ["--input-type=module", "-e", script(tag)], {
        cwd: repoRoot,
        stdio: ["ignore", "ignore", "pipe"],
      });
      let stderr = "";
      child.stderr.on("data", (chunk) => (stderr += chunk));
      child.on("close", (code) => (code === 0 ? done() : fail(new Error(stderr.slice(-600)))));
    });

  await Promise.all([run("a"), run("b")]);

  const disk = JSON.parse(readFileSync(path, "utf8"));
  assert.equal(disk.accounts.length, 20, "no writes lost between processes");
  const emails = new Set(disk.accounts.map((account) => account.email));
  assert.equal(emails.size, 20);
  rmSync(dir, { recursive: true, force: true });
});
