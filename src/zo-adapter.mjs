// Zo Computer 反代适配器 —— OpenAI / Anthropic 形状 ⇄ `POST /zo/ask`。
//
// 为什么必须单独一层（和 moonai-adapter 同样的理由，但形状完全不同）：
//   * Zo 的公开面只有一个对话入口 `POST /zo/ask`：
//       {input, conversation_id, model_name, persona_id, output_format, memory_mode, stream}
//     入参里**没有** messages / tools / temperature / max_tokens —— 传了要么被忽略、
//     要么被 pydantic 挡掉。多轮上下文在 Zo 侧按 conversation_id 维护，不是把
//     messages 全量塞回去。
//   * 输出也不是 choices：非流式是 `{output, conversation_id}`；流式是
//     SSE 事件 `FrontendModelResponse`（data.content 增量）/ `End`（data.output）/
//     `Error`（data.message），响应头 `x-conversation-id` 给会话 id。
//   * 编程工具一定会发 `stream:true`、`tools`、`tool_choice`、`stop`、`n`……
//
// 这一层做四件事：
//   1) **折叠对话**：把 OpenAI messages 折成一段带角色标记的 input（首轮）；
//      同一会话后续轮只发新的 user 段（有 conversation_id 时），避免重复喂历史。
//   2) **模型名映射**：`zo:openai/gpt-6.2-astra` 这类是上游 id；客户端可能写
//      `gpt-5`、`zo-astra`，按别名表 + 目录前缀兜底。
//   3) **流式翻译**：把 Zo 的事件流翻成 OpenAI `chat.completion.chunk`；上游
//      没开 stream 时用整段 output 合成 SSE。
//   4) **工具语义**：Zo 的 agent 自己会调工具（bash / 读写文件 / 网页…），
//      所以 `tools` 不往上送；但我们把"可用工具"写进 input 的指令块，并在回程
//      解析 `{"tool":…,"arguments":…}`（作者显式要求时）折成 OpenAI tool_calls。
//
// 这一层只在 `provider.relay.kind === "zo-ask"` 时被 provider-relay 调用，
// 不改动 wavespeed / novita / moonai 的既有路径。
import { synthesizeChatSSE as synthesizeFromCompletion } from "./moonai-adapter.mjs";

export const ZO_MODEL_PREFIX = "zo:";

const DEFAULT_MODEL_ALIASES = {
  // 客户端常用短名 → 上游 model_name
  "gpt-5": "zo:openai/gpt-6.2-astra",
  "gpt-5.4": "zo:openai/gpt-6.2-astra",
  astra: "zo:openai/gpt-6.2-astra",
  sol: "zo:openai/gpt-6.2-sol",
  sonnet: "zo:anthropic/claude-sonnet-4",
  opus: "zo:anthropic/claude-opus-4",
  haiku: "zo:anthropic/claude-haiku-4-5",
};

/** 取 provider 里配置的别名表（catalog 可覆盖）。 */
export function modelAliases(provider = {}) {
  return { ...DEFAULT_MODEL_ALIASES, ...(provider.modelAliases ?? {}) };
}

