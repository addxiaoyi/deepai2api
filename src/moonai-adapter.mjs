// MoonAI 反代适配器 —— OpenAI 形状 ⇄ mooonai llm-proxy 形状。
//
// 为什么单独一层：
//   * 上游 `POST /api/v1/services/llm-proxy/chat/completions` 是 **自有协议**：
//     请求 pydantic `extra_forbidden`（多一个字段就 422），响应是
//     `{content, finish_reason, model, usage}`，**没有 choices**，**不支持 stream**。
//   * 编程工具（Cline / Roo / Continue / Claude Code / Aider / OpenAI SDK）一定会发
//     `stream:true`、`tools`、`tool_choice`、`stop`、`n`、`user`、`response_format`……
//     这些字段直传就是 422。
//
// 所以这一层做四件事：
//   1) 剥字段（dropFields）并按白名单重建 body；
//   2) 工具调用模拟（emulateTools）：tools 编译成系统提示里的 JSON 协议，回程解析成
//      OpenAI `tool_calls` + `finish_reason:"tool_calls"`；
//   3) 流式合成（synthesizeStream）：把整段 content 切片成 SSE delta 序列；
//   4) 令牌续期：401/403 时用 refresh_token 换新的 access_token 并写回号池。
//
// 这一层只被 provider-relay 在 `provider.relay.kind === "moonai-llm-proxy"` 时调用，
// 不改变 wavespeed / novita 的既有行为。

const DEFAULT_TOOL_PROTOCOL = [
  "You are running behind an OpenAI-compatible gateway that cannot pass native tool schemas upstream.",
  "Tool calling is emulated with a strict JSON protocol.",
  "When a tool call is required, reply with ONE JSON object and nothing else:",
  '{"tool":"<tool_name>","arguments":{...}}',
  "When no tool call is required, reply with normal prose.",
  "Never wrap the JSON in prose, and never mix prose with a tool object.",
].join("\n");

/** 消息内容既可能是 string，也可能是 part 数组；上游只吃 string。 */
function flattenContent(content) {
  if (typeof content === "string") return content;
  if (Array.isArray(content)) {
    return content
      .map((part) => {
        if (typeof part === "string") return part;
        if (part?.type === "text" || part?.type === "input_text") return String(part.text ?? "");
        if (part?.type === "image_url") return "[image omitted by gateway: upstream text-only]";
        return "";
      })
      .filter(Boolean)
      .join("\n");
  }
  return content == null ? "" : String(content);
}

/** tool schema -> 提示词里的一行签名（参数用 JSON Schema 原文，模型能直接照抄）。 */
function describeTools(tools) {
  const lines = [];
  for (const tool of tools ?? []) {
    const fn = tool?.function ?? tool;
    const name = String(fn?.name ?? "").trim();
    if (!name) continue;
    const description = String(fn?.description ?? "").replace(/\s+/g, " ").slice(0, 400);
    const parameters = fn?.parameters ? JSON.stringify(fn.parameters) : '{"type":"object","properties":{}}';
    lines.push(`- ${name}: ${description || "(no description)"}\n  parameters: ${parameters}`);
  }
  return lines.join("\n");
}

/**
 * OpenAI chat body -> mooonai llm-proxy body。
 *
 * @returns {{payload: object, toolMode: boolean, requestedModel: string}}
 */
