// jevai 号池测试：账号合并、Key 轮询、冷却、导出、网关 Key 绑定。
import { test } from "node:test";
import assert from "node:assert/strict";
import { mkdtempSync, readFileSync, existsSync } from "node:fs";
import { tmpdir } from "node:os";
import { join } from "node:path";

import {
  KEY_PREFIX,
  POOL_SCHEMA,
  JevaiPool,
  importAccount,
  maskAccount,
} from "../src/jevai-pool.mjs";
import { Store } from "../src/store.mjs";

const KEY_A = "sk-glm5-aaaaaaaaaaaaaaaa1111";
const KEY_B = "sk-glm5-bbbbbbbbbbbbbbbb2222";

const sample = (overrides = {}) => ({
  email: "pool@example.com",
  password: "pw-123456",
  name: "Pool",
  apiKey: KEY_A,
  userId: "u-1",
  emailVerified: true,
  credits: { credits: 100 },
  ...overrides,
});

test("pool persists the schema and normalizes the key", () => {
  const dir = mkdtempSync(join(tmpdir(), "jev-pool-"));
  const pool = new JevaiPool(join(dir, "jevai-pool.json"));
  const { account, created } = pool.add(sample());
  assert.equal(created, true);
  assert.equal(account.email, "pool@example.com");
  assert.equal(account.apiKey, KEY_A);
  assert.equal(account.keyPrefix, KEY_PREFIX);
  assert.equal(pool.stats().keys, 1);

  const raw = JSON.parse(readFileSync(pool.path, "utf8"));
  assert.equal(raw.schema, POOL_SCHEMA);
  assert.equal(raw.provider, "jevai");
  assert.ok(raw.apiBase.endsWith("/api/v1"));
});

test("re-import merges by apiKey or email and keeps createdAt", () => {
  const dir = mkdtempSync(join(tmpdir(), "jev-pool-"));
  const pool = new JevaiPool(join(dir, "pool.json"));
  const first = pool.add(sample());
  const createdAt = first.account.createdAt;

  // 同邮箱再来一次：只更新，不新建
  const byEmail = pool.add(sample({ apiKey: KEY_B }));
  assert.equal(byEmail.created, false, "同邮箱视为同一账号");
  assert.equal(pool.size, 1);
  assert.equal(pool.accounts[0].apiKey, KEY_B, "Key 会被更新");
  assert.equal(pool.accounts[0].createdAt, createdAt);

  // 邮箱变了但 Key 没变：同 Key 视为同一账号，邮箱跟着更新
  const byKey = pool.add(sample({ email: "renamed@example.com", apiKey: KEY_B }));
  assert.equal(byKey.created, false, "同 Key 视为同一账号");
  assert.equal(pool.size, 1);
  assert.equal(pool.accounts[0].email, "renamed@example.com");
});

test("healthy/pick rotate and skip cooling or keyless accounts", () => {
  const dir = mkdtempSync(join(tmpdir(), "jev-pool-"));
  const pool = new JevaiPool(join(dir, "pool.json"));
  pool.add(sample());
  pool.add(sample({ email: "b@example.com", apiKey: KEY_B }));
  pool.add(sample({ email: "nokey@example.com", apiKey: "" }));

  assert.equal(pool.healthy().length, 2, "没 Key 的不算健康");
  const a = pool.pick();
  const b = pool.pick();
  assert.notEqual(a.id, b.id);

  pool.recordResult(a.id, { ok: false, error: "HTTP 429", cooldownMs: 60_000 });
  const remaining = pool.healthy().map((x) => x.id);
  assert.ok(!remaining.includes(a.id), "冷却中的不再被选中");
  assert.equal(pool.stats().cooling, 1);
  assert.equal(pool.stats().unhealthy, 1);
});

test("export keys + connection file for other tools", () => {
  const dir = mkdtempSync(join(tmpdir(), "jev-pool-"));
  const pool = new JevaiPool(join(dir, "pool.json"));
  pool.add(sample());
  pool.add(sample({ email: "b@example.com", apiKey: KEY_B }));

  const keys = pool.exportKeys(join(dir, "keys", "jevai.txt"));
  assert.equal(keys.rows, 2);
  assert.deepEqual(readFileSync(keys.file, "utf8").trim().split("\n"), [KEY_A, KEY_B]);

  const connection = pool.exportConnection(join(dir, "keys", "jevai.json"));
  assert.equal(connection.keys, 2);
  const payload = JSON.parse(readFileSync(connection.file, "utf8"));
  assert.equal(payload.provider, "jevai");
  assert.equal(payload.openai_base, "https://jev-ai.org/api/v1");
  assert.equal(payload.key_prefix, KEY_PREFIX);
  assert.deepEqual(payload.keys, [KEY_A, KEY_B]);
  assert.ok(existsSync(connection.file));
});

test("exportTo supports keys / jsonl / csv / pool", () => {
  const dir = mkdtempSync(join(tmpdir(), "jev-pool-"));
  const pool = new JevaiPool(join(dir, "pool.json"));
  pool.add(sample());

  const jsonl = pool.exportTo(join(dir, "a.jsonl"), { format: "jsonl" });
  assert.equal(jsonl.rows, 1);
  assert.equal(JSON.parse(readFileSync(jsonl.file, "utf8").trim()).apiKey, KEY_A);

  const csv = pool.exportTo(join(dir, "a.csv"), { format: "csv" });
  assert.match(readFileSync(csv.file, "utf8").split("\n")[0], /api_key/);

  const keys = pool.exportTo(join(dir, "a.txt"), { format: "keys" });
  assert.equal(readFileSync(keys.file, "utf8").trim(), KEY_A);
});

test("importAccount mints exactly one gateway key", () => {
  const dir = mkdtempSync(join(tmpdir(), "jev-pool-"));
  const pool = new JevaiPool(join(dir, "pool.json"));
  const store = Store.at(dir);

  const first = importAccount({ pool, store, record: sample() });
  assert.equal(first.created, true);
  assert.ok(first.gatewayKey?.plaintext?.startsWith("sk-dcg-"));
  assert.equal(store.listKeys().length, 1);

  const again = importAccount({ pool, store, record: sample() });
  assert.equal(again.created, false);
  assert.equal(store.listKeys().length, 1, "重复导入不会再发一把");
  assert.equal(again.gatewayKey.id, first.gatewayKey.id);
});

test("maskAccount hides the key but keeps a preview", () => {
  const dir = mkdtempSync(join(tmpdir(), "jev-pool-"));
  const pool = new JevaiPool(join(dir, "pool.json"));
  pool.add(sample());
  const masked = maskAccount(pool.accounts[0]);
  assert.ok(!JSON.stringify(masked).includes(KEY_A));
  assert.ok(masked.apiKeyPreview.startsWith("sk-glm5-aa"));
  assert.equal(masked.emailVerified, true);
});