/** 客户端给的 model → 上游 `model_name`；认不出就原样（上游会回 400/回落到默认）。 */
export function resolveModelName(model, provider = {}) {
  const raw = String(model ?? "").trim();
  if (!raw) return provider.defaultModel || "";
  const aliases = modelAliases(provider);
  const hit = aliases[raw] || aliases[raw.toLowerCase()];
  if (hit) return hit;
  if (raw.startsWith(ZO_MODEL_PREFIX)) return raw;
  // 目录里的 id 有时省掉 zo: 前缀（例如 deepai 风格转发），补回来
  if (/^(openai|anthropic|google|xai|deepseek|meta)\//i.test(raw)) return `${ZO_MODEL_PREFIX}${raw}`;
  return raw;
}

/** 消息内容既可能是 string，也可能是 part 数组。 */
function flattenContent(content) {
  if (typeof content === "string") return content;
  if (Array.isArray(content)) {
    return content
      .map((part) => {
        if (typeof part === "string") return part;
        if (part?.type === "text" || part?.type === "input_text") return String(part.text ?? "");
        if (part?.type === "image_url") return "[image omitted: zo /zo/ask accepts text input only]";
        return "";
      })
      .filter(Boolean)
      .join("\n");
  }
  return content == null ? "" : String(content);
}

const ROLE_LABEL = {
  system: "system",
  developer: "system",
  user: "user",
  assistant: "assistant",
  tool: "tool_result",
};

/**
 * 把 OpenAI messages 折成一段 Zo 能读的 input。
 * - 首轮：带角色标记的整段转录（system 放最前，Zo 当成任务约束）
 * - 有 conversation_id 的后续轮：只发最后一段 user（历史在 Zo 那边）
 */
export function foldMessages(messages, { hasConversation = false } = {}) {
  const list = Array.isArray(messages) ? messages : [];
  if (hasConversation) {
    const lastUser = [...list].reverse().find((m) => m?.role === "user");
    if (lastUser) return flattenContent(lastUser.content);
  }
  const blocks = [];
  for (const message of list) {
    const label = ROLE_LABEL[String(message?.role ?? "user")] ?? "user";
    const text = flattenContent(message?.content).trim();
    const toolCalls = Array.isArray(message?.tool_calls) ? message.tool_calls : [];
    if (!text && !toolCalls.length) continue;
    if (toolCalls.length) {
      const rendered = toolCalls
        .map((call) => `{"tool":"${call?.function?.name ?? ""}","arguments":${call?.function?.arguments ?? "{}"}}`)
        .join("\n");
      blocks.push(`[${label}]\n${text ? `${text}\n` : ""}${rendered}`);
      continue;
    }
    blocks.push(`[${label}]\n${text}`);
  }
  return blocks.join("\n\n");
}

/** tools 只用于"告诉 Zo 有这些能力"，不指望它按 OpenAI 的 function schema 回。 */
function describeTools(tools) {
  const lines = [];
  for (const tool of tools ?? []) {
    const fn = tool?.function ?? tool;
    const name = String(fn?.name ?? "").trim();
    if (!name) continue;
    const description = String(fn?.description ?? "").replace(/\s+/g, " ").slice(0, 300);
    lines.push(`- ${name}: ${description || "(no description)"}`);
  }
  return lines.join("\n");
}

/**
 * OpenAI chat body → Zo `/zo/ask` body。
 *
 * @returns {{payload: object, toolMode: boolean, requestedModel: string, conversationId: string|null}}
 */
export function buildZoAskPayload(body = {}, provider = {}, { conversationId = null } = {}) {
  const relay = provider.relay ?? {};
  const messages = Array.isArray(body.messages) ? body.messages : [];
  const conversation = conversationId || (typeof body.conversation_id === "string" ? body.conversation_id : null);

  let input = foldMessages(messages, { hasConversation: Boolean(conversation) });

  const tools = Array.isArray(body.tools) ? body.tools.filter((t) => (t?.function ?? t)?.name) : [];
  const toolMode = tools.length > 0 && relay.emulateTools !== false;
  if (toolMode) {
    const directive = [
      "Tool protocol (gateway emulation): when a tool call is the right next step,",
      'reply with exactly one JSON object {"tool":"<name>","arguments":{...}} and nothing else.',
      "Otherwise answer normally. Available tools:",
      describeTools(tools),
    ].join("\n");
    input = `${directive}\n\n${input}`;
  }

  const payload = {
    input,
    conversation_id: conversation,
    model_name: body.model ? resolveModelName(body.model, provider) : null,
    persona_id: typeof body.persona_id === "string" ? body.persona_id : null,
    stream: body.stream === true,
  };
  // 结构化输出（部分客户端用 response_format 表达）
  const format = body.output_format ?? body.response_format?.json_schema?.schema ?? null;
  if (format && typeof format === "object") payload.output_format = format;
  if (body.memory_mode === "off" || body.memory === "off") payload.memory_mode = "off";
  if (!payload.model_name) delete payload.model_name;
  // allowedFields 只用于文档与自检；这里已经是最小集合，不放宽
  return { payload, toolMode, requestedModel: payload.model_name ?? provider.defaultModel ?? "", conversationId: payload.conversation_id };
}

/* ------------------------------------------------------------------ 响应 */

/** 从 Zo 的 output（string | object）取文本。 */
function outputText(output) {
  if (output == null) return "";
  if (typeof output === "string") return output;
  try {
    return JSON.stringify(output);
  } catch {
    return String(output);
  }
}

/** 从模型自由文本里抠工具调用 JSON（允许 ```json 包裹）。 */
export function parseToolCall(content) {
  const text = String(content ?? "").trim();
  if (!text) return null;
  const candidates = [];
  const fenced = /```(?:json)?\s*([\s\S]*?)```/g;
  let match;
  while ((match = fenced.exec(text))) candidates.push(match[1].trim());
  candidates.push(text);
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
    const args = parsed.arguments ?? parsed.args ?? parsed.parameters ?? {};
    return { name, arguments: typeof args === "string" ? args : JSON.stringify(args) };
  }
  return null;
}