export function buildUpstreamPayload(body = {}, provider = {}) {
  const relay = provider.relay ?? {};
  const allowed = new Set(relay.allowedFields ?? ["model", "messages", "temperature", "top_p", "max_tokens"]);

  const messages = [];
  for (const message of Array.isArray(body.messages) ? body.messages : []) {
    const role = String(message?.role ?? "user");
    // 上游只认 system / user / assistant；tool 结果折成 user（附工具名）
    if (role === "tool") {
      messages.push({
        role: "user",
        content: `[tool_result ${message?.tool_call_id ?? ""}]\n${flattenContent(message?.content)}`,
      });
      continue;
    }
    if (role === "developer") {
      messages.push({ role: "system", content: flattenContent(message?.content) });
      continue;
    }
    if (role !== "system" && role !== "user" && role !== "assistant") {
      messages.push({ role: "user", content: flattenContent(message?.content) });
      continue;
    }
    // assistant 上的 tool_calls 要还原成模型能看懂的文本，否则上下文断裂
    const toolCalls = Array.isArray(message?.tool_calls) ? message.tool_calls : [];
    const text = flattenContent(message?.content);
    if (toolCalls.length && !text) {
      const rendered = toolCalls
        .map((call) => `{"tool":"${call?.function?.name ?? ""}","arguments":${call?.function?.arguments ?? "{}"}}`)
        .join("\n");
      messages.push({ role: "assistant", content: rendered });
      continue;
    }
    messages.push({ role, content: text });
  }

  const tools = Array.isArray(body.tools) ? body.tools.filter((t) => (t?.function ?? t)?.name) : [];
  const toolMode = tools.length > 0 && relay.emulateTools !== false;
  if (toolMode) {
    const toolChoice = body.tool_choice;
    let directive = DEFAULT_TOOL_PROTOCOL;
    if (toolChoice === "required") directive += "\nA tool call is mandatory for this turn.";
    else if (typeof toolChoice === "object" && toolChoice?.function?.name) {
      directive += `\nThis turn you MUST call the tool named "${toolChoice.function.name}".`;
    }
    const system = [
      directive,
      "",
      "Available tools:",
      describeTools(tools),
      "",
      'Reminder: the JSON object must use exactly the keys "tool" and "arguments".',
    ].join("\n");
    // 已有的 system 提示保留，工具协议放最前面（模型的注意力更稳）
    const firstSystem = messages.findIndex((m) => m.role === "system");
    if (firstSystem === -1) messages.unshift({ role: "system", content: system });
    else messages[firstSystem] = { role: "system", content: `${system}\n\n${messages[firstSystem].content}` };
  }

  const payload = {};
  for (const field of allowed) {
    if (body[field] === undefined) continue;
    if (field === "messages") {
      payload.messages = messages;
      continue;
    }
    payload[field] = body[field];
  }
  if (!payload.messages) payload.messages = messages;
  if (!payload.model) payload.model = provider.defaultModel || "gpt-4o-mini";

  // 上游没有 stop / n / penalties；max_completion_tokens 统一收敛到 max_tokens
  if (payload.max_completion_tokens !== undefined && payload.max_tokens === undefined) {
    payload.max_tokens = payload.max_completion_tokens;
  }
  delete payload.max_completion_tokens;
  return { payload, toolMode, requestedModel: String(payload.model) };
}

/** 从模型自由文本里抠出唯一的工具调用 JSON（允许 ```json 包裹）。 */
export function parseToolCall(content) {
  const text = String(content ?? "").trim();
  if (!text) return null;
  const candidates = [];
  const fenced = /```(?:json)?\s*([\s\S]*?)```/g;
  let match;
  while ((match = fenced.exec(text))) candidates.push(match[1].trim());
  candidates.push(text);
  // 退一步：抓第一个平衡的 {...}
  const start = text.indexOf("{");
  const end = text.lastIndexOf("}");
  if (start !== -1 && end > start) candidates.push(text.slice(start, end + 1));

  for (const candidate of candidates) {
    let parsed;
    try {
      parsed = JSON.parse(candidate);
    } catch {
      continue;
    }
    if (!parsed || typeof parsed !== "object") continue;
    const name = parsed.tool ?? parsed.name ?? parsed.function?.name;
    if (!name || typeof name !== "string") continue;
    const args = parsed.arguments ?? parsed.args ?? parsed.parameters ?? parsed.function?.arguments ?? {};
    return {
      name,
      arguments: typeof args === "string" ? args : JSON.stringify(args),
    };
  }
  return null;
}

/**
 * 上游 JSON -> OpenAI `chat.completion`。
 * `toolMode` 时先尝试解析工具调用，成功则给 finish_reason="tool_calls"。
 */
