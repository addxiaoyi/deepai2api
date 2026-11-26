// 六条 OpenAI 兼容线的引擎测试：号池语义、匿名线、导出、网关 Key。
import { test } from "node:test";
import assert from "node:assert/strict";
import { mkdtempSync, readFileSync } from "node:fs";
import { tmpdir } from "node:os";
import { join } from "node:path";

import {
  LinePool,
  defineLine,
  explainModelError,
  importAccount,
  maskAccount,
} from "../src/openai-lines.mjs";
import { LINE as AGNES } from "../src/agnes-pool.mjs";
import { LINE as POLLINATIONS } from "../src/pollinations-pool.mjs";
import { Store } from "../src/store.mjs";

const dir = () => mkdtempSync(join(tmpdir(), "line-pool-"));

test("defineLine fills defaults and freezes the descriptor", () => {
  const line = defineLine({ id: "demo", name: "Demo", baseUrl: "https://x.test", apiBase: "https://x.test/v1" });
  assert.equal(line.schema, "dcg.pool/demo@1");
  assert.equal(line.pyModule, "demo_reg");
  assert.equal(line.toolDir, "tools/demo-reg");
  assert.equal(line.relayPrefix, "/demo/v1/");
  assert.equal(line.keyPrefix, "sk-");
  assert.equal(line.anonymous, false);
  assert.ok(Object.isFrozen(line));
});

test("pool dedupes by apiKey then email and keeps createdAt", () => {
  const pool = new LinePool(AGNES, join(dir(), "agnes-pool.json"));
  const first = pool.add({ email: "a@example.com", apiKey: "sk-aaaaaaaa1111" });
  assert.equal(first.created, true);
  const createdAt = first.account.createdAt;

  const again = pool.add({ email: "a@example.com", apiKey: "sk-bbbbbbbb2222" });
  assert.equal(again.created, false, "同邮箱只更新");
  assert.equal(pool.size, 1);
  assert.equal(pool.accounts[0].apiKey, "sk-bbbbbbbb2222");
  assert.equal(pool.accounts[0].createdAt, createdAt);

  const byKey = pool.add({ email: "other@example.com", apiKey: "sk-bbbbbbbb2222" });
  assert.equal(byKey.created, false, "同 Key 视为同一账号");
});

test("healthy/pick skip cooling and keyless accounts", () => {
  const pool = new LinePool(AGNES, join(dir(), "p.json"));
  pool.add({ email: "a@example.com", apiKey: "sk-aaaaaaaa1111" });
  pool.add({ email: "b@example.com", apiKey: "sk-bbbbbbbb2222" });
  pool.add({ email: "nokey@example.com", apiKey: "" });
  assert.equal(pool.healthy().length, 2, "没 Key 的不接流量");

  const first = pool.pick();
  pool.recordResult(first.id, { ok: false, error: "HTTP 429", cooldownMs: 60_000 });
  assert.ok(!pool.healthy().some((a) => a.id === first.id));
  assert.equal(pool.stats().cooling, 1);
  assert.equal(pool.stats().failures, 1);
});

test("anonymous line accepts keyless accounts", () => {
  assert.equal(POLLINATIONS.anonymous, true);
  const pool = new LinePool(POLLINATIONS, join(dir(), "poll.json"));
  const { account, created } = pool.add({ email: "anon@pollinations", apiKey: "", engine: "anonymous" });
  assert.equal(created, true);
  assert.equal(account.apiKey, "");
  assert.equal(pool.healthy().length, 1, "匿名线不需要 Key 也算健康");
  assert.equal(pool.keys().length, 0);
  assert.equal(pool.stats().anonymous, true);
});

test("exportKeys / exportConnection write what other tools need", () => {
  const root = dir();
  const pool = new LinePool(AGNES, join(root, "agnes-pool.json"));
  pool.add({ email: "a@example.com", apiKey: "sk-aaaaaaaa1111" });
  pool.add({ email: "b@example.com", apiKey: "sk-bbbbbbbb2222" });

  const keys = pool.exportKeys(join(root, "keys", "agnes.txt"));
  assert.equal(keys.rows, 2);
  assert.deepEqual(readFileSync(keys.file, "utf8").trim().split("\n"), ["sk-aaaaaaaa1111", "sk-bbbbbbbb2222"]);

  const connection = pool.exportConnection(join(root, "keys", "agnes.json"));
  const payload = JSON.parse(readFileSync(connection.file, "utf8"));
  assert.equal(payload.provider, "agnes");
  assert.equal(payload.openai_base, "https://api.agnes-ai.cn/v1");
  assert.equal(payload.key_prefix, "sk-");
  assert.equal(payload.keys.length, 2);
});

test("importAccount mints exactly one gateway key", () => {
  const root = dir();
  const pool = new LinePool(AGNES, join(root, "agnes-pool.json"));
  const store = Store.at(root);
  const first = importAccount({ line: AGNES, pool, store, record: { email: "a@example.com", apiKey: "sk-aaaaaaaa1111" } });
  assert.ok(first.gatewayKey?.plaintext?.startsWith("sk-dcg-"));
  assert.equal(store.listKeys().length, 1);
  const again = importAccount({ line: AGNES, pool, store, record: { email: "a@example.com", apiKey: "sk-aaaaaaaa1111" } });
  assert.equal(store.listKeys().length, 1, "重复导入不再发 Key");
  assert.equal(again.gatewayKey.id, first.gatewayKey.id);
});

test("maskAccount hides the upstream key", () => {
  const pool = new LinePool(AGNES, join(dir(), "p.json"));
  pool.add({ email: "a@example.com", apiKey: "sk-aaaaaaaa1111" });
  const masked = maskAccount(pool.accounts[0]);
  assert.ok(!JSON.stringify(masked).includes("sk-aaaaaaaa1111"));
  assert.ok(masked.apiKeyPreview.startsWith("sk-aaaaaaa"));
});

test("explainModelError only fires for model-shaped 4xx", () => {
  assert.equal(explainModelError(AGNES, 500, "boom"), null);
  assert.equal(explainModelError(AGNES, 404, "not found"), null);
  const explained = explainModelError(AGNES, 404, '{"error":"model not found"}');
  assert.equal(explained.error.code, "model_not_found");
  assert.match(explained.error.message, /models/);

  const withSuffix = explainModelError(defineLine({ id: "x", name: "X", baseUrl: "https://x", apiBase: "https://x/v1", freeSuffix: ":free" }), 400, "unknown model");
  assert.match(withSuffix.error.message, /:free/);
});
