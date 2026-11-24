// Real-upstream smoke test: seeds a temporary gateway from the desktop app's
// stored login and drives it the way a downstream tool would.
// Requires a signed-in DeepAI Code desktop app (or DEEPAI_API_KEY).
import assert from "node:assert/strict";
import { mkdtempSync, rmSync } from "node:fs";
import { tmpdir } from "node:os";
import { join } from "node:path";

import { loadConfig } from "../src/config.mjs";
import { readStoredAccount } from "../src/credentials.mjs";
import { makeMachineHeader } from "../src/machine.mjs";
import { makeDispatcher } from "../src/dispatcher.mjs";
import { makeLane } from "../src/lane.mjs";
import { Store } from "../src/store.mjs";
import { createGateway } from "../src/server.mjs";
import { makeScheduler } from "../src/scheduler.mjs";

const dataDir = mkdtempSync(join(tmpdir(), "dcg-smoke-"));
const account = readStoredAccount(loadConfig([]).secretsDir);
const adminToken = "smoke-admin";

const config = { ...loadConfig([]), port: 0, host: "127.0.0.1", token: adminToken, dataDir, log: "warn" };
const store = Store.at(dataDir);
store.setAccount({ apiKey: account.apiKey, username: account.username, isPro: account.isPro });

const { dispatcher } = makeDispatcher();
const lane = makeLane({ config, getAccount: () => store.account, machineHeader: makeMachineHeader(config.machineRaw), dispatcher });
const scheduler = makeScheduler({ store });
const { server } = createGateway({ config, lane, store, scheduler });
await new Promise((resolve) => server.listen(0, "127.0.0.1", resolve));
const base = `http://127.0.0.1:${server.address().port}`;

let cookie = "";
const call = async (path, { method = "GET", body, token } = {}) => {
  const res = await fetch(base + path, {
    method,
    headers: {
      ...(body ? { "Content-Type": "application/json" } : {}),
      ...(token ? { Authorization: `Bearer ${token}` } : {}),
      ...(cookie ? { Cookie: cookie } : {}),
      "X-Admin": "1",
    },
    body: body ? JSON.stringify(body) : undefined,
  });
  const setCookie = res.headers.get("set-cookie");
  if (setCookie) cookie = setCookie.split(";")[0];
  const text = await res.text();
  let json = null;
  try {
    json = text ? JSON.parse(text) : null;
  } catch {}
  return { status: res.status, body: json, text, headers: res.headers };
};

const results = [];
const check = async (name, fn) => {
  const started = Date.now();
  try {
    await fn();
    results.push(true);
    console.log(`PASS ${name} (${Date.now() - started}ms)`);
  } catch (error) {
    results.push(false);
    console.log(`FAIL ${name}: ${error.message}`);
  }
};

console.log(`account: ${account.username}`);

await check("admin login", async () => {
  const res = await call("/api/admin/login", { method: "POST", body: { token: adminToken } });
  assert.equal(res.status, 200);
});

let key = null;
await check("mint key", async () => {
  const res = await call("/api/admin/keys", { method: "POST", body: { label: "smoke" } });
  assert.equal(res.status, 201);
  key = res.body.key;
});

await check("live model catalog through the key", async () => {
  const res = await call("/v1/models", { token: key });
  assert.equal(res.status, 200);
  assert.ok(res.body.data.length >= 10, `catalog too small: ${res.body.data.length}`);
});

await check("non-streaming completion (real upstream)", async () => {
  const res = await call("/v1/chat/completions", {
    method: "POST",
    token: key,
    body: { model: "deepseek-v4.1-flash", messages: [{ role: "user", content: "Reply with exactly: PONG" }] },
  });
  assert.equal(res.status, 200);
  assert.equal(res.body.choices[0].message.content.trim(), "PONG");
  assert.ok(res.body.usage.total_tokens > 0);
});

await check("streaming completion (real upstream)", async () => {
  const res = await fetch(`${base}/v1/chat/completions`, {
    method: "POST",
    headers: { Authorization: `Bearer ${key}`, "Content-Type": "application/json" },
    body: JSON.stringify({ model: "deepseek-v4.1-flash", messages: [{ role: "user", content: "count 1 to 3" }], stream: true }),
  });
  assert.equal(res.status, 200);
  assert.match(res.headers.get("content-type") ?? "", /text\/event-stream/);
  const text = await res.text();
  assert.match(text, /data: /);
  assert.match(text, /\[DONE\]/);
});

await check("allowance", async () => {
  const res = await call("/v1/usage", { token: key });
  assert.equal(res.status, 200);
  assert.ok(res.body.balance !== undefined);
});

server.close();
rmSync(dataDir, { recursive: true, force: true });
const failed = results.filter((ok) => !ok).length;
console.log(`\n${results.length - failed}/${results.length} checks passed`);
process.exit(failed === 0 ? 0 : 1);

