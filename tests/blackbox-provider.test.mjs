// Blackbox provider 的结构性单测（不需要网络、不需要 python）。
//
//   node --test tests/blackbox-provider.test.mjs
//
// 守住的是"接进通用 provider 运行时"这件事本身：
//   * 描述符字段（端点 / 模型白名单 / Auth.js 参数 / relay 形态）
//   * 号池文件物理隔离 + schema + 去重 + 网关 Key 只发一次
//   * 环境变量覆盖端点
//   * 运行时的 list/stats/requireProvider 能看到 blackbox 且不串号
import assert from "node:assert/strict";
import { existsSync, mkdtempSync, readFileSync, rmSync } from "node:fs";
import { tmpdir } from "node:os";
import { join } from "node:path";
import { test } from "node:test";

import { PROVIDERS, getProvider, providerSummary, resolveEndpoints } from "../src/provider-catalog.mjs";
import { ProviderPool, importAccount, maskAccount, normalizeAccount } from "../src/provider-pool.mjs";
import { createProviderRuntime } from "../src/providers.mjs";
import { Store } from "../src/store.mjs";

const tempdir = () => mkdtempSync(join(tmpdir(), "bbx-provider-"));

test("catalog descriptor carries the verified blackbox facts", () => {
  const provider = getProvider("blackbox");
  assert.equal(provider.id, "blackbox");
  assert.equal(provider.panelBase, "https://app.blackbox.ai");
  assert.equal(provider.apiBase, "https://enterprise.blackbox.ai");
  assert.equal(provider.openAiCompatible, true);
  assert.equal(provider.supportsEmailSignup, false);
  assert.equal(provider.supportsOAuthOnly, true);
  assert.equal(provider.auth.kind, "authjs");
  assert.match(provider.auth.googleClientId, /^\d+-[a-z0-9]+\.apps\.googleusercontent\.com$/);
  assert.equal(provider.auth.sessionCookie, "__Secure-authjs.session-token");
  assert.equal(provider.relay.kind, "openai");
  assert.ok(provider.models.includes("blackboxai/blackbox-pro"));
  assert.ok(provider.panelApi.keyCandidates.length >= 5);
  assert.equal(provider.cli.npm, "@blackbox_ai/blackbox-cli");
  // 默认文件命名约定必须按 id 派生（保证不撞别的 provider）
  assert.equal(provider.poolFile, "blackbox-pool.json");
  assert.equal(provider.lockFile, "blackbox-register.lock");
  assert.equal(provider.workerModule, "blackbox_reg");
  assert.equal(provider.envPrefix, "BLACKBOX");
});

test("endpoints are overridable per provider via env without leaking to others", () => {
  process.env.BLACKBOX_API_BASE = "http://127.0.0.1:9999";
  process.env.BLACKBOX_PANEL_BASE = "http://127.0.0.1:9998";
  try {
    const blackbox = resolveEndpoints(getProvider("blackbox"));
    assert.equal(blackbox.apiBase, "http://127.0.0.1:9999");
    assert.equal(blackbox.panelBase, "http://127.0.0.1:9998");
    const novita = resolveEndpoints(getProvider("novita"));
    assert.equal(novita.apiBase, "https://api.novita.ai/openai");
  } finally {
    delete process.env.BLACKBOX_API_BASE;
    delete process.env.BLACKBOX_PANEL_BASE;
  }
});

test("providerSummary exposes auth/cli/console hints for the admin UI", () => {
  const summary = providerSummary(getProvider("blackbox"));
  assert.equal(summary.auth.kind, "authjs");
  assert.equal(summary.cli.npm, "@blackbox_ai/blackbox-cli");
  assert.equal(summary.consoleLogin.turnstile, true);
  assert.ok(summary.models.length >= 5);
});

test("pool file is isolated, schema-stamped and idempotent", () => {
  const dir = tempdir();
  try {
    const store = Store.at(dir);
    const pool = ProviderPool.at(dir, getProvider("blackbox"));
    const record = {
      email: "Pool@Own.Example",
      apiKey: "sk-pool-aaaaaaaaaaaaaaaaaaaa",
      plan: "pro",
      userId: "u-1",
      engine: "protocol",
      cookies: [{ name: "__Secure-authjs.session-token", value: "x", domain: "app.blackbox.ai", path: "/" }],
    };

    const first = importAccount({ pool, store, record, mintKey: true });
    assert.equal(first.created, true);
    assert.ok(first.gatewayKey.plaintext.startsWith("sk-dcg-"));

    // 同一 apiKey 重推：不新建、不重发 Key
    const second = importAccount({ pool, store, record: { ...record, plan: "free" }, mintKey: true });
    assert.equal(second.created, false);
    assert.equal(second.account.gatewayKey.id, first.gatewayKey.id);
    assert.equal(pool.size, 1);
    assert.equal(second.account.plan, "free");

    const file = join(dir, "blackbox-pool.json");
    assert.ok(existsSync(file));
    const parsed = JSON.parse(readFileSync(file, "utf8"));
    assert.equal(parsed.schema, "dcg.pool/blackbox@1");
    assert.equal(parsed.provider, "blackbox");
    assert.equal(parsed.accounts.length, 1);
    assert.equal(parsed.accounts[0].email, "pool@own.example");
    assert.ok(parsed.accounts[0].gatewayKey.plaintext.startsWith("sk-dcg-"));

    // 别的 provider 的号池文件不会出现
    assert.equal(existsSync(join(dir, "allmodels-pool.json")), false);
    assert.equal(existsSync(join(dir, "novita-pool.json")), false);
  } finally {
    rmSync(dir, { recursive: true, force: true });
  }
});

test("normalizeAccount keeps blackbox-specific fields and masks secrets", () => {
  const normalized = normalizeAccount({
    email: "A@B.C",
    api_key: "sk-normalized-000000000000",
    plan: "pro",
    user_id: "u-9",
    key_source: "created",
    cookies: [{ name: "k", value: "v" }],
  });
  assert.equal(normalized.email, "a@b.c");
  assert.equal(normalized.apiKey, "sk-normalized-000000000000");

  const masked = maskAccount({ ...normalized, gatewayKey: { id: "g1", prefix: "sk-dcg-AAAA", label: "l" } });
  assert.equal(masked.apiKeyPreview.includes("…"), true);
  assert.equal(masked.gatewayKey.prefix, "sk-dcg-AAAA");
  assert.equal(masked.apiKey, undefined);
});

test("runtime mounts blackbox next to the other providers without collisions", () => {
  const dir = tempdir();
  try {
    const store = Store.at(dir);
    const runtime = createProviderRuntime({
      dataDir: dir,
      port: 0,
      token: "t",
      store,
      enabled: ["blackbox"],
      endpointOverrides: { blackbox: { apiBase: "http://127.0.0.1:1234", panelBase: "http://127.0.0.1:1235" } },
    });
    const rows = runtime.list();
    assert.equal(rows.length, 1);
    assert.equal(rows[0].id, "blackbox");
    assert.equal(rows[0].apiBase, "http://127.0.0.1:1234");
    assert.deepEqual(runtime.stats().blackbox, { accounts: 0, healthy: 0, keys: 0, running: false });
    assert.equal(runtime.requireProvider("blackbox").provider.id, "blackbox");
    assert.throws(() => runtime.requireProvider("nope"), /未知 provider/);
    const ids = PROVIDERS.map((p) => p.id);
    assert.equal(new Set(ids).size, ids.length, "provider id 必须唯一");
  } finally {
    rmSync(dir, { recursive: true, force: true });
  }
});
