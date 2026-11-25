// MoonAI 端到端：mock 上游 → provider 反代 → OpenAI / Anthropic / SSE / 工具调用 / 换号续期。
//
// 跑法：node tests/moonai-e2e.mjs
// 不碰真实站点，也不读真实号池（临时目录 + 临时 env）。
import assert from "node:assert/strict";
import { createServer } from "node:http";
import { mkdtempSync, rmSync } from "node:fs";
import { tmpdir } from "node:os";
import { join } from "node:path";

import { hashKey, extractPresentedKey } from "../src/keys.mjs";
import { Store } from "../src/store.mjs";
import { importAccount } from "../src/provider-pool.mjs";
import { createProviderRuntime } from "../src/providers.mjs";

const MAX_MODELS = 6;
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

/* ------------------------------------------------------------ mock 上游 */
const upstream = {
  calls: [],
  failFirstAccount: false,
  rejectOnce: false,
  refreshed: 0,
};
const upstreamServer = createServer((req, res) => {
  const url = new URL(req.url, "http://127.0.0.1");
  const chunks = [];
  req.on("data", (c) => chunks.push(c));
  req.on("end", () => {
    const raw = Buffer.concat(chunks).toString("utf8");
    const isJson = raw.trim().startsWith("{");
    const body = isJson ? JSON.parse(raw) : Object.fromEntries(new URLSearchParams(raw));
    upstream.calls.push({ path: url.pathname, auth: req.headers.authorization, body });

    const send = (status, value) => {
      const payload = Buffer.from(JSON.stringify(value));
      res.writeHead(status, { "content-type": "application/json", "content-length": String(payload.length) });
      res.end(payload);
    };

    if (url.pathname === "/api/v1/auth/refresh") {
      upstream.refreshed++;
      return send(200, { access_token: "jwt-refreshed", refresh_token: "rf-2", expires_in: 86400, token_type: "Bearer" });
    }

    if (url.pathname === "/api/v1/services/llm-proxy/chat/completions") {
      if (upstream.rejectOnce) {
        upstream.rejectOnce = false;
        return send(500, { detail: "temporary" });
      }
      if (req.headers.authorization === "Bearer jwt-dead") {
        return send(403, { detail: "This endpoint requires user authentication" });
      }
      const wantsTool = JSON.stringify(body.messages).includes("<tool_name>");
      // 模拟上游的 extra_forbidden：多一个字段就 422
      for (const forbidden of ["stream", "tools", "tool_choice", "stop", "n", "user"]) {
        if (body[forbidden] !== undefined) {
          return send(422, { detail: "Validation error", field: forbidden });
        }
      }
      const content = wantsTool ? '{"tool":"read_file","arguments":{"path":"a.txt"}}' : `echo:${body.messages.at(-1)?.content ?? ""}`;
      return send(200, {
        content,
        finish_reason: "stop",
        model: `${body.model}-2024-07-18`,
        usage: { prompt_tokens: 11, completion_tokens: 5, total_tokens: 16 },
      });
    }

    send(404, { detail: "Not Found" });
  });
});

await new Promise((resolve) => upstreamServer.listen(0, "127.0.0.1", resolve));
const upstreamPort = upstreamServer.address().port;
const upstreamBase = `http://127.0.0.1:${upstreamPort}`;

/* ------------------------------------------------------------- 网关侧 */
const dataDir = mkdtempSync(join(tmpdir(), "moonai-e2e-"));
const store = Store.at(dataDir);
const runtime = createProviderRuntime({
  dataDir,
  port: 0,
  token: "admin-test",
  store,
  log: () => {},
  // 端点覆盖：不依赖环境变量（沙箱里 process.env 不一定按预期工作），
  // 也让测试完全走本地 mock，不打真实站点。
  endpointOverrides: { moonai: { apiBase: upstreamBase, panelBase: upstreamBase, coreUrl: upstreamBase } },
});
const { pool } = runtime.requireProvider("moonai");

const first = importAccount({
  pool,
  store,
  record: { email: "one@example.com", apiKey: "api_ONE", accessToken: "jwt-live", refreshToken: "rf-1", accessTokenExpiresAt: Date.now() + 3600_000 },
});
const second = importAccount({
  pool,
  store,
  record: { email: "two@example.com", apiKey: "api_TWO", accessToken: "jwt-live-2", refreshToken: "rf-2", accessTokenExpiresAt: Date.now() + 3600_000 },
});
const gatewayKey = first.gatewayKey.plaintext;

