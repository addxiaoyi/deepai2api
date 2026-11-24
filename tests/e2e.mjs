// End-to-end test of the whole product against a mock lane:
// admin login -> OAuth callback -> account linked -> key minted -> chat via that
// key -> key revoked. No browser, no real DeepAI account.
import assert from "node:assert/strict";
import { mkdtempSync, rmSync } from "node:fs";
import { tmpdir } from "node:os";
import { join } from "node:path";

import { loadConfig } from "../src/config.mjs";
import { makeMachineHeader } from "../src/machine.mjs";
import { makeDispatcher } from "../src/dispatcher.mjs";
import { makeLane } from "../src/lane.mjs";
import { Store } from "../src/store.mjs";
import { createGateway } from "../src/server.mjs";
import { makeScheduler } from "../src/scheduler.mjs";
import { startMockUpstream } from "./mock-upstream.mjs";

const adminToken = "test-admin-token";
const dataDir = mkdtempSync(join(tmpdir(), "dcg-test-"));
const mock = await startMockUpstream();

const config = {
  ...loadConfig([]),
  port: 0,
  host: "127.0.0.1",
  token: adminToken,
  dataDir,
  apiOrigin: mock.origin,
  signInOrigin: mock.origin,
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
const { server } = createGateway({ config, lane, store, scheduler });
await new Promise((resolve) => server.listen(0, "127.0.0.1", resolve));
const base = `http://127.0.0.1:${server.address().port}`;

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

/** Small cookie-aware client. */
let cookie = "";
const call = async (path, { method = "GET", body, token, headers = {} } = {}) => {
  const res = await fetch(base + path, {
    method,
    headers: {
      ...(body ? { "Content-Type": "application/json" } : {}),
      ...(token ? { Authorization: `Bearer ${token}` } : {}),
      ...(cookie ? { Cookie: cookie } : {}),
      ...headers,
    },
    body: body ? JSON.stringify(body) : undefined,
    redirect: "manual",
  });
  const setCookie = res.headers.get("set-cookie");
  if (setCookie) cookie = setCookie.split(";")[0];
  const text = await res.text();
  let json = null;
  try {
    json = text ? JSON.parse(text) : null;
  } catch {
    json = null;
  }
  return { status: res.status, body: json, text, headers: res.headers };
};

await check("gateway serves an operator landing page, not a UI", async () => {
  const res = await fetch(`${base}/`);
  assert.equal(res.status, 200);
  const text = await res.text();
  assert.match(text, /DeepAI Code Gateway/);
  assert.match(text, /\/v1/);
});

await check("gateway redirects / to Open WebUI when configured", async () => {
  const redirected = createGateway({
    config: { ...config, webuiUrl: "http://127.0.0.1:3000" },
    lane,
    store,
    scheduler,
  });
  await new Promise((resolve) => redirected.server.listen(0, "127.0.0.1", resolve));
  const port = redirected.server.address().port;
  const res = await fetch(`http://127.0.0.1:${port}/`, { redirect: "manual" });
  assert.equal(res.status, 302);
  assert.equal(res.headers.get("location"), "http://127.0.0.1:3000");
  redirected.server.close();
});

await check("admin API rejects an anonymous caller", async () => {
  const res = await call("/api/admin/state");
  assert.equal(res.status, 401);
});

await check("admin login with the wrong password fails", async () => {
  const res = await call("/api/admin/login", { method: "POST", body: { token: "nope" } });
  assert.equal(res.status, 401);
});

await check("admin login sets a session cookie", async () => {
  const res = await call("/api/admin/login", { method: "POST", body: { token: adminToken } });
  assert.equal(res.status, 200);
  assert.match(cookie, /^dcg_admin=/);
});

await check("sign-in start returns an authorize URL with PKCE", async () => {
  const res = await call("/api/admin/login-start", { method: "POST" });
  assert.equal(res.status, 200);
  const url = new URL(res.body.url);
  assert.equal(url.pathname, "/deepai_code/desktop_auth/");
  assert.equal(url.searchParams.get("code_challenge_method"), "S256");
  assert.match(url.searchParams.get("state"), /^[0-9a-f]{64}$/);
  assert.match(url.searchParams.get("code_challenge"), /^[A-Za-z0-9_-]{43}$/);
  globalThis.__state = url.searchParams.get("state");
});

await check("callback rejects a forged state", async () => {
  const res = await fetch(`${base}/auth-callback?code=test-code&state=deadbeef`);
  assert.equal(res.status, 400);
});

await check("OAuth callback exchanges the code and links the account", async () => {
  const res = await fetch(`${base}/auth-callback?code=test-code&state=${globalThis.__state}`);
  assert.equal(res.status, 200);
  const text = await res.text();
  assert.match(text, /登录成功/);
  assert.equal(mock.seen.exchanges, 1);
  assert.equal(store.account?.apiKey, "mock-account-api-key");
  assert.equal(store.account?.username, "mock-user");
});

await check("admin state shows the linked account and catalog", async () => {
  const res = await call("/api/admin/state");
  assert.equal(res.status, 200);
  assert.equal(res.body.account.username, "mock-user");
  assert.ok(res.body.models.length >= 3);
  assert.equal(res.body.usage.balance, "42.00000");
});

await check("model payload keeps the OpenAI snake_case contract the UI reads", async () => {
  const res = await call("/api/admin/state");
  const model = res.body.models.find((m) => m.id === "glm-5.3");
  // The admin page renders these exact keys; a rename silently blanks the table.
  assert.equal(model.context_window, 320000);
  assert.equal(model.max_tokens, 8192);
  assert.equal(model.theos_per_request, "3");
  assert.equal(model.reasoning, true);
  // and a free model must not look metered
  const free = res.body.models.find((m) => m.id === "deepai-cyber");
  assert.equal(free.theos_per_request, "0");
});

await check("minting a key needs admin auth", async () => {
  const saved = cookie;
  cookie = "";
  const res = await call("/api/admin/keys", { method: "POST", body: { label: "x" } });
  assert.equal(res.status, 401);
  cookie = saved;
});

let minted = null;
await check("mint an API key", async () => {
  const res = await call("/api/admin/keys", { method: "POST", body: { label: "openwebui" } });
  assert.equal(res.status, 201);
  assert.match(res.body.key, /^sk-dcg-[A-Za-z0-9_-]{40,}$/);
  assert.equal(res.body.record.label, "openwebui");
  minted = res.body.key;
  assert.ok(!JSON.stringify(store.state).includes(minted), "plaintext key must not be persisted");
});

await check("/v1 rejects a bogus key", async () => {
  const res = await call("/v1/models", { token: "sk-dcg-bogusbogusbogusbogusbogusbogusbogus" });
  assert.equal(res.status, 401);
});

await check("/v1/models works with the minted key", async () => {
  const res = await call("/v1/models", { token: minted });
  assert.equal(res.status, 200);
  assert.equal(res.body.object, "list");
  assert.ok(res.body.data.some((m) => m.id === "deepai-cyber"));
});

await check("chat completion through the minted key", async () => {
  const res = await call("/v1/chat/completions", {
    method: "POST",
    token: minted,
    body: { model: "deepai-cyber", messages: [{ role: "user", content: "hi" }] },
  });
  assert.equal(res.status, 200);
  assert.equal(res.body.choices[0].message.content, "echo:deepai-cyber");
});

await check("streaming completion through the minted key", async () => {
  const res = await fetch(`${base}/v1/chat/completions`, {
    method: "POST",
    headers: { Authorization: `Bearer ${minted}`, "Content-Type": "application/json" },
    body: JSON.stringify({ model: "deepseek-v4.1-flash", messages: [{ role: "user", content: "hi" }], stream: true }),
  });
  assert.equal(res.status, 200);
  assert.match(res.headers.get("content-type") ?? "", /text\/event-stream/);
  const text = await res.text();
  assert.match(text, /echo:deepseek-v4\.1-flash/);
  assert.match(text, /\[DONE\]/);
});

await check("key usage is counted for model calls, not for catalog reads", async () => {
  const res = await call("/api/admin/state");
  const record = res.body.keys.find((k) => k.label === "openwebui");
  // non-stream chat + stream chat; listing models is not billed
  assert.equal(record.requests, 2);
  assert.ok(record.lastUsedAt);
});

await check("Anthropic /v1/messages (non-streaming)", async () => {
  const res = await call("/v1/messages", {
    method: "POST",
    token: minted,
    body: {
      model: "deepai-cyber",
      max_tokens: 64,
      system: "be terse",
      messages: [{ role: "user", content: [{ type: "text", text: "hi" }] }],
    },
  });
  assert.equal(res.status, 200);
  assert.equal(res.body.type, "message");
  assert.equal(res.body.role, "assistant");
  assert.equal(res.body.content[0].type, "text");
  assert.equal(res.body.content[0].text, "echo:deepai-cyber");
  assert.equal(res.body.stop_reason, "end_turn");
  assert.ok(res.body.usage.input_tokens >= 0);
});

await check("Anthropic /v1/messages (streaming event sequence)", async () => {
  const res = await fetch(`${base}/v1/messages`, {
    method: "POST",
    headers: { Authorization: `Bearer ${minted}`, "Content-Type": "application/json" },
    body: JSON.stringify({
      model: "deepseek-v4.1-flash",
      max_tokens: 64,
      stream: true,
      messages: [{ role: "user", content: "hi" }],
    }),
  });
  assert.equal(res.status, 200);
  assert.match(res.headers.get("content-type") ?? "", /text\/event-stream/);
  const text = await res.text();
  assert.match(text, /event: message_start/);
  assert.match(text, /event: content_block_delta/);
  assert.match(text, /text_delta/);
  assert.match(text, /event: message_stop/);
});

await check("billing view reports the key's usage", async () => {
  const res = await call("/v1/billing", { token: minted });
  assert.equal(res.status, 200);
  assert.equal(res.body.object, "billing");
  assert.ok(res.body.usage.requests >= 3);
  assert.equal(res.body.key.label, "openwebui");
});

await check("per-key rate limit is enforced", async () => {
  const created = await call("/api/admin/keys", { method: "POST", body: { label: "limited", limits: { rpm: 2 } } });
  assert.equal(created.status, 201);
  const limitedKey = created.body.key;
  const one = await call("/v1/chat/completions", { method: "POST", token: limitedKey, body: { model: "deepai-cyber", messages: [{ role: "user", content: "a" }] } });
  assert.equal(one.status, 200);
  const two = await call("/v1/chat/completions", { method: "POST", token: limitedKey, body: { model: "deepai-cyber", messages: [{ role: "user", content: "b" }] } });
  assert.equal(two.status, 200);
  const three = await call("/v1/chat/completions", { method: "POST", token: limitedKey, body: { model: "deepai-cyber", messages: [{ role: "user", content: "c" }] } });
  assert.equal(three.status, 429);
  assert.equal(three.body.error.code, "rate_limit");
});

await check("sticky sessions keep a key on the same account", async () => {
  const created = await call("/api/admin/keys", { method: "POST", body: { label: "sticky" } });
  const stickyKey = created.body.key;
  const first = await call("/v1/chat/completions", {
    method: "POST",
    token: stickyKey,
    body: { model: "deepai-cyber", messages: [{ role: "user", content: "one" }] },
  });
  const second = await call("/v1/chat/completions", {
    method: "POST",
    token: stickyKey,
    body: { model: "deepai-cyber", messages: [{ role: "user", content: "two" }] },
  });
  assert.equal(first.status, 200);
  assert.equal(second.status, 200);
  assert.equal(first.headers.get("x-gateway-account"), second.headers.get("x-gateway-account"));
});

await check("a broken account fails over to a healthy one", async () => {
  // A revoked account goes in front of the good one, and this key has no sticky
  // assignment yet, so the scheduler really does try it first.
  store.upsertAccount({ apiKey: "bad-key", username: "broken", source: "test" });
  const broken = store.accounts.find((a) => a.apiKey === "bad-key");
  store.state.accounts = [broken, ...store.state.accounts.filter((a) => a.id !== broken.id)];
  store.save();

  const created = await call("/api/admin/keys", { method: "POST", body: { label: "failover" } });
  const freshKey = created.body.key;

  const res = await call("/v1/chat/completions", {
    method: "POST",
    token: freshKey,
    body: { model: "deepai-cyber", messages: [{ role: "user", content: "hi" }] },
  });
  assert.equal(res.status, 200);
  assert.equal(res.body.choices[0].message.content, "echo:deepai-cyber");
  assert.notEqual(res.headers.get("x-gateway-account"), broken.id, "must not answer from the broken account");

  const state = await call("/api/admin/state");
  const brokenView = state.body.accounts.find((a) => a.username === "broken");
  assert.equal(brokenView.healthy, false);
  assert.ok(brokenView.cooldownUntil > Date.now());

  // clean up so later checks see one healthy account
  store.removeAccount(broken.id);
});

await check("only broken accounts report all_accounts_failed, not an auth error", async () => {
  store.upsertAccount({ apiKey: "bad-key", username: "broken", source: "test" });
  const good = store.accounts.filter((a) => a.apiKey === "mock-account-api-key");
  const broken = store.accounts.filter((a) => a.apiKey !== "mock-account-api-key");
  store.state.accounts = broken;
  store.save();
  const res = await call("/v1/chat/completions", {
    method: "POST",
    token: minted,
    body: { model: "deepai-cyber", messages: [{ role: "user", content: "hi" }] },
  });
  assert.equal(res.status, 503);
  assert.equal(res.body.error.code, "all_accounts_failed");
  store.state.accounts = [...good, ...broken];
  store.save();
});

await check("unknown model falls back to the configured default", async () => {
  const res = await call("/v1/chat/completions", {
    method: "POST",
    token: minted,
    body: { model: "gpt-4o", messages: [{ role: "user", content: "hi" }] },
  });
  assert.equal(res.status, 200);
  assert.equal(res.headers.get("x-gateway-model-fallback"), "deepseek-v4.1-flash");
});

await check("revoking a key cuts access immediately", async () => {
  const state = await call("/api/admin/state");
  const record = state.body.keys.find((k) => k.label === "openwebui");
  const revoked = await call(`/api/admin/keys/${record.id}/revoke`, { method: "POST" });
  assert.equal(revoked.status, 200);
  const res = await call("/v1/models", { token: minted });
  assert.equal(res.status, 401);
});

await check("/health reports the linked account", async () => {
  const res = await call("/health");
  assert.equal(res.status, 200);
  assert.equal(res.body.account.username, "mock-user");
});

await check("unlinking clears the account", async () => {
  const res = await call("/api/admin/unlink", { method: "POST" });
  assert.equal(res.status, 200);
  assert.equal(res.body.state.account, null);
  const after = await call("/v1/chat/completions", {
    method: "POST",
    token: adminToken,
    body: { model: "deepai-cyber", messages: [{ role: "user", content: "hi" }] },
  });
  assert.equal(after.status, 401);
  assert.equal(after.body.error.code, "no_account");
});

server.close();
await mock.close();
rmSync(dataDir, { recursive: true, force: true });

const failed = results.filter((r) => !r.ok);
console.log(`\n${results.length - failed.length}/${results.length} checks passed`);
process.exit(failed.length === 0 ? 0 : 1);