export function toOpenAIChatCompletion(upstream = {}, { model, toolMode = false, requestId } = {}) {
  const content = String(upstream.content ?? "");
  const usage = upstream.usage ?? {};
  const created = Math.floor(Date.now() / 1000);
  const id = requestId ?? `chatcmpl-${created.toString(16)}${Math.random().toString(16).slice(2, 8)}`;

  const toolCall = toolMode ? parseToolCall(content) : null;
  const message = toolCall
    ? {
        role: "assistant",
        content: null,
        tool_calls: [
          {
            id: `call_${(created % 1e6).toString(36)}${Math.random().toString(36).slice(2, 8)}`,
            type: "function",
            function: { name: toolCall.name, arguments: toolCall.arguments },
          },
        ],
      }
    : { role: "assistant", content };

  return {
    id,
    object: "chat.completion",
    created,
    model: model || upstream.model || "moonai",
    choices: [
      {
        index: 0,
        message,
        finish_reason: toolCall ? "tool_calls" : upstream.finish_reason === "length" ? "length" : "stop",
        logprobs: null,
      },
    ],
    usage: {
      prompt_tokens: Number(usage.prompt_tokens ?? 0),
      completion_tokens: Number(usage.completion_tokens ?? 0),
      total_tokens: Number(usage.total_tokens ?? 0),
    },
    ...(upstream.model ? { system_fingerprint: `moonai:${upstream.model}` } : {}),
  };
}

/** 上游 JSON -> Anthropic message（/v1/messages 用）。 */
export function toAnthropicMessage(upstream = {}, { model, requestId } = {}) {
  const content = String(upstream.content ?? "");
  const usage = upstream.usage ?? {};
  return {
    id: requestId ?? `msg_${Date.now().toString(16)}`,
    type: "message",
    role: "assistant",
    model: model || upstream.model || "moonai",
    content: content ? [{ type: "text", text: content }] : [],
    stop_reason: upstream.finish_reason === "length" ? "max_tokens" : "end_turn",
    stop_sequence: null,
    usage: {
      input_tokens: Number(usage.prompt_tokens ?? 0),
      output_tokens: Number(usage.completion_tokens ?? 0),
    },
  };
}

const sseChunk = (payload) => `data: ${JSON.stringify(payload)}\n\n`;

/** 把完整的 OpenAI completion 切成 SSE 片段（上游没有流式，只能合成）。 */
export function synthesizeChatSSE(completion, { sliceSize = 24, delayMs = 0 } = {}) {
  const choice = completion.choices?.[0] ?? {};
  const message = choice.message ?? {};
  const chunks = [];
  const base = {
    id: completion.id,
    object: "chat.completion.chunk",
    created: completion.created,
    model: completion.model,
  };

  chunks.push(
    sseChunk({
      ...base,
      choices: [{ index: 0, delta: { role: "assistant", content: "" }, finish_reason: null }],
    }),
  );

  if (choice.finish_reason === "tool_calls" && Array.isArray(message.tool_calls)) {
    message.tool_calls.forEach((call, index) => {
      const args = String(call.function?.arguments ?? "");
      const steps = Math.max(1, Math.ceil(args.length / sliceSize));
      for (let step = 0; step < steps; step++) {
        chunks.push(
          sseChunk({
            ...base,
            choices: [
              {
                index: 0,
                delta: {
                  tool_calls: [
                    {
                      index,
                      id: step === 0 ? call.id : undefined,
                      type: step === 0 ? "function" : undefined,
                      function: {
                        ...(step === 0 ? { name: call.function?.name } : {}),
                        arguments: args.slice(step * sliceSize, (step + 1) * sliceSize),
                      },
                    },
                  ],
                },
                finish_reason: null,
              },
            ],
          }),
        );
      }
    });
  } else {
    const text = String(message.content ?? "");
    for (let offset = 0; offset < text.length; offset += sliceSize) {
      chunks.push(
        sseChunk({
          ...base,
          choices: [
            { index: 0, delta: { content: text.slice(offset, offset + sliceSize) }, finish_reason: null },
          ],
        }),
      );
    }
    if (!text) {
      chunks.push(sseChunk({ ...base, choices: [{ index: 0, delta: { content: "" }, finish_reason: null }] }));
    }
  }

  chunks.push(
    sseChunk({
      ...base,
      choices: [{ index: 0, delta: {}, finish_reason: choice.finish_reason ?? "stop" }],
      ...(completion.usage ? { usage: completion.usage } : {}),
    }),
  );
  chunks.push("data: [DONE]\n\n");
  return { chunks, delayMs };
}

