// 全链路集成：Python 注册机 → NDJSON 事件 → 网关号池 → /p/moonai/v1 反代出 Key。
//
// 上游用本地 mock（同 python 离线测试的那套形状），所以不碰真实站点：
// 测的是"注册机 ↔ 网关 ↔ 反代层"三者的接线，而不是站点行为。
import assert from "node:assert/strict";
import { createServer } from "node:http";
import { mkdtempSync, rmSync } from "node:fs";
import { tmpdir } from "node:os";
import { join } from "node:path";
import { spawn } from "node:child_process";

import { loadConfig } from "../src/config.mjs";
import { makeMachineHeader } from "../src/machine.mjs";
import { makeDispatcher } from "../src/dispatcher.mjs";
import { makeLane } from "../src/lane.mjs";
import { Store } from "../src/store.mjs";
import { createGateway } from "../src/server.mjs";
import { makeScheduler } from "../src/scheduler.mjs";
import { startMockUpstream } from "./mock-upstream.mjs";

/* ------------------------------------------------------- mock mooonai 站点 */
const calls = [];
const site = createServer((req, res) => {
  const url = new URL(req.url, "http://127.0.0.1");
  const chunks = [];
  req.on("data", (c) => chunks.push(c));
  req.on("end", () => {
    const raw = Buffer.concat(chunks).toString("utf8");
    const form = Object.fromEntries(new URLSearchParams(raw));
    const headers = { "content-type": "application/json" };
    calls.push({ method: req.method, path: url.pathname });
    const json = (status, value) => {
      const payload = Buffer.from(JSON.stringify(value));
      res.writeHead(status, { ...headers, "content-length": String(payload.length) });
      res.end(payload);
    };
    const html = (body, status = 200, extra = {}) => {
      const payload = Buffer.from(body);
      res.writeHead(status, { "content-type": "text/html; charset=utf-8", "content-length": String(payload.length), ...extra });
      res.end(payload);
    };
    const redirect = (location, extra = {}) => {
      res.writeHead(302, { location, ...extra, "content-length": "0" });
      res.end();
    };

    if (req.method === "GET" && url.pathname === "/auth/signup/") {
      return html('<input type="hidden" name="csrfmiddlewaretoken" value="csrf-signup">');
    }
    if (req.method === "POST" && url.pathname === "/auth/signup/") {
      return redirect("/auth/email-verification-sent/");
    }
    if (req.method === "GET" && url.pathname === "/auth/verify-email/") {
      return redirect("http://localhost/?registration_success=true");
    }
    if (req.method === "GET" && url.pathname === "/auth/signin/") {
      return html('<input type="hidden" name="csrfmiddlewaretoken" value="csrf-signin">');
    }
    if (req.method === "POST" && url.pathname === "/auth/signin/") {
      return redirect("/", { "set-cookie": "sessionid=sess-1; Path=/" });
    }
    if (url.pathname === "/api/v1/auth/url") {
      return json(200, { auth_url: `http://127.0.0.1:${site.address().port}/oauth2/authorize/?state=st` });
    }
    if (url.pathname === "/oauth2/authorize/") {
      return redirect(`http://127.0.0.1:${site.address().port}/oauth?code=CODE1&state=st`);
    }
    if (url.pathname === "/api/v1/auth/token") {
      if (!form.auth_response_url) return json(422, { detail: "auth_response_url required" });
      return json(200, { access_token: "jwt-live", refresh_token: "rf-live", expires_in: 86400, token_type: "Bearer" });
    }
    if (url.pathname === "/api/v1/auth/userinfo") {
      return json(200, { sub: "1", email_verified: true, preferred_username: "moon" });
    }
    if (req.method === "GET" && url.pathname === "/api/v1/organizations/") {
      return json(200, { count: 0, results: [] });
    }
    if (req.method === "POST" && url.pathname === "/api/v1/organizations") {
      return json(200, { id: 5100, name: "Moon E2E", subscription_type: "organization" });
    }
    if (url.pathname === "/api/v1/organizations/5100/api-keys") {
      // 真实站点每次都签新 key；mock 也必须有唯一性，否则号池按 apiKey 去重
      // 会把两个账号折成一条（测试夹具坑，不是产品行为）。
      const seq = (calls.filter((c) => c.path === "/api/v1/organizations/5100/api-keys").length || 0) + 1;
      return json(200, { id: 18 + seq, name: "openai-sdk", key: `api_E2E_${seq}`, is_active: true });
    }
    if (url.pathname === "/api/v1/services/llm-proxy/chat/completions") {
      if (req.headers.authorization !== "Bearer jwt-live") return json(403, { detail: "requires user authentication" });
      const body = JSON.parse(raw);
      return json(200, {
        content: `echo:${body.messages.at(-1)?.content ?? ""}`,
        finish_reason: "stop",
        model: `${body.model}-2024-07-18`,
        usage: { prompt_tokens: 4, completion_tokens: 3, total_tokens: 7 },
      });
    }
    json(404, { detail: "Not Found" });
  });
});
await new Promise((resolve) => site.listen(0, "127.0.0.1", resolve));
const siteBase = `http://127.0.0.1:${site.address().port}`;