/** Zo `/zo/ask` JSON → OpenAI `chat.completion`。 */
export function toOpenAIChatCompletion(upstream = {}, { model, toolMode = false, requestId, conversationId } = {}) {
  const content = outputText(upstream.output);
  const created = Math.floor(Date.now() / 1000);
  const id = requestId ?? `chatcmpl-zo-${created.toString(16)}${Math.random().toString(16).slice(2, 8)}`;
  const toolCall = toolMode ? parseToolCall(content) : null;
  if (upstream.error) {
    const error = new Error(String(upstream.error));
    error.status = 502;
    throw error;
  }
  return {
    id,
    object: "chat.completion",
    created,
    model: model || "zo",
    choices: [
      {
        index: 0,
        message: toolCall
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
          : { role: "assistant", content },
        finish_reason: toolCall ? "tool_calls" : "stop",
        logprobs: null,
      },
    ],
    usage: { prompt_tokens: 0, completion_tokens: 0, total_tokens: 0 },
    zo_conversation_id: upstream.conversation_id ?? conversationId ?? null,
  };
}

/** Zo `/zo/ask` JSON → Anthropic message。 */
export function toAnthropicMessage(upstream = {}, { model, requestId, conversationId } = {}) {
  const content = outputText(upstream.output);
  if (upstream.error) {
    const error = new Error(String(upstream.error));
    error.status = 502;
    throw error;
  }
  return {
    id: requestId ?? `msg_zo_${Date.now().toString(16)}`,
    type: "message",
    role: "assistant",
    model: model || "zo",
    content: content ? [{ type: "text", text: content }] : [],
    stop_reason: "end_turn",
    stop_sequence: null,
    usage: { input_tokens: 0, output_tokens: 0 },
    ...(conversationId || upstream.conversation_id
      ? { zo_conversation_id: conversationId ?? upstream.conversation_id }
      : {}),
  };
}

const sseChunk = (payload) => `data: ${JSON.stringify(payload)}\n\n`;

/** 完整 completion → OpenAI SSE（上游非流式时用）。 */
export function synthesizeChatSSE(completion) {
  return synthesizeFromCompletion(completion);
}

/**
 * 解析 Zo 的 SSE 事件流，产出 `{type, ...}`：
 *   {type:"delta", text}            FrontendModelResponse
 *   {type:"end", output, conversationId}
 *   {type:"error", message}
 *
 * 上游事件名在 `event:` 行，数据在 `data:` 行（JSON）；也有只给 data 的情况。
 */
export async function* iterateZoEvents(body) {
  const decoder = new TextDecoder();
  let buffer = "";
  let eventName = "";
  for await (const chunk of body) {
    buffer += decoder.decode(chunk, { stream: true });
    let index;
    while ((index = buffer.indexOf("\n")) !== -1) {
      const line = buffer.slice(0, index).replace(/\r$/, "");
      buffer = buffer.slice(index + 1);
      if (!line.trim()) {
        eventName = "";
        continue;
      }
      if (line.startsWith("event:")) {
        eventName = line.slice(6).trim();
        continue;
      }
      if (!line.startsWith("data:")) continue;
      const raw = line.slice(5).trim();
      if (!raw || raw === "[DONE]") continue;
      let data;
      try {
        data = JSON.parse(raw);
      } catch {
        continue;
      }
      const type = String(data.type ?? eventName ?? "").toLowerCase();
      if (type.includes("frontendmodelresponse") || (data.content !== undefined && !data.output)) {
        yield { type: "delta", text: String(data.content ?? "") };
        continue;
      }
      if (type === "end" || data.output !== undefined) {
        yield {
          type: "end",
          output: data.output ?? null,
          conversationId: data.conversation_id ?? data.conversationId ?? null,
        };
        continue;
      }
      if (type === "error" || data.message) {
        yield { type: "error", message: String(data.message ?? "zo stream error") };
      }
    }
  }
}