/* ------------------------------------------------------------------ tokens */

/** access_token 快过期就提前续期；返回可能更新过的账号对象。 */
export function needsRefresh(account, withinMs = 10 * 60 * 1000) {
  const expiresAt = Number(account?.accessTokenExpiresAt ?? 0);
  if (!expiresAt) return false;
  return expiresAt - Date.now() <= withinMs;
}

/**
 * 用 refresh_token 换新的 access_token。
 * 上游：POST /api/v1/auth/refresh（Bearer access_token + form refresh_token）。
 */
export async function refreshAccessToken(provider, account, { fetchImpl = fetch, timeoutMs = 20_000 } = {}) {
  const base = (provider?.apiBase || "https://core.mooonai.com").replace(/\/+$/, "");
  const path = provider?.relay?.refreshPath || "/api/v1/auth/refresh";
  const body = new URLSearchParams({ refresh_token: String(account?.refreshToken ?? "") });
  const res = await fetchImpl(`${base}${path}`, {
    method: "POST",
    headers: {
      Authorization: `Bearer ${account?.accessToken ?? ""}`,
      "Content-Type": "application/x-www-form-urlencoded",
      Accept: "application/json",
    },
    body: body.toString(),
    signal: AbortSignal.timeout(timeoutMs),
  });
  const text = await res.text();
  if (!res.ok) {
    const error = new Error(`moonai 续期失败（HTTP ${res.status}）：${text.slice(0, 200)}`);
    error.status = res.status;
    throw error;
  }
  let parsed;
  try {
    parsed = JSON.parse(text);
  } catch {
    throw new Error("moonai 续期返回了非 JSON 响应。");
  }
  const expiresIn = Number(parsed.expires_in ?? 86400);
  return {
    accessToken: String(parsed.access_token ?? ""),
    refreshToken: String(parsed.refresh_token ?? account?.refreshToken ?? ""),
    accessTokenExpiresAt: Date.now() + expiresIn * 1000,
    expiresIn,
  };
}

/** 号池账号 -> 上游鉴权头（moonai 用 access_token，不是 api_ Key）。 */
export function upstreamAuthHeaders(account, provider) {
  const mode = provider?.relay?.authMode ?? "user-token";
  const token = mode === "user-token" ? account?.accessToken || account?.apiKey : account?.apiKey;
  return {
    Authorization: `Bearer ${token ?? ""}`,
    "Content-Type": "application/json",
    Accept: "application/json",
  };
}

/** 反代层自检：用号池里一把 access_token 打一次最小请求。 */
export async function probeMoonai({ provider, account, model, timeoutMs = 30_000, fetchImpl = fetch }) {
  const base = (provider?.apiBase || "https://core.mooonai.com").replace(/\/+$/, "");
  const path = provider?.relay?.path || "/api/v1/services/llm-proxy/chat/completions";
  const started = Date.now();
  try {
    const res = await fetchImpl(`${base}${path}`, {
      method: "POST",
      headers: upstreamAuthHeaders(account, provider),
      body: JSON.stringify({
        model: model || provider?.defaultModel || "gpt-4o-mini",
        messages: [{ role: "user", content: "ping" }],
        max_tokens: 8,
      }),
      signal: AbortSignal.timeout(timeoutMs),
    });
    const text = await res.text();
    return { ok: res.ok, status: res.status, body: text.slice(0, 200), ms: Date.now() - started };
  } catch (cause) {
    return { ok: false, reason: String(cause?.message ?? cause), ms: Date.now() - started };
  }
}