/* ------------------------------------------------------------------ 网关 */
const adminToken = "worker-e2e-token";
const dataDir = mkdtempSync(join(tmpdir(), "moonai-worker-"));
const mock = await startMockUpstream();

// 先占一个端口，让网关用它（worker 的 --push-url 用的就是 config.port）
const probeServer = createServer();
await new Promise((resolve) => probeServer.listen(0, "127.0.0.1", resolve));
const gatewayPort = probeServer.address().port;
await new Promise((resolve) => probeServer.close(resolve));

const config = {
  ...loadConfig([]),
  port: gatewayPort,
  host: "127.0.0.1",
  token: adminToken,
  dataDir,
  apiOrigin: mock.origin,
  signInOrigin: mock.origin,
  log: "warn",
  // moonai 的端点指到本地 mock：注册机（--base-url/--api-base）与反代层都读这里
  providerEndpoints: { moonai: { apiBase: siteBase, panelBase: siteBase, coreUrl: siteBase } },
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
const gateway = createGateway({ config, lane, store, scheduler });
await new Promise((resolve) => gateway.server.listen(gatewayPort, "127.0.0.1", resolve));
const base = `http://127.0.0.1:${gatewayPort}`;

const runWorker = (args) =>
  new Promise((resolve, reject) => {
    const child = spawn(process.env.MOOONAI_PYTHON || (process.platform === "win32" ? "python" : "python3"), args, {
      cwd: join(process.cwd(), "tools", "moonai-reg"),
      env: { ...process.env, PYTHONUTF8: "1", PYTHONIOENCODING: "utf-8", PYTHONUNBUFFERED: "1", PYTHONPATH: "." },
      windowsHide: true,
    });
    let stdout = "";
    let stderr = "";
    child.stdout.on("data", (c) => (stdout += c));
    child.stderr.on("data", (c) => (stderr += c));
    child.on("error", reject);
    child.on("close", (code) => resolve({ code, stdout, stderr }));
  });

let passed = 0;
const check = (name, fn) => {
  try {
    fn();
    passed++;
    console.log(`  ok   ${name}`);
  } catch (error) {
    console.error(`  FAIL ${name}\n       ${error.message}`);
    throw error;
  }
};

console.log("MoonAI worker → gateway e2e");
const workerOut = `out-e2e-${process.pid}`;
try {
  const result = await runWorker([
    "-m", "moonai_reg", "run", "--json",
    "-n", "1", "-c", "1",
    "--mail-provider", "fixed",
    "--email", "worker@example.com",
    "--engine", "protocol",
    "--base-url", siteBase,
    "--api-base", siteBase,
    "--no-verify",
    "--keys-per-account", "1",
    "--out-dir", workerOut,
    "--push-url", base,
    "--push-token", adminToken,
  ]);

  const events = result.stdout
    .split(/\r?\n/)
    .map((line) => line.trim())
    .filter(Boolean)
    .map((line) => {
      try {
        return JSON.parse(line);
      } catch {
        return null;
      }
    })
    .filter(Boolean);

  check("worker 退出码 0", () => assert.equal(result.code, 0, result.stderr.slice(-400)));
  check("事件流有 start / account / pushed / done", () => {
    const types = events.map((e) => e.type);
    assert.ok(types.includes("start"), types.join(","));
    assert.ok(types.includes("account"), types.join(","));
    assert.ok(types.includes("pushed"), types.join(","));
    assert.ok(types.includes("done"), types.join(","));
  });

  const accountEvent = events.find((e) => e.type === "account");
  check("account 事件带齐号池字段", () => {
    const account = accountEvent.account;
    assert.equal(account.email, "worker@example.com");
    assert.match(account.apiKey, /^api_E2E_\d+$/);
    assert.equal(account.accessToken, "jwt-live");
    assert.equal(account.organizationId, "5100");
    assert.equal(account.status, "active");
  });

  const poolRes = await fetch(`${base}/api/admin/providers/moonai/pool`, { headers: { authorization: `Bearer ${adminToken}` } });
  const poolJson = await poolRes.json();
  check("网关号池收到账号并发了 sk-dcg Key", () => {
    assert.equal(poolJson.stats.total, 1);
    assert.equal(poolJson.stats.keys, 1);
    assert.equal(poolJson.accounts[0].email, "worker@example.com");
    assert.match(poolJson.accounts[0].gatewayKey.prefix, /^sk-dcg-/);
    assert.equal(JSON.stringify(poolJson).includes("jwt-live"), false);
  });

  const keysRes = await fetch(`${base}/api/admin/providers/moonai/keys`, { headers: { authorization: `Bearer ${adminToken}` } });
  const keysJson = await keysRes.json();
  check("Key 视图里能看到面板 Key 与 org", () => {
    assert.equal(keysJson.keys.length, 1);
    assert.equal(keysJson.keys[0].organizationId, "5100");
    assert.equal(keysJson.keys[0].hasRefreshToken, true);
  });

  // 网关 Key 明文只在 import 响应里出现一次；这里从号池文件取（本地测试目录）
  const runtimePool = gateway.providers.requireProvider("moonai").pool;
  const plaintextKey = runtimePool.accounts[0].gatewayKey.plaintext;
  check("号池里保存了网关 Key 明文（供反代自检）", () => assert.match(plaintextKey, /^sk-dcg-/));

  const chatRes = await fetch(`${base}/p/moonai/v1/chat/completions`, {
    method: "POST",
    headers: { authorization: `Bearer ${plaintextKey}`, "content-type": "application/json" },
    body: JSON.stringify({ model: "gpt-4o-mini", messages: [{ role: "user", content: "hello" }] }),
  });
  const chatText = await chatRes.text();
  check("用这把 Key 走反代出模型结果（编程工具就是这样调的）", () => {
    assert.equal(chatRes.status, 200, chatText.slice(0, 200));
    const parsed = JSON.parse(chatText);
    assert.equal(parsed.choices[0].message.content, "echo:hello");
    assert.equal(parsed.usage.total_tokens, 7);
  });

  const streamRes = await fetch(`${base}/p/moonai/v1/chat/completions`, {
    method: "POST",
    headers: { authorization: `Bearer ${plaintextKey}`, "content-type": "application/json", accept: "text/event-stream" },
    body: JSON.stringify({ model: "gpt-4o-mini", messages: [{ role: "user", content: "hi" }], stream: true }),
  });
  const streamText = await streamRes.text();
  check("流式同样可用（Cline / Claude Code 默认开流）", () => {
    assert.equal(streamRes.status, 200);
    assert.ok(streamText.trimEnd().endsWith("data: [DONE]"));
    assert.ok(streamText.includes("echo:hi"));
  });

  /* ---------------- 面板走的那条路：HTTP 起任务 → 轮询 → 入池 ---------------- */
  const started = await fetch(`${base}/api/admin/providers/moonai/register`, {
    method: "POST",
    headers: { authorization: `Bearer ${adminToken}`, "content-type": "application/json" },
    body: JSON.stringify({
      count: 1,
      concurrency: 1,
      engine: "protocol",
      mailProvider: "fixed",
      email: "worker2@example.com",
      verifyEmail: false,
      keysPerAccount: 1,
      push: true,
    }),
  });
  const startedJson = await started.json();
  check("POST /providers/moonai/register 起任务（面板按钮等价路径）", () => {
    assert.equal(started.status, 202, JSON.stringify(startedJson).slice(0, 300));
    assert.equal(startedJson.running, true);
  });

  let job = null;
  for (let i = 0; i < 60; i++) {
    const res = await fetch(`${base}/api/admin/providers/moonai/register`, {
      headers: { authorization: `Bearer ${adminToken}` },
    });
    const json = await res.json();
    job = json.job ?? null;
    if (job && job.status !== "running") break;
    await new Promise((r) => setTimeout(r, 1000));
  }
  check("任务结束并记下 created=1", () => {
    assert.ok(job, "没有 job 快照");
    const tail = (job.lines ?? []).slice(-6).map((l) => `${l.level}:${l.text}`).join(" | ");
    assert.equal(job.status, "done", `job 状态 ${job.status} · ${tail}`);
    assert.equal(job.created, 1, `created=${job.created} · ${tail}`);
  });

  const poolAfter = await (
    await fetch(`${base}/api/admin/providers/moonai/pool`, { headers: { authorization: `Bearer ${adminToken}` } })
  ).json();
  check("第二个账号也进了同一份号池（换号不换池文件）", () => {
    const emails = poolAfter.accounts.map((a) => a.email).sort();
    assert.deepEqual(emails, ["worker2@example.com", "worker@example.com"], JSON.stringify(emails));
    assert.equal(poolAfter.stats.total, 2);
  });

  console.log(`\n${passed} 项通过`);
} finally {
  gateway.server.close();
  await mock.close?.();
  site.close();
  store.flush?.();
  rmSync(dataDir, { recursive: true, force: true });
  rmSync(join(process.cwd(), "tools", "moonai-reg", workerOut), { recursive: true, force: true });
}