/** 把 Zo 事件流翻成 OpenAI SSE 字节串（relay 直接写进 res）。 */
export async function* zoEventsToOpenAIChunks(events, { model, requestId } = {}) {
  const created = Math.floor(Date.now() / 1000);
  const id = requestId ?? `chatcmpl-zo-${created.toString(16)}`;
  const base = { id, object: "chat.completion.chunk", created, model: model || "zo" };
  yield sseChunk({ ...base, choices: [{ index: 0, delta: { role: "assistant", content: "" }, finish_reason: null }] });
  let sawText = false;
  let final = null;
  for await (const event of events) {
    if (event.type === "delta" && event.text) {
      sawText = true;
      yield sseChunk({ ...base, choices: [{ index: 0, delta: { content: event.text }, finish_reason: null }] });
      continue;
    }
    if (event.type === "end") {
      final = event;
      // 上游只在 End 里给整段 output 的情况：补发一次全文
      if (!sawText) {
        const text = outputText(event.output);
        if (text) {
          sawText = true;
          yield sseChunk({ ...base, choices: [{ index: 0, delta: { content: text }, finish_reason: null }] });
        }
      }
      continue;
    }
    if (event.type === "error") {
      yield sseChunk({ error: { message: event.message, type: "upstream_error" } });
      yield "data: [DONE]\n\n";
      return;
    }
  }
  yield sseChunk({
    ...base,
    choices: [{ index: 0, delta: {}, finish_reason: "stop" }],
    ...(final?.conversationId ? { zo_conversation_id: final.conversationId } : {}),
  });
  yield "data: [DONE]\n\n";
}

/* ------------------------------------------------------------------ tokens */

/** 号池账号 → 上游鉴权头。Zo 用 access token（zo_sk_…），不是 OpenAI Key。 */
export function upstreamAuthHeaders(account, provider) {
  const mode = provider?.relay?.authMode ?? "user-token";
  const token = mode === "user-token" ? account?.accessToken || account?.apiKey : account?.apiKey;
  return {
    Authorization: `Bearer ${token ?? ""}`,
    "Content-Type": "application/json",
    Accept: "application/json, text/event-stream",
  };
}

export function needsRefresh(account, withinMs = 10 * 60 * 1000) {
  const expiresAt = Number(account?.accessTokenExpiresAt ?? 0);
  if (!expiresAt) return false;
  return expiresAt - Date.now() <= withinMs;
}

/**
 * 续期。Zo 的 access/refresh 是 **HttpOnly cookie**（Domain=.zo.computer），
 * 网页端走 `POST /api/auth/refresh`；API 侧还留了 `${apiBase}/zo/refresh_token`。
 * 两种都试一次，谁通用谁 —— 两条路径都不通时抛错，让上层换号。
 */
export async function refreshAccessToken(provider, account, { fetchImpl = fetch, timeoutMs = 20_000 } = {}) {
  const cookie = typeof account?.cookies === "string"
    ? account.cookies
    : Array.isArray(account?.cookies)
      ? account.cookies.map((c) => `${c.name}=${c.value}`).join("; ")
      : "";
  const panel = (provider?.panelBase || "https://www.zo.computer").replace(/\/+$/, "");
  const api = (provider?.apiBase || "https://api.zo.computer").replace(/\/+$/, "");
  const attempts = [
    { url: `${panel}/api/auth/refresh`, method: "POST" },
    { url: `${api}/zo/refresh_token`, method: "POST" },
  ];
  let last = null;
  for (const attempt of attempts) {
    try {
      const res = await fetchImpl(attempt.url, {
        method: attempt.method,
        headers: {
          Accept: "application/json",
          "Content-Type": "application/json",
          ...(cookie ? { Cookie: cookie } : {}),
          ...(account?.accessToken ? { Authorization: `Bearer ${account.accessToken}` } : {}),
        },
        body: JSON.stringify({ refresh_token: account?.refreshToken ?? "" }),
        signal: AbortSignal.timeout(timeoutMs),
      });
      const text = await res.text();
      if (!res.ok) {
        last = new Error(`zo 续期失败（HTTP ${res.status}）：${text.slice(0, 160)}`);
        continue;
      }
      let parsed = {};
      try {
        parsed = JSON.parse(text);
      } catch {
        parsed = {};
      }
      const token = String(parsed.access_token ?? parsed.accessToken ?? "");
      if (!token) {
        last = new Error(`zo 续期返回里没有 access_token：${text.slice(0, 160)}`);
        continue;
      }
      const expiresIn = Number(parsed.expires_in ?? parsed.expiresIn ?? 86400);
      return {
        accessToken: token,
        refreshToken: String(parsed.refresh_token ?? account?.refreshToken ?? ""),
        accessTokenExpiresAt: Date.now() + expiresIn * 1000,
        expiresIn,
      };
    } catch (cause) {
      last = cause;
    }
  }
  throw last ?? new Error("zo 续期失败");
}

/* -------------------------------------------------------------------- MCP */

let mcpRequestId = 1;

/**
 * 直接调 Zo 的 MCP 端点（HTTP transport，JSON-RPC 2.0）。
 * 编程工具通过网关的 `/p/zo/mcp` 走到这里，不用自己配 header。
 */
