// MoonAI 适配层与号池的单测（node --test）。
//
// 覆盖点都是"直传会炸"的地方：
//   * 上游 pydantic extra_forbidden → 请求体必须只剩白名单字段
//   * 上游没有 stream / tools → 必须合成 SSE、必须把工具调用翻译成 tool_calls
//   * 上游用 access_token 而不是 api_ Key → 鉴权头取错就会 403
//   * 号池重推只带 apiKey 时不能把已有 access_token 抹掉
import assert from "node:assert/strict";
import { mkdtempSync, readFileSync, rmSync, utimesSync } from "node:fs";
import { tmpdir } from "node:os";
import { join } from "node:path";
import test from "node:test";

import { PROVIDERS, getProvider, providerSummary } from "../src/provider-catalog.mjs";
import { ProviderPool, importAccount, maskAccount, normalizeAccount } from "../src/provider-pool.mjs";
import {
  buildUpstreamPayload,
  needsRefresh,
  parseToolCall,
  synthesizeChatSSE,
  toAnthropicMessage,
  toOpenAIChatCompletion,
  upstreamAuthHeaders,
} from "../src/moonai-adapter.mjs";

const moonai = getProvider("moonai");

/* ------------------------------------------------------------------ catalog */

test("catalog: moonai 描述符带齐反代与面板元数据", () => {
  assert.ok(PROVIDERS.some((p) => p.id === "moonai"));
  assert.equal(moonai.poolFile, "moonai-pool.json");
  assert.equal(moonai.workerModule, "moonai_reg");
  assert.equal(moonai.relay.kind, "moonai-llm-proxy");
  assert.equal(moonai.relay.authMode, "user-token");
  assert.equal(moonai.relay.emulateTools, true);
  assert.equal(moonai.relay.synthesizeStream, true);
  assert.deepEqual(moonai.models, ["gpt-4.1", "gpt-4o", "gpt-4o-mini", "gpt-5-nano", "o3-mini", "o4-mini"]);
  const summary = providerSummary(moonai);
  assert.equal(summary.relay.kind, "moonai-llm-proxy");
  assert.equal(summary.models.length, 6);
  assert.ok(summary.panelApi.llmProxy.path.endsWith("/llm-proxy/chat/completions"));
});

/* ----------------------------------------------------------------- adapter */

test("adapter: 剥掉上游不接受的字段", () => {
  const { payload } = buildUpstreamPayload(
    {
      model: "gpt-4o-mini",
      messages: [{ role: "user", content: "hi" }],
      stream: true,
      stream_options: { include_usage: true },
      stop: ["\n"],
      n: 1,
      user: "u",
      frequency_penalty: 0,
      presence_penalty: 0,
      response_format: { type: "json_object" },
      seed: 1,
      max_completion_tokens: 64,
      temperature: 0.2,
      top_p: 0.9,
    },
    moonai,
  );
  for (const forbidden of ["stream", "stream_options", "stop", "n", "user", "frequency_penalty", "presence_penalty", "response_format", "seed"]) {
    assert.equal(payload[forbidden], undefined, `${forbidden} 必须被剥掉`);
  }
  assert.equal(payload.model, "gpt-4o-mini");
  assert.equal(payload.temperature, 0.2);
  assert.equal(payload.top_p, 0.9);
  // max_completion_tokens 收敛成 max_tokens
  assert.equal(payload.max_tokens, 64);
  assert.equal(payload.max_completion_tokens, undefined);
});

test("adapter: 工具协议注入 + tool 结果折成 user", () => {
  const { payload, toolMode } = buildUpstreamPayload(
    {
      model: "gpt-4o-mini",
      messages: [
        { role: "system", content: "you are a coder" },
        { role: "user", content: "read a.txt" },
        { role: "assistant", content: "", tool_calls: [{ id: "c1", type: "function", function: { name: "read_file", arguments: '{"path":"a.txt"}' } }] },
        { role: "tool", tool_call_id: "c1", content: "hello" },
      ],
      tools: [
        {
          type: "function",
          function: {
            name: "read_file",
            description: "read a file",
            parameters: { type: "object", properties: { path: { type: "string" } }, required: ["path"] },
          },
        },
      ],
      tool_choice: "auto",
    },
    moonai,
  );
  assert.equal(toolMode, true);
  const system = payload.messages.find((m) => m.role === "system");
  assert.match(system.content, /read_file/);
  assert.match(system.content, /"tool":"<tool_name>"/);
  assert.match(system.content, /you are a coder/);
  // assistant 的 tool_calls 还原成 JSON 文本
  const assistant = payload.messages.find((m) => m.role === "assistant");
  assert.match(assistant.content, /"tool":"read_file"/);
  // tool 结果变成 user 消息
  const toolTurn = payload.messages.at(-1);
  assert.equal(toolTurn.role, "user");
  assert.match(toolTurn.content, /hello/);
});

