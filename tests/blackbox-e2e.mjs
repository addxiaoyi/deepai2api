// Blackbox 全链路 e2e：mock 站点 → 真 python worker → 网关号池 → 反代出 Key。
//
//   node tests/blackbox-e2e.mjs
//
// 只 mock 站点，worker / 号池 / 反代 / 鉴权全是真代码路径：
//   1. /api/admin/providers 里能看到 blackbox（说明描述符装上了）
//   2. 手工 import 一个账号 → 号池有它 + 网关 Key
//   3. worker 用 protocol 腿跑一轮（session-file 复用登录态）→ 自己建 Key → 回推入池
//   4. /p/blackbox/v1/models 走 catalog 兜底（上游故意 404）
//   5. /p/blackbox/v1/chat/completions 透传 tool_calls；stream=true 走 SSE
//   6. /p/blackbox/v1/messages 由 relay 翻成 Anthropic message
//   7. 错误 Key 401；号池文件 schema 正确；不碰别的 provider 的号池
import assert from "node:assert/strict";
import { createServer } from "node:net";
import { existsSync, mkdtempSync, readFileSync, rmSync, writeFileSync } from "node:fs";
import { tmpdir } from "node:os";
import { join } from "node:path";

import { loadConfig } from "../src/config.mjs";
import { makeDispatcher } from "../src/dispatcher.mjs";
import { makeLane } from "../src/lane.mjs";
import { makeMachineHeader } from "../src/machine.mjs";
import { makeScheduler } from "../src/scheduler.mjs";
import { createGateway } from "../src/server.mjs";
import { Store } from "../src/store.mjs";
import { createMockBlackbox } from "./mock-blackbox.mjs";

const adminToken = "blackbox-test-token";
const dataDir = mkdtempSync(join(tmpdir(), "dcg-blackbox-"));
const mock = await createMockBlackbox({});
const seedKey = mock.seed("seed@own.example");

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
  providerEndpoints: {
    blackbox: { panelBase: mock.url, apiBase: mock.url },
  },
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
const { server, providers } = createGateway({ config, lane, store, scheduler });
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

const call = async (path, { method = "GET", body, token = adminToken, raw = false } = {}) => {
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
  return { status: res.status, body: json, text, headers: res.headers, raw };
};

/* ---------------------------------------------------------------- 1. catalog */
await check("provider catalog exposes blackbox with its real endpoints", async () => {
  const listed = await call("/api/admin/providers");
  assert.equal(listed.status, 200, listed.text.slice(0, 200));
  const row = listed.body.providers.find((p) => p.id === "blackbox");
  assert.ok(row, "blackbox 不在 provider 列表里");
  assert.equal(row.apiBase, mock.url);
  assert.equal(row.defaultModel, "blackboxai/blackbox-pro");
  assert.equal(row.openAiCompatible, true);
  assert.equal(row.supportsEmailSignup, false);
  assert.equal(row.supportsOAuthOnly, true);
  assert.ok(row.models.includes("blackboxai/blackbox-pro"));
  assert.equal(row.auth.kind, "authjs");
  assert.ok(listed.body.schemas.includes("dcg.pool/blackbox@1"));
});

/* --------------------------------------------------------- 2. import + pool */
let gatewayKey = "";
await check("import a account mints a gateway key and isolates the pool file", async () => {
  const imported = await call("/api/admin/providers/blackbox/pool/import", {
    method: "POST",
    body: { account: { email: "seed@own.example", apiKey: seedKey, plan: "pro", engine: "protocol" }, mintKey: true },
  });
  assert.equal(imported.status, 200, imported.text.slice(0, 300));
  gatewayKey = imported.body.gatewayKey?.plaintext ?? "";
  assert.ok(gatewayKey.startsWith("sk-dcg-"), "没有拿到网关 Key");
  assert.equal(imported.body.pool.total, 1);

  const poolFile = join(dataDir, "blackbox-pool.json");
  assert.ok(existsSync(poolFile), "号池文件没落盘");
  const parsed = JSON.parse(readFileSync(poolFile, "utf8"));
  assert.equal(parsed.schema, "dcg.pool/blackbox@1");
  assert.equal(parsed.provider, "blackbox");
  assert.equal(parsed.accounts[0].email, "seed@own.example");
  // 不碰别人的号池
  assert.equal(existsSync(join(dataDir, "allmodels-pool.json")), false);
  assert.equal(existsSync(join(dataDir, "moonai-pool.json")), false);
});

/* --------------------------------------------------------- 3. models fallback */
await check("/p/blackbox/v1/models falls back to the descriptor catalog", async () => {
  const listed = await call("/p/blackbox/v1/models", { token: gatewayKey });
  assert.equal(listed.status, 200, listed.text.slice(0, 300));
  const ids = listed.body.data.map((m) => m.id);
  assert.ok(ids.includes("blackboxai/blackbox-pro"), `catalog 兜底失效：${ids.join(",")}`);
  assert.ok(ids.length >= 5);
});

/* ------------------------------------------------------------ 4. chat passthrough */
await check("chat completions pass through with tool_calls intact", async () => {
  const res = await call("/p/blackbox/v1/chat/completions", {
    method: "POST",
    token: gatewayKey,
    body: {
      model: "blackboxai/blackbox-pro",
      messages: [{ role: "user", content: "list files" }],
      tools: [{ type: "function", function: { name: "shell", parameters: { type: "object", properties: { command: { type: "string" } } } } }],
    },
  });
  assert.equal(res.status, 200, res.text.slice(0, 300));
  assert.equal(res.headers.get("x-gateway-provider"), "blackbox");
  const choice = res.body.choices[0];
  assert.equal(choice.finish_reason, "tool_calls");
  assert.equal(choice.message.tool_calls[0].function.name, "shell");
  assert.equal(mock.state.calls.chat >= 1, true);
});