export async function zoMcpCall(provider, account, method, params = {}, { fetchImpl = fetch, timeoutMs = 60_000, sessionId = "" } = {}) {
  const api = (provider?.apiBase || "https://api.zo.computer").replace(/\/+$/, "");
  const url = `${api}${provider?.mcpPath || "/mcp"}`;
  const id = mcpRequestId++;
  const res = await fetchImpl(url, {
    method: "POST",
    headers: {
      ...upstreamAuthHeaders(account, provider),
      ...(sessionId ? { "Mcp-Session-Id": sessionId } : {}),
    },
    body: JSON.stringify({ jsonrpc: "2.0", id, method, params }),
    signal: AbortSignal.timeout(timeoutMs),
  });
  const text = await res.text();
  let parsed;
  try {
    parsed = JSON.parse(text);
  } catch {
    const error = new Error(`zo mcp 返回非 JSON（HTTP ${res.status}）：${text.slice(0, 200)}`);
    error.status = res.status;
    throw error;
  }
  if (parsed.error) {
    const error = new Error(`zo mcp ${method} 失败：${parsed.error.message ?? JSON.stringify(parsed.error).slice(0, 160)}`);
    error.status = res.status === 200 ? 502 : res.status;
    error.code = parsed.error.code;
    throw error;
  }
  return { result: parsed.result, sessionId: res.headers.get("mcp-session-id") ?? sessionId };
}

/** 列 Zo 暴露的 MCP 工具（bash / 文件 / 网页 / 图像…）。 */
export async function zoMcpListTools(provider, account, options = {}) {
  const { result } = await zoMcpCall(provider, account, "tools/list", {}, options);
  const tools = Array.isArray(result?.tools) ? result.tools : [];
  return tools.map((tool) => ({
    name: String(tool.name ?? ""),
    description: String(tool.description ?? ""),
    inputSchema: tool.inputSchema ?? { type: "object", properties: {} },
  }));
}

/** 调一个 Zo MCP 工具（编程工具用它跑 shell / 读写文件）。 */
export async function zoMcpCallTool(provider, account, name, args = {}, options = {}) {
  const { result } = await zoMcpCall(
    provider,
    account,
    "tools/call",
    { name: String(name), arguments: args && typeof args === "object" ? args : {} },
    options,
  );
  return result;
}

/* -------------------------------------------------------------------- 自检 */

/** 用一把 token 打 /models/available + 一次最小 /zo/ask（可选）。 */
export async function probeZo({ provider, account, model, ask = false, timeoutMs = 40_000, fetchImpl = fetch }) {
  const api = (provider?.apiBase || "https://api.zo.computer").replace(/\/+$/, "");
  const started = Date.now();
  try {
    const res = await fetchImpl(`${api}/models/available`, {
      headers: upstreamAuthHeaders(account, provider),
      signal: AbortSignal.timeout(timeoutMs),
    });
    const text = await res.text();
    if (!res.ok) {
      return { ok: false, status: res.status, body: text.slice(0, 200), ms: Date.now() - started };
    }
    let models = [];
    try {
      models = JSON.parse(text).models ?? [];
    } catch {
      models = [];
    }
    if (!ask) {
      return { ok: true, status: res.status, models: models.length, ms: Date.now() - started };
    }
    const askRes = await fetchImpl(`${api}/zo/ask`, {
      method: "POST",
      headers: upstreamAuthHeaders(account, provider),
      body: JSON.stringify({ input: "ping", model_name: model || provider?.defaultModel || null, stream: false }),
      signal: AbortSignal.timeout(timeoutMs),
    });
    const askText = await askRes.text();
    return {
      ok: askRes.ok,
      status: askRes.status,
      models: models.length,
      body: askText.slice(0, 200),
      ms: Date.now() - started,
    };
  } catch (cause) {
    return { ok: false, reason: String(cause?.message ?? cause), ms: Date.now() - started };
  }
}

/** 模型列表归一：Zo 的 models/available → OpenAI `model` 数组。 */
export function toOpenAIModelList(payload = {}) {
  const rows = Array.isArray(payload.models) ? payload.models : [];
  return rows.map((row) => ({
    id: String(row.model_name ?? row.id ?? ""),
    object: "model",
    owned_by: String(row.vendor ?? "zo").toLowerCase().replace(/\s+/g, "-"),
    ...(row.label ? { label: row.label } : {}),
    ...(row.context_window ? { context_window: row.context_window } : {}),
    ...(row.type ? { tier: row.type } : {}),
  })).filter((row) => row.id);
}