test("adapter: 没有 tools 时不注入协议", () => {
  const { toolMode } = buildUpstreamPayload({ model: "gpt-4o-mini", messages: [{ role: "user", content: "hi" }] }, moonai);
  assert.equal(toolMode, false);
});

test("adapter: parseToolCall 认得裸 JSON / fenced / 带前言", () => {
  assert.deepEqual(parseToolCall('{"tool":"read_file","arguments":{"path":"a.txt"}}'), {
    name: "read_file",
    arguments: '{"path":"a.txt"}',
  });
  assert.equal(parseToolCall('```json\n{"name":"grep","arguments":{"q":"x"}}\n```').name, "grep");
  assert.equal(parseToolCall('sure: {"tool":"ls","arguments":{}}').name, "ls");
  assert.equal(parseToolCall("plain prose"), null);
  assert.equal(parseToolCall('{"foo":1}'), null);
});

test("adapter: 上游 JSON -> OpenAI completion", () => {
  const completion = toOpenAIChatCompletion(
    { content: "pong", finish_reason: "stop", model: "gpt-4o-mini-2024-07-18", usage: { prompt_tokens: 3, completion_tokens: 1, total_tokens: 4 } },
    { model: "gpt-4o-mini" },
  );
  assert.equal(completion.object, "chat.completion");
  assert.equal(completion.model, "gpt-4o-mini");
  assert.equal(completion.choices[0].message.content, "pong");
  assert.equal(completion.choices[0].finish_reason, "stop");
  assert.equal(completion.usage.total_tokens, 4);
});

test("adapter: 工具调用回程变成 tool_calls", () => {
  const completion = toOpenAIChatCompletion(
    { content: '{"tool":"read_file","arguments":{"path":"a.txt"}}', finish_reason: "stop", usage: {} },
    { model: "gpt-4o-mini", toolMode: true },
  );
  const choice = completion.choices[0];
  assert.equal(choice.finish_reason, "tool_calls");
  assert.equal(choice.message.content, null);
  assert.equal(choice.message.tool_calls[0].function.name, "read_file");
  assert.deepEqual(JSON.parse(choice.message.tool_calls[0].function.arguments), { path: "a.txt" });
});

test("adapter: SSE 合成有 role 头、正文分片、finish 与 [DONE]", () => {
  const completion = toOpenAIChatCompletion({ content: "abcdefghij", finish_reason: "stop", usage: { total_tokens: 3 } }, { model: "gpt-4o-mini" });
  const { chunks } = synthesizeChatSSE(completion, { sliceSize: 4 });
  assert.match(chunks[0], /"role":"assistant"/);
  assert.equal(chunks.at(-1), "data: [DONE]\n\n");
  const text = chunks
    .filter((c) => c !== "data: [DONE]\n\n")
    .map((c) => JSON.parse(c.slice(6)))
    .map((p) => p.choices?.[0]?.delta?.content ?? "")
    .join("");
  assert.equal(text, "abcdefghij");
  assert.ok(chunks.some((c) => c.includes('"finish_reason":"stop"')));
});

test("adapter: 工具调用的 SSE 分片带 index 与 arguments 增量", () => {
  const completion = toOpenAIChatCompletion(
    { content: '{"tool":"read_file","arguments":{"path":"a.txt"}}', usage: {} },
    { model: "gpt-4o-mini", toolMode: true },
  );
  const { chunks } = synthesizeChatSSE(completion, { sliceSize: 8 });
  const deltas = chunks
    .filter((c) => c !== "data: [DONE]\n\n")
    .map((c) => JSON.parse(c.slice(6)).choices?.[0]?.delta)
    .filter((d) => d?.tool_calls?.length);
  assert.ok(deltas.length >= 1);
  assert.equal(deltas[0].tool_calls[0].function.name, "read_file");
  const joined = deltas.map((d) => d.tool_calls[0].function.arguments ?? "").join("");
  assert.deepEqual(JSON.parse(joined), { path: "a.txt" });
});

test("adapter: Anthropic 形状", () => {
  const message = toAnthropicMessage({ content: "hi", finish_reason: "stop", usage: { prompt_tokens: 2, completion_tokens: 1 } }, { model: "gpt-4o-mini" });
  assert.equal(message.type, "message");
  assert.equal(message.content[0].text, "hi");
  assert.equal(message.stop_reason, "end_turn");
  assert.equal(message.usage.input_tokens, 2);
});

test("adapter: 鉴权头用 access_token 而不是 api_ Key", () => {
  const headers = upstreamAuthHeaders({ apiKey: "api_XXX", accessToken: "jwt-YYY" }, moonai);
  assert.equal(headers.Authorization, "Bearer jwt-YYY");
});