await check("stream=true relays SSE and ends with [DONE]", async () => {
  const res = await fetch(`${base}/p/blackbox/v1/chat/completions`, {
    method: "POST",
    headers: { Authorization: `Bearer ${gatewayKey}`, "Content-Type": "application/json" },
    body: JSON.stringify({ model: "blackboxai/blackbox-pro", messages: [{ role: "user", content: "hi" }], stream: true }),
  });
  assert.equal(res.status, 200);
  const text = await res.text();
  assert.ok(text.includes("data: "), "没有 SSE 数据帧");
  assert.ok(text.trim().endsWith("data: [DONE]"), `SSE 没有正确收尾：${text.slice(-60)}`);
});

/* --------------------------------------------------------- 5. anthropic surface */
await check("/p/blackbox/v1/messages is translated to an Anthropic message", async () => {
  const res = await call("/p/blackbox/v1/messages", {
    method: "POST",
    token: gatewayKey,
    body: { model: "blackboxai/blackbox-pro", max_tokens: 64, messages: [{ role: "user", content: "hi" }] },
  });
  assert.equal(res.status, 200, res.text.slice(0, 300));
  assert.equal(res.body.type, "message");
  assert.equal(res.body.role, "assistant");
  assert.ok(Array.isArray(res.body.content));
});

/* ------------------------------------------------------------- 6. auth errors */
await check("a bogus gateway key is rejected before touching upstream", async () => {
  const res = await call("/p/blackbox/v1/chat/completions", {
    method: "POST",
    token: "sk-dcg-not-a-real-key-000000000000",
    body: { model: "blackboxai/blackbox-pro", messages: [{ role: "user", content: "hi" }] },
  });
  assert.equal(res.status, 401, res.text.slice(0, 200));
});

/* ------------------------------------------------- 7. worker: protocol lane */
await check("python worker registers through the protocol lane, mints a key and pushes back", async () => {
  const cookieFile = join(dataDir, "worker-session.json");
  writeFileSync(
    cookieFile,
    JSON.stringify([
      { name: "__Secure-authjs.session-token", value: "worker@own.example", domain: "", path: "/" },
    ]),
  );

  const started = await call("/api/admin/providers/blackbox/register", {
    method: "POST",
    body: {
      count: 1,
      concurrency: 1,
      engine: "protocol",
      mailProvider: "fixed",
      email: "worker@own.example",
      baseUrl: mock.url,
      apiBase: mock.url,
      keysPerAccount: 1,
      verifyEmail: false,
      session: { file: cookieFile },
      antiBan: { jitterMs: [0, 0], ipConcurrency: 1 },
    },
  });
  if (started.status !== 202) {
    // worker 目录缺失时给出明确原因，而不是不明所以的超时
    throw new Error(`register 没有启动：HTTP ${started.status} ${started.text.slice(0, 400)}`);
  }

  const deadline = Date.now() + 180_000;
  let status = started.body;
  while (status.running && Date.now() < deadline) {
    await new Promise((done) => setTimeout(done, 700));
    status = (await call("/api/admin/providers/blackbox/register")).body;
  }
  assert.equal(status.running, false, "job 超时未结束");
  assert.equal(status.job?.status, "done", `job 状态 ${status.job?.status}：${JSON.stringify(status.job?.lines?.slice(-6))}`);
  assert.ok((status.job.created ?? 0) >= 1, `没有创建账号：${JSON.stringify(status.job?.lines?.slice(-8))}`);

  const pool = (await call("/api/admin/providers/blackbox/pool")).body;
  const row = pool.accounts.find((a) => a.email === "worker@own.example");
  assert.ok(row, "worker 的账号没进号池");
  assert.ok(row.apiKeyPreview.startsWith("sk-mock"), `面板 Key 不对：${row.apiKeyPreview}`);
  assert.ok(row.gatewayKey?.prefix?.startsWith("sk-dcg-"), "worker 的账号没有绑定网关 Key");

  // 用 worker 产出的那把 Key 真的打一次反代
  const workerKeyPool = JSON.parse(readFileSync(join(dataDir, "blackbox-pool.json"), "utf8"));
  const workerKey = workerKeyPool.accounts.find((a) => a.email === "worker@own.example").gatewayKey.plaintext;
  const chat = await call("/p/blackbox/v1/chat/completions", {
    method: "POST",
    token: workerKey,
    body: { model: "blackboxai/blackbox-pro", messages: [{ role: "user", content: "ping" }] },
  });
  assert.equal(chat.status, 200, chat.text.slice(0, 300));
});

/* ----------------------------------------------------------------- 8. cleanup */
const failed = results.filter((row) => !row.ok);
console.log(`\n${results.length - failed.length}/${results.length} checks passed`);
console.log(`mock calls: ${JSON.stringify(mock.state.calls)}`);

await mock.close();
await new Promise((done) => server.close(done));
try {
  rmSync(dataDir, { recursive: true, force: true });
} catch {
  // Windows 上 sqlite 可能还占着句柄，交给系统清理
}
process.exit(failed.length ? 1 : 0);