const gateway = createServer(async (req, res) => {
  const url = new URL(req.url, "http://127.0.0.1");
  const presented = extractPresentedKey(req, url);
  const key = presented ? store.findByHash(hashKey(presented)) : null;
  const handled = await runtime.relay.handle(req, res, url, { auth: key ? { ok: true, kind: "key", keyId: key.id } : { ok: false } });
  if (!handled) {
    res.writeHead(404).end("no route");
  }
});
await new Promise((resolve) => gateway.listen(0, "127.0.0.1", resolve));
const base = `http://127.0.0.1:${gateway.address().port}`;

const call = async (path, { method = "POST", body, key = gatewayKey, accept = "application/json" } = {}) => {
  const res = await fetch(`${base}${path}`, {
    method,
    headers: {
      ...(key ? { authorization: `Bearer ${key}` } : {}),
      "content-type": "application/json",
      accept,
    },
    body: body ? JSON.stringify(body) : undefined,
  });
  const text = await res.text();
  return { status: res.status, text, headers: res.headers };
};

const sse = (text) =>
  text
    .split("\n")
    .filter((line) => line.startsWith("data: "))
    .map((line) => line.slice(6).trim())
    .filter((line) => line && line !== "[DONE]")
    .map((line) => JSON.parse(line));

console.log("MoonAI e2e");
try {
  /* ---------------------------------------------------------------- models */
  {
    const res = await call("/p/moonai/v1/models", { method: "GET" });
    check("GET /p/moonai/v1/models → 白名单 6 个模型", () => {
      assert.equal(res.status, 200);
      const parsed = JSON.parse(res.text);
      assert.equal(parsed.object, "list");
      assert.equal(parsed.data.length, MAX_MODELS);
      assert.ok(parsed.data.some((m) => m.id === "gpt-4o-mini"));
    });
  }

  /* ------------------------------------------------------------ 非流式聊天 */
  {
    const res = await call("/p/moonai/v1/chat/completions", {
      body: { model: "gpt-4o-mini", messages: [{ role: "user", content: "hello" }], stream: false, temperature: 0.3, top_p: 0.9 },
    });
    check("非流式：OpenAI completion 形状 + 上游只收到白名单字段", () => {
      assert.equal(res.status, 200, `上游 ${runtime.endpoints("moonai").apiBase} → ${res.text.slice(0, 200)}`);
      const parsed = JSON.parse(res.text);
      assert.equal(parsed.object, "chat.completion");
      assert.equal(parsed.choices[0].message.content, "echo:hello");
      assert.equal(parsed.choices[0].finish_reason, "stop");
      assert.equal(parsed.usage.total_tokens, 16);
      const sent = upstream.calls.at(-1).body;
      assert.equal(sent.stream, undefined);
      assert.equal(sent.temperature, 0.3);
      assert.equal(sent.top_p, 0.9);
      assert.equal(upstream.calls.at(-1).auth, "Bearer jwt-live");
    });
  }

  /* ---------------------------------------------------------------- 流式 */
  {
    const res = await call("/p/moonai/v1/chat/completions", {
      body: { model: "gpt-4o-mini", messages: [{ role: "user", content: "hi" }], stream: true },
      accept: "text/event-stream",
    });
    check("流式：合成 SSE（role 头 / 正文 / finish / [DONE]）", () => {
      assert.equal(res.status, 200);
      assert.match(res.headers.get("content-type") ?? "", /text\/event-stream/);
      assert.ok(res.text.trimEnd().endsWith("data: [DONE]"));
      const deltas = sse(res.text);
      assert.equal(deltas[0].choices[0].delta.role, "assistant");
      const text = deltas.map((d) => d.choices?.[0]?.delta?.content ?? "").join("");
      assert.equal(text, "echo:hi");
      assert.ok(deltas.some((d) => d.choices?.[0]?.finish_reason === "stop"));
    });
  }

  /* ------------------------------------------------------------ 工具调用 */
  {
    const res = await call("/p/moonai/v1/chat/completions", {
      body: {
        model: "gpt-4o-mini",
        messages: [{ role: "user", content: "read a.txt" }],
        tools: [
          {
            type: "function",
            function: { name: "read_file", description: "read", parameters: { type: "object", properties: { path: { type: "string" } } } },
          },
        ],
        tool_choice: "auto",
      },
    });
    check("工具调用：tools 被翻译成提示词，回程变成 tool_calls", () => {
      assert.equal(res.status, 200);
      const parsed = JSON.parse(res.text);
      assert.equal(parsed.choices[0].finish_reason, "tool_calls");
      assert.equal(parsed.choices[0].message.tool_calls[0].function.name, "read_file");
      assert.deepEqual(JSON.parse(parsed.choices[0].message.tool_calls[0].function.arguments), { path: "a.txt" });
      const sent = upstream.calls.at(-1).body;
      assert.equal(sent.tools, undefined);
      assert.equal(sent.tool_choice, undefined);
      assert.match(sent.messages[0].content, /read_file/);
    });
  }

  /* ------------------------------------------------------------ Anthropic */
  {
    const res = await call("/p/moonai/v1/messages", {
      body: { model: "gpt-4o-mini", max_tokens: 32, messages: [{ role: "user", content: "hello" }] },
    });
    check("/v1/messages：Anthropic message 形状", () => {
      assert.equal(res.status, 200);
      const parsed = JSON.parse(res.text);
      assert.equal(parsed.type, "message");
      assert.equal(parsed.role, "assistant");
      assert.equal(parsed.content[0].text, "echo:hello");
      assert.equal(parsed.stop_reason, "end_turn");
    });
  }

  /* ------------------------------------------------------------ 换号重试 */
  {
    upstream.rejectOnce = true;
    const before = upstream.calls.length;
    const res = await call("/p/moonai/v1/chat/completions", {
      body: { model: "gpt-4o-mini", messages: [{ role: "user", content: "rotate" }] },
    });
    check("上游 500：自动换号并成功返回", () => {
      assert.equal(res.status, 200);
      assert.equal(JSON.parse(res.text).choices[0].message.content, "echo:rotate");
      assert.ok(upstream.calls.length - before >= 2, "至少打了两次上游");
      const auths = upstream.calls.slice(before).map((c) => c.auth);
      assert.ok(new Set(auths).size >= 1);
    });
  }

  /* ------------------------------------------------------------ 令牌续期 */
  {
    const dead = importAccount({
      pool,
      store,
      record: { email: "dead@example.com", apiKey: "api_DEAD", accessToken: "jwt-dead", refreshToken: "rf-dead", accessTokenExpiresAt: Date.now() + 3600_000 },
    });
    const refreshedBefore = upstream.refreshed;
    // 只留这一个号可用，确保请求打到它
    for (const account of pool.accounts) {
      if (account.email !== "dead@example.com") pool.update(account.id, { status: "disabled" });
    }
    const res = await call("/p/moonai/v1/chat/completions", {
      body: { model: "gpt-4o-mini", messages: [{ role: "user", content: "renew" }] },
      key: dead.gatewayKey.plaintext,
    });
    check("上游 403：refresh_token 续期后重试成功", () => {
      assert.equal(res.status, 200);
      assert.ok(upstream.refreshed > refreshedBefore, "应当调用过 refresh");
      assert.equal(pool.byEmail("dead@example.com").accessToken, "jwt-refreshed");
    });
    for (const account of pool.accounts) pool.update(account.id, { status: "active" });
  }

  /* ---------------------------------------------------------------- 鉴权 */
  {
    const res = await call("/p/moonai/v1/chat/completions", {
      body: { model: "gpt-4o-mini", messages: [{ role: "user", content: "x" }] },
      key: "",
    });
    check("没有网关 Key → 401", () => {
      assert.equal(res.status, 401);
      assert.match(res.text, /invalid_api_key/);
    });
  }

  /* ------------------------------------------------------------ 未知 provider */
  {
    const res = await call("/p/nope/v1/chat/completions", { body: {} });
    check("未知 provider → 404 unknown_provider", () => {
      assert.equal(res.status, 404);
      assert.match(res.text, /unknown_provider/);
    });
  }

  /* ------------------------------------------------------------ 号池隔离 */
  {
    check("号池文件按 provider 隔离", () => {
      assert.ok(pool.path.endsWith("moonai-pool.json"));
      assert.equal(pool.toJSON().schema, "dcg.pool/moonai@1");
      assert.equal(runtime.pools.has("wavespeed"), true);
      assert.notEqual(runtime.pools.get("wavespeed").path, pool.path);
    });
  }

  console.log(`\n${passed} 项通过`);
} finally {
  gateway.close();
  upstreamServer.close();
  store.flush?.();
  rmSync(dataDir, { recursive: true, force: true });
}