test("adapter: 续期判断", () => {
  assert.equal(needsRefresh({ accessTokenExpiresAt: Date.now() + 5 * 60_000 }, 10 * 60_000), true);
  assert.equal(needsRefresh({ accessTokenExpiresAt: Date.now() + 60 * 60_000 }, 10 * 60_000), false);
  assert.equal(needsRefresh({ accessTokenExpiresAt: 0 }), false);
});

/* -------------------------------------------------------------------- pool */

test("pool: 归一化带上会话令牌字段", () => {
  const account = normalizeAccount({
    email: "A@Example.com",
    access_token: "jwt",
    refresh_token: "rf",
    access_token_expires_at: 123,
    organization_id: "5100",
  });
  assert.equal(account.email, "a@example.com");
  assert.equal(account.accessToken, "jwt");
  assert.equal(account.refreshToken, "rf");
  assert.equal(account.accessTokenExpiresAt, 123);
  assert.equal(account.organizationId, "5100");
});

test("pool: 重推只带 apiKey 时不清掉已有令牌", () => {
  const dir = mkdtempSync(join(tmpdir(), "moonai-pool-"));
  try {
    const pool = ProviderPool.at(dir, moonai);
    pool.add({ email: "a@example.com", password: "p", apiKey: "api_1", accessToken: "jwt-1", refreshToken: "rf-1", accessTokenExpiresAt: 999 });
    const { account } = pool.add({ email: "a@example.com", password: "p", apiKey: "api_1" });
    assert.equal(account.accessToken, "jwt-1");
    assert.equal(account.refreshToken, "rf-1");
    assert.equal(account.accessTokenExpiresAt, 999);
    // 号池文件按 provider 物理隔离
    assert.equal(pool.path.endsWith("moonai-pool.json"), true);
    assert.equal(pool.toJSON().schema, "dcg.pool/moonai@1");
  } finally {
    rmSync(dir, { recursive: true, force: true });
  }
});

test("pool: 脱敏视图不泄漏令牌明文", () => {
  const masked = maskAccount({ email: "a@example.com", accessToken: "supersecretjwt", refreshToken: "rf", apiKey: "api_1234567890" });
  assert.equal(JSON.stringify(masked).includes("supersecretjwt"), false);
  assert.equal(masked.hasRefreshToken, true);
  assert.equal(masked.apiKeyPreview.startsWith("api_1234"), true);
});

test("pool: 磁盘快照较旧时不丢本地账号（回归：曾按 touchedIds 过滤导致丢号）", () => {
  const dir = mkdtempSync(join(tmpdir(), "moonai-pool-"));
  try {
    // p1 先落一条
    const p1 = ProviderPool.at(dir, moonai);
    p1.add({ email: "a@example.com", apiKey: "api_a", accessToken: "t" });

    // 另一个写入方（p2）再加一条，落盘后人为把 mtime 推后，
    // 让 p1 下一次 save() 走"磁盘更新 → 合并"分支
    const p2 = ProviderPool.at(dir, moonai);
    p2.add({ email: "b@example.com", apiKey: "api_b", accessToken: "t" });
    const future = new Date(Date.now() + 5000);
    utimesSync(p1.path, future, future);

    // p1 在内存里再建一条（此刻 p1 不知道 b，且 a 不在 touchedIds 里）
    p1.add({ email: "c@example.com", apiKey: "api_c", accessToken: "t" });

    const disk = JSON.parse(readFileSync(p1.path, "utf8"));
    const emails = disk.accounts.map((a) => a.email).sort();
    assert.deepEqual(emails, ["a@example.com", "b@example.com", "c@example.com"]);
  } finally {
    rmSync(dir, { recursive: true, force: true });
  }
});

test("pool: importAccount 幂等，不会重复发网关 Key", () => {
  const dir = mkdtempSync(join(tmpdir(), "moonai-pool-"));
  try {
    const pool = ProviderPool.at(dir, moonai);
    const store = {
      keys: [],
      addKey(entry) {
        const key = { id: `k${this.keys.length + 1}`, label: entry.label, prefix: entry.prefix, createdAt: new Date().toISOString() };
        this.keys.push({ ...entry, id: key.id });
        return key;
      },
    };
    const first = importAccount({ pool, store, record: { email: "b@example.com", apiKey: "api_2", accessToken: "jwt" } });
    const second = importAccount({ pool, store, record: { email: "b@example.com", apiKey: "api_2", accessToken: "jwt" } });
    assert.equal(first.created, true);
    assert.equal(second.created, false);
    assert.equal(store.keys.length, 1);
    assert.equal(second.gatewayKey.prefix, first.gatewayKey.prefix);
  } finally {
    rmSync(dir, { recursive: true, force: true });
  }
});
