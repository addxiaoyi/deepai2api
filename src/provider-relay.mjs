// provider 网页反代层 —— 把号池里的面板 API Key 对外变成 OpenAI 兼容接口。
//
// 路由形态（挂在网关自己的 HTTP server 上，不另起进程、不占新端口）：
//
//   GET  /p/<provider>/v1/models                    列该 provider 号池可用的模型
//   POST /p/<provider>/v1/chat/completions          OpenAI 兼容对话
//   POST /p/<provider>/v1/completions                OpenAI 兼容补全
//   POST /p/<provider>/v1/responses                  OpenAI Responses 兼容
//   POST /p/<provider>/v1/messages                   Anthropic 兼容
//   POST /p/<provider>/v1/images/generations         OpenAI 兼容图像（同步）
//   POST /p/<provider>/v1/async/<task-path>           异步任务（wavespeed / novita）
//   GET  /p/<provider>/v1/async/result?id=...         异步任务轮询
//
// 鉴权两段：
//   1) 客户端侧：网关自己的 `sk-dcg-…` Key（和 /v1/* 一致），保证计费与限额走同一条路；
//   2) 上游侧：号池里轮询出来的面板 Key（`Authorization: Bearer <panel key>`）。
//
// 失败自动换号：401/402/403/429/5xx 都会换下一个账号重试（最多 N 个），
// 并把结果回报号池（recordResult），失败账号自动进入冷却。
import { messagesToChat, chatToMessage, streamAsAnthropic } from "./anthropic.mjs";
import { hashKey } from "./keys.mjs";
import { resolveEndpoints } from "./provider-catalog.mjs";
import {
  buildUpstreamPayload,
  needsRefresh,
  refreshAccessToken,
  synthesizeChatSSE,
  toAnthropicMessage,
  toOpenAIChatCompletion,
  upstreamAuthHeaders,
} from "./moonai-adapter.mjs";
import {
  buildZoAskPayload,
  needsRefresh as zoNeedsRefresh,
  refreshAccessToken as zoRefreshToken,
  toAnthropicMessage as zoToAnthropicMessage,
  toOpenAIChatCompletion as zoToCompletion,
  toOpenAIModelList,
  upstreamAuthHeaders as zoAuthHeaders,
  zoEventsToOpenAIChunks,
  zoMcpCall,
  iterateZoEvents,
} from "./zo-adapter.mjs";

const RETRYABLE = new Set([401, 402, 403, 429, 500, 502, 503, 504]);

const clampInt = (value, fallback, min, max) => {
  const parsed = Number(value);
  if (!Number.isFinite(parsed)) return fallback;
  return Math.min(max, Math.max(min, Math.trunc(parsed)));
};

const readJsonBody = async (req, limit = 32 * 1024 * 1024) => {
  const chunks = [];
  let size = 0;
  for await (const chunk of req) {
    size += chunk.length;
    if (size > limit) {
      const error = new Error("Request body too large.");
      error.status = 413;
      throw error;
    }
    chunks.push(chunk);
  }
  const text = Buffer.concat(chunks).toString("utf8");
  if (!text.trim()) return {};
  try {
    return JSON.parse(text);
  } catch {
    const error = new Error("Request body is not valid JSON.");
    error.status = 400;
    throw error;
  }
};

const sendJson = (res, status, value, extraHeaders = {}) => {
  const body = Buffer.from(JSON.stringify(value));
  res.writeHead(status, {
    "Content-Type": "application/json",
    "Content-Length": String(body.length),
    "Cache-Control": "no-store",
    ...extraHeaders,
  });
  res.end(body);
};

const errorBody = (message, type = "invalid_request_error", code = null) => ({
  error: { message, type, ...(code ? { code } : {}) },
});

/**
 * 反代层。
 *
 * @param {{pools: Map<string, object>, stores: object, config: object, log?: Function}} options
 */
export function createProviderRelay({ pools, store, config, log = () => {} } = {}) {
  /**
   * 面板 Key → 上游请求。
   * 两个 provider 的 OpenAI 兼容前缀不同（wavespeed: /v1；novita: /openai/v1），
   * 这里统一按 provider 描述符里的 apiBase 拼，body 原样透传（保留
   * enable_thinking / separate_reasoning 等私有扩展字段）。
   */
  async function upstreamFetch(provider, account, { path, body, method = "POST", headers = {}, signal, base: baseOverride = "" }) {
    const endpoints = resolveEndpoints(provider);
    const base = (baseOverride || endpoints.apiBase || "").replace(/\/+$/, "");
    if (!base) {
      const error = new Error(`${provider.id} 没有配置 apiBase，无法反代。`);
      error.status = 500;
      throw error;
    }
    const url = `${base}${path.startsWith("/") ? path : `/${path}`}`;
    const requestHeaders = {
      "Content-Type": "application/json",
      Accept: "application/json, text/event-stream",
      Authorization: `Bearer ${account.apiKey}`,
      ...headers,
    };
    if (body !== undefined && body !== null) requestHeaders["Content-Type"] = "application/json";
    return fetch(url, {
      method,
      headers: requestHeaders,
      body: body === undefined || body === null ? undefined : JSON.stringify(body),
      signal,
      redirect: "error",
    });
  }

  /** 在号池里轮询一个可用账号。 */
  function pickAccount(providerId, exclude = []) {
    const pool = pools.get(providerId);
    if (!pool) return null;
    return pool.pick({ exclude });
  }

  /**
   * 带换号重试的上游调用。
   * 每次失败都回报号池：成功清冷却，失败累计失败次数并进入冷却。
   */
  async function callWithFailover({ providerId, path, body, method = "POST", headers, signal, maxAttempts = 3, base = "" }) {
    const pool = pools.get(providerId);
    if (!pool) {
      const error = new Error(`provider ${providerId} 没有号池。`);
      error.status = 500;
      throw error;
    }
    if (!pool.healthy().length) {
      const error = new Error(`${providerId} 号池里没有可用账号（未注册 / 冷却中 / 已失效）。`);
      error.status = 503;
      error.code = "no_account";
      throw error;
    }

    const exclude = [];
    let lastError = null;
    const attempts = Math.min(maxAttempts, Math.max(1, pool.healthy().length));

    for (let attempt = 0; attempt < attempts; attempt++) {
      const account = pickAccount(providerId, exclude);
      if (!account) break;
      exclude.push(account.id);
      try {
        const res = await upstreamFetch(pool.provider, account, {
          path,
          body,
          method,
          headers,
          signal,
          base,
        });
        if (!RETRYABLE.has(res.status)) {
          pool.recordResult(account.id, { ok: true });
          return { res, account };
        }
        const text = await res.text().catch(() => "");
        const detail = text.slice(0, 300);
        pool.recordResult(account.id, {
          ok: false,
          error: `HTTP ${res.status}: ${detail}`,
          cooldownMs: res.status === 429 ? 60_000 : 30_000,
        });
        lastError = Object.assign(new Error(`${providerId} 上游拒绝请求（HTTP ${res.status}）。`), {
          status: res.status,
          body: detail,
        });
        log("warn", `${providerId} account ${account.email} failed (${res.status}), rotating`);
      } catch (cause) {
        if (signal?.aborted) throw cause;
        pool.recordResult(account.id, {
          ok: false,
          error: String(cause?.message ?? cause).slice(0, 200),
          cooldownMs: 20_000,
        });
        lastError = cause;
        log("warn", `${providerId} account ${account.email} transport error: ${cause?.message ?? cause}`);
      }
    }

    if (lastError) {
      lastError.status = lastError.status && lastError.status >= 400 ? lastError.status : 502;
      throw lastError;
    }
    const error = new Error(`${providerId} 号池里没有可用账号。`);
    error.status = 503;
    error.code = "no_account";
    throw error;
  }

  /** 把上游响应原样回给客户端（支持 SSE 流）。 */
  async function relayUpstream(res, upstream, tag = "provider-relay") {
    const contentType = upstream.headers.get("content-type") ?? "application/json";
    if (!upstream.body) {
      res.writeHead(upstream.status, { "Content-Type": contentType, "Cache-Control": "no-store" });
      res.end();
      return;
    }
    res.writeHead(upstream.status, {
      "Content-Type": contentType,
      "Cache-Control": "no-store",
      "X-Accel-Buffering": "no",
      ...(upstream.headers.has("retry-after") ? { "Retry-After": upstream.headers.get("retry-after") } : {}),
    });
    try {
      for await (const chunk of upstream.body) {
        if (res.writableEnded) break;
        if (!res.write(chunk)) {
          await new Promise((done) => {
            res.once("drain", done);
            res.once("close", done);
          });
        }
      }
    } catch (cause) {
      if (!res.writableEnded) {
        log("error", `${tag} stream failed: ${cause?.message ?? cause}`);
        res.destroy();
        return;
      }
    }
    if (!res.writableEnded) res.end();
  }

  /**
   * moonai 专用反代：上游是自有形状（无 choices / 无 stream / 不接受 tools），
   * 由 src/moonai-adapter.mjs 翻译。挂在 `/p/moonai/v1/*` 下，与其它 provider 并列。
   */
  async function handleMoonai({ req, res, rest, provider, pool, controller }) {
    const relay = provider.relay ?? {};
    const base = (resolveEndpoints(provider).apiBase || "").replace(/\/+$/, "");
    if (!base) {
      sendJson(res, 500, errorBody("moonai 没有配置 apiBase，无法反代。", "invalid_request_error", "no_api_base"));
      return true;
    }

    // ---- 模型列表：上游没有 /models，用描述符里的白名单 ----
    if (req.method === "GET" && rest === "/v1/models") {
      const ids = (provider.models?.length ? provider.models : [provider.defaultModel]).filter(Boolean);
      sendJson(res, 200, {
        object: "list",
        data: ids.map((id) => ({ id, object: "model", owned_by: provider.id })),
      });
      return true;
    }
    if (req.method !== "POST") {
      sendJson(res, 405, errorBody(`方法不支持：${req.method} ${rest}`, "invalid_request_error", "method_not_allowed"));
      return true;
    }

    const body = await readJsonBody(req);
    const anthropic = rest === "/v1/messages";
    const chatBody = anthropic ? messagesToChat(body, { model: body.model }) : body;
    const { payload, toolMode } = buildUpstreamPayload(chatBody, provider);
    const wantedStream = body.stream === true;
    const url = `${base}${relay.path || "/api/v1/services/llm-proxy/chat/completions"}`;

    if (!pool.healthy().length) {
      sendJson(
        res,
        503,
        errorBody("moonai 号池里没有可用账号（未注册 / 冷却中 / 已失效）。", "upstream_error", "no_account"),
      );
      return true;
    }

    const exclude = [];
    let lastError = null;
    let upstream = null;
    let used = null;
    const attempts = Math.min(3, Math.max(1, pool.healthy().length));

    for (let attempt = 0; attempt < attempts; attempt++) {
      const account = pool.pick({ exclude });
      if (!account) break;
      exclude.push(account.id);

      let current = account;
      if (needsRefresh(current, relay.refreshWithinMs)) {
        try {
          current = pool.update(current.id, await refreshAccessToken(provider, current)) ?? current;
        } catch (cause) {
          log("warn", `moonai 预续期失败 ${account.email}：${cause?.message ?? cause}`);
        }
      }

      let attemptRes = null;
      try {
        attemptRes = await fetch(url, {
          method: "POST",
          headers: upstreamAuthHeaders(current, provider),
          body: JSON.stringify(payload),
          signal: controller.signal,
        });
        // 令牌过期：续一次再打
        if (attemptRes.status === 401 || attemptRes.status === 403) {
          const stale = await attemptRes.text().catch(() => "");
          try {
            current = pool.update(current.id, await refreshAccessToken(provider, current)) ?? current;
            attemptRes = await fetch(url, {
              method: "POST",
              headers: upstreamAuthHeaders(current, provider),
              body: JSON.stringify(payload),
              signal: controller.signal,
            });
          } catch (cause) {
            log("warn", `moonai 续期失败 ${account.email}（${stale.slice(0, 80)}）：${cause?.message ?? cause}`);
          }
        }
      } catch (cause) {
        if (controller.signal.aborted) return true;
        pool.recordResult(account.id, { ok: false, error: String(cause?.message ?? cause), cooldownMs: 20_000 });
        lastError = cause;
        continue;
      }

      if (attemptRes.ok) {
        upstream = attemptRes;
        used = current;
        break;
      }

      const detail = await attemptRes.text().catch(() => "");
      pool.recordResult(account.id, {
        ok: false,
        error: `HTTP ${attemptRes.status}: ${detail.slice(0, 160)}`,
        cooldownMs: attemptRes.status === 429 ? 60_000 : 30_000,
      });
      lastError = Object.assign(new Error(`moonai 上游拒绝请求（HTTP ${attemptRes.status}）。`), {
        status: attemptRes.status,
        body: detail.slice(0, 300),
      });
      log("warn", `moonai account ${account.email} failed (${attemptRes.status}), rotating`);
    }

    if (!upstream || !used) {
      if (lastError) {
        throw Object.assign(lastError, { status: lastError.status ?? 502 });
      }
      sendJson(res, 503, errorBody("moonai 号池里没有可用账号。", "upstream_error", "no_account"));
      return true;
    }

    pool.recordResult(used.id, { ok: true });
    res.setHeader("X-Gateway-Provider", provider.id);
    res.setHeader("X-Gateway-Account", String(used.id));

    const text = await upstream.text();
    let parsed;
    try {
      parsed = JSON.parse(text);
    } catch {
      sendJson(res, 502, errorBody(`moonai 返回了非 JSON 响应：${text.slice(0, 200)}`, "upstream_error"));
      return true;
    }

    if (anthropic) {
      const message = toAnthropicMessage(parsed, { model: body.model });
      if (wantedStream) {
        // 上游没有真流：先把 OpenAI SSE 合成出来，再交给现有的 Anthropic 事件转换器
        const completion = toOpenAIChatCompletion(parsed, { model: body.model });
        const { chunks } = synthesizeChatSSE(completion);
        const source = (async function* () {
          yield Buffer.from(chunks.join(""), "utf8");
        })();
        res.writeHead(200, {
          "Content-Type": "text/event-stream; charset=utf-8",
          "Cache-Control": "no-store",
          "X-Accel-Buffering": "no",
        });
        for await (const chunk of streamAsAnthropic(source, { model: body.model })) {
          if (res.writableEnded) break;
          res.write(chunk);
        }
        if (!res.writableEnded) res.end();
        return true;
      }
      sendJson(res, 200, message);
      return true;
    }

    const completion = toOpenAIChatCompletion(parsed, { model: body.model, toolMode });
    if (!wantedStream) {
      sendJson(res, 200, completion);
      return true;
    }

    const { chunks } = synthesizeChatSSE(completion);
    res.writeHead(200, {
      "Content-Type": "text/event-stream; charset=utf-8",
      "Cache-Control": "no-store",
      "X-Accel-Buffering": "no",
      Connection: "keep-alive",
    });
    for (const chunk of chunks) {
      if (res.writableEnded) break;
      res.write(chunk);
    }
    if (!res.writableEnded) res.end();
    return true;
  }

  /**
   * zo 专用反代：上游只有 `/zo/ask`（自有入参）+ `/mcp`（JSON-RPC），
   * 翻译在 src/zo-adapter.mjs。挂在 `/p/zo/v1/*` 与 `/p/zo/mcp`。
   */
  async function handleZo({ req, res, rest, provider, pool, controller, url }) {
    const api = (resolveEndpoints(provider).apiBase || "").replace(/\/+$/, "");
    const relay = provider.relay ?? {};
    if (!api) {
      sendJson(res, 500, errorBody("zo 没有配置 apiBase，无法反代。", "invalid_request_error", "no_api_base"));
      return true;
    }
    const askPath = relay.path || "/zo/ask";

    /** 取一个可用账号；带 401/403 续期一次。 */
    async function withAccount(exclude, run) {
      const account = pool.pick({ exclude });
      if (!account) return { skipped: true };
      let current = account;
      if (zoNeedsRefresh(current, relay.refreshWithinMs)) {
        try {
          current = pool.update(current.id, await zoRefreshToken(provider, current)) ?? current;
        } catch (cause) {
          log("warn", `zo 预续期失败 ${account.email}：${cause?.message ?? cause}`);
        }
      }
      let result = await run(current);
      if (result && (result.status === 401 || result.status === 403)) {
        try {
          current = pool.update(current.id, await zoRefreshToken(provider, current)) ?? current;
          result = await run(current, { retry: true });
        } catch (cause) {
          log("warn", `zo 续期失败 ${account.email}：${cause?.message ?? cause}`);
        }
      }
      return { account: current, result };
    }

    // ---- MCP：给编程工具直接调 Zo 的工具（bash / 文件 / 网页…）----
    if (rest === "/mcp" || rest === "/v1/mcp") {
      if (req.method !== "POST") {
        sendJson(res, 405, errorBody("zo MCP 只接受 POST（JSON-RPC 2.0）。", "invalid_request_error", "method_not_allowed"));
        return true;
      }
      const body = await readJsonBody(req);
      if (!Array.isArray(body) || !body.length) {
        sendJson(res, 400, errorBody("MCP 请求体必须是 JSON-RPC 消息或消息数组。", "invalid_request_error", "bad_mcp_body"));
        return true;
      }
      if (!pool.healthy().length) {
        sendJson(res, 503, errorBody("zo 号池里没有可用账号。", "upstream_error", "no_account"));
        return true;
      }
      const sessionId = String(req.headers["mcp-session-id"] ?? "");
      const exclude = [];
      let lastError = null;
      const attempts = Math.min(3, Math.max(1, pool.healthy().length));
      for (let attempt = 0; attempt < attempts; attempt++) {
        const account = pool.pick({ exclude });
        if (!account) break;
        exclude.push(account.id);
        try {
          const out = [];
          let returnedSession = sessionId;
          for (const message of body) {
            const method = String(message?.method ?? "");
            // 握手类请求逐个转发；通知（无 id）也转发但没有响应
            const response = await fetch(`${api}${relay.mcpPath || "/mcp"}`, {
              method: "POST",
              headers: {
                ...zoAuthHeaders(account, provider),
                ...(returnedSession ? { "Mcp-Session-Id": returnedSession } : {}),
              },
              body: JSON.stringify(message),
              signal: controller.signal,
            });
            returnedSession = response.headers.get("mcp-session-id") ?? returnedSession;
            const text = await response.text();
            if (!text.trim()) continue;
            out.push(JSON.parse(text));
            if (!response.ok) {
              const detail = text.slice(0, 200);
              pool.recordResult(account.id, {
                ok: false,
                error: `HTTP ${response.status}: ${detail}`,
                cooldownMs: response.status === 429 ? 60_000 : 30_000,
              });
              lastError = Object.assign(new Error(`zo MCP 上游拒绝（HTTP ${response.status}）。`), { status: response.status });
              throw lastError;
            }
          }
          pool.recordResult(account.id, { ok: true });
          const payload = out.length === 1 ? out[0] : out;
          const raw = Buffer.from(JSON.stringify(payload));
          res.writeHead(200, {
            "Content-Type": "application/json",
            "Content-Length": String(raw.length),
            "Cache-Control": "no-store",
            "X-Gateway-Provider": provider.id,
            "X-Gateway-Account": String(account.id),
            ...(returnedSession ? { "Mcp-Session-Id": returnedSession } : {}),
          });
          res.end(raw);
          return true;
        } catch (cause) {
          if (controller.signal.aborted) return true;
          lastError = lastError ?? cause;
          log("warn", `zo mcp account ${account.email} failed: ${cause?.message ?? cause}`);
        }
      }
      throw Object.assign(lastError ?? new Error("zo MCP 无可用账号。"), {
        status: lastError?.status ?? 503,
        code: "no_account",
      });
    }

    // ---- 模型列表 ----
    if (req.method === "GET" && rest === "/v1/models") {
      const account = pool.pick({});
      if (!account) {
        const ids = (provider.models?.length ? provider.models : [provider.defaultModel]).filter(Boolean);
        sendJson(res, 200, { object: "list", data: ids.map((id) => ({ id, object: "model", owned_by: provider.id })) });
        return true;
      }
      try {
        const upstream = await fetch(`${api}${relay.modelsPath || "/models/available"}`, {
          headers: zoAuthHeaders(account, provider),
          signal: controller.signal,
        });
        const text = await upstream.text();
        if (upstream.ok) {
          const parsed = JSON.parse(text);
          const rows = toOpenAIModelList(parsed);
          if (rows.length) {
            sendJson(res, 200, { object: "list", data: rows });
            return true;
          }
        }
        log("warn", `zo models 列表失败（HTTP ${upstream.status}）：${text.slice(0, 120)}`);
      } catch (cause) {
        log("warn", `zo models 列表异常：${cause?.message ?? cause}`);
      }
      const ids = (provider.models?.length ? provider.models : [provider.defaultModel]).filter(Boolean);
      sendJson(res, 200, { object: "list", data: ids.map((id) => ({ id, object: "model", owned_by: provider.id })) });
      return true;
    }

    if (req.method !== "POST") {
      sendJson(res, 405, errorBody(`方法不支持：${req.method} ${rest}`, "invalid_request_error", "method_not_allowed"));
      return true;
    }

    const body = await readJsonBody(req);
    const anthropic = rest === "/v1/messages";
    // /v1/completions、/v1/responses 也折成 messages
    let chatBody = body;
    if (anthropic) {
      chatBody = messagesToChat(body, { model: body.model });
    } else if (rest === "/v1/completions") {
      const prompt = typeof body.prompt === "string" ? body.prompt : Array.isArray(body.prompt) ? body.prompt.join("\n") : "";
      chatBody = { model: body.model, stream: body.stream, messages: [{ role: "user", content: prompt }] };
    } else if (rest === "/v1/responses") {
      const input = body.input;
      const messages = Array.isArray(input)
        ? input.map((item) => ({ role: item?.role ?? "user", content: item?.content ?? "" }))
        : [{ role: "user", content: typeof input === "string" ? input : JSON.stringify(input ?? "") }];
      chatBody = { model: body.model, stream: body.stream, messages };
    } else if (rest !== "/v1/chat/completions") {
      sendJson(res, 404, errorBody(`zo 没有这个路由：${rest}`, "invalid_request_error", "not_found"));
      return true;
    }

    const conversationId = typeof body.conversation_id === "string" ? body.conversation_id : null;
    const { payload, toolMode, requestedModel } = buildZoAskPayload(chatBody, provider, { conversationId });
    const wantedStream = chatBody.stream === true;
    // 上游流式能力：先请求流；若上游回 4xx 再退化成非流式（客户端仍拿到 SSE）
    const askUrl = `${api}${askPath}`;

    if (!pool.healthy().length) {
      sendJson(res, 503, errorBody("zo 号池里没有可用账号（未注册 / 冷却中 / 已失效）。", "upstream_error", "no_account"));
      return true;
    }

    const exclude = [];
    let upstream = null;
    let used = null;
    let lastError = null;
    const attempts = Math.min(3, Math.max(1, pool.healthy().length));

    for (let attempt = 0; attempt < attempts; attempt++) {
      const account = pool.pick({ exclude });
      if (!account) break;
      exclude.push(account.id);
      let current = account;
      if (zoNeedsRefresh(current, relay.refreshWithinMs)) {
        try {
          current = pool.update(current.id, await zoRefreshToken(provider, current)) ?? current;
        } catch (cause) {
          log("warn", `zo 预续期失败 ${account.email}：${cause?.message ?? cause}`);
        }
      }
      const doFetch = (who) => fetch(askUrl, {
        method: "POST",
        headers: zoAuthHeaders(who, provider),
        body: JSON.stringify(payload),
        signal: controller.signal,
      });
      try {
        let attemptRes = await doFetch(current);
        if (attemptRes.status === 401 || attemptRes.status === 403) {
          await attemptRes.text().catch(() => "");
          try {
            current = pool.update(current.id, await zoRefreshToken(provider, current)) ?? current;
            attemptRes = await doFetch(current);
          } catch (cause) {
            log("warn", `zo 续期失败 ${account.email}：${cause?.message ?? cause}`);
          }
        }
        if (attemptRes.ok) {
          upstream = attemptRes;
          used = current;
          break;
        }
        const detail = await attemptRes.text().catch(() => "");
        pool.recordResult(account.id, {
          ok: false,
          error: `HTTP ${attemptRes.status}: ${detail.slice(0, 160)}`,
          cooldownMs: attemptRes.status === 429 ? 60_000 : 30_000,
        });
        lastError = Object.assign(new Error(`zo 上游拒绝请求（HTTP ${attemptRes.status}）。`), {
          status: attemptRes.status,
          body: detail.slice(0, 300),
        });
        // 409 = 同一会话还有一轮在跑，换号没用，直接回给客户端
        if (attemptRes.status === 409) break;
        log("warn", `zo account ${account.email} failed (${attemptRes.status}), rotating`);
      } catch (cause) {
        if (controller.signal.aborted) return true;
        pool.recordResult(account.id, { ok: false, error: String(cause?.message ?? cause), cooldownMs: 20_000 });
        lastError = cause;
        continue;
      }
    }

    if (!upstream || !used) {
      if (lastError) throw Object.assign(lastError, { status: lastError.status ?? 502 });
      sendJson(res, 503, errorBody("zo 号池里没有可用账号。", "upstream_error", "no_account"));
      return true;
    }

    pool.recordResult(used.id, { ok: true });
    res.setHeader("X-Gateway-Provider", provider.id);
    res.setHeader("X-Gateway-Account", String(used.id));

    const contentType = upstream.headers.get("content-type") ?? "";
    const upstreamStream = contentType.includes("text/event-stream");

    // ---- 非流式上游 ----
    if (!upstreamStream) {
      const text = await upstream.text();
      let parsed;
      try {
        parsed = JSON.parse(text);
      } catch {
        sendJson(res, 502, errorBody(`zo 返回了非 JSON 响应：${text.slice(0, 200)}`, "upstream_error"));
        return true;
      }
      const upstreamConversation = parsed.conversation_id ?? payload.conversation_id ?? null;
      if (anthropic) {
        const message = zoToAnthropicMessage(parsed, { model: chatBody.model ?? requestedModel, conversationId: upstreamConversation });
        if (wantedStream) {
          const completion = zoToCompletion(parsed, { model: chatBody.model, toolMode, conversationId: upstreamConversation });
          const { chunks } = synthesizeChatSSE(completion);
          const source = (async function* () {
            yield Buffer.from(chunks.join(""), "utf8");
          })();
          res.writeHead(200, {
            "Content-Type": "text/event-stream; charset=utf-8",
            "Cache-Control": "no-store",
            "X-Accel-Buffering": "no",
            ...(upstreamConversation ? { "X-Zo-Conversation-Id": String(upstreamConversation) } : {}),
          });
          for await (const chunk of streamAsAnthropic(source, { model: chatBody.model })) {
            if (res.writableEnded) break;
            res.write(chunk);
          }
          if (!res.writableEnded) res.end();
          return true;
        }
        if (upstreamConversation) res.setHeader("X-Zo-Conversation-Id", String(upstreamConversation));
        sendJson(res, 200, message);
        return true;
      }

      const completion = zoToCompletion(parsed, {
        model: chatBody.model ?? requestedModel,
        toolMode,
        conversationId: upstreamConversation,
      });
      if (!wantedStream) {
        if (upstreamConversation) res.setHeader("X-Zo-Conversation-Id", String(upstreamConversation));
        sendJson(res, 200, completion);
        return true;
      }
      const { chunks } = synthesizeChatSSE(completion);
      res.writeHead(200, {
        "Content-Type": "text/event-stream; charset=utf-8",
        "Cache-Control": "no-store",
        "X-Accel-Buffering": "no",
        ...(upstreamConversation ? { "X-Zo-Conversation-Id": String(upstreamConversation) } : {}),
      });
      for (const chunk of chunks) {
        if (res.writableEnded) break;
        res.write(chunk);
      }
      if (!res.writableEnded) res.end();
      return true;
    }

    // ---- 流式上游 ----
    const events = iterateZoEvents(upstream.body);
    if (anthropic) {
      // Anthropic 客户端要 Anthropic 事件序列：先把 Zo 事件折成 OpenAI chunk 字符串，
      // 再交给既有的 streamAsAnthropic 转换器（它是按 OpenAI SSE 写的）。
      const openaiChunks = zoEventsToOpenAIChunks(events, { model: chatBody.model ?? requestedModel });
      const source = (async function* () {
        for await (const chunk of openaiChunks) {
          yield Buffer.from(chunk, "utf8");
        }
      })();
      res.writeHead(200, {
        "Content-Type": "text/event-stream; charset=utf-8",
        "Cache-Control": "no-store",
        "X-Accel-Buffering": "no",
      });
      for await (const chunk of streamAsAnthropic(source, { model: chatBody.model })) {
        if (res.writableEnded) break;
        res.write(chunk);
      }
      if (!res.writableEnded) res.end();
      return true;
    }

    res.writeHead(200, {
      "Content-Type": "text/event-stream; charset=utf-8",
      "Cache-Control": "no-store",
      "X-Accel-Buffering": "no",
      Connection: "keep-alive",
    });
    for await (const chunk of zoEventsToOpenAIChunks(events, { model: chatBody.model ?? requestedModel })) {
      if (res.writableEnded) break;
      if (!res.write(chunk)) {
        await new Promise((done) => {
          res.once("drain", done);
          res.once("close", done);
        });
      }
    }
    if (!res.writableEnded) res.end();
    return true;
  }

  /** 路由处理：返回 true 表示已处理。 */
  /**
   * 生成类异步任务：提交到 provider 的 **task** 基址（与 LLM 基址不同）。
   *
   *   POST /p/wavespeed/v1/async/<model_id...>
   *        -> POST {taskApiBase}/<model_id>            （taskApiBase 已含 /api/v3）
   *   POST /p/novita/v1/async/<op>
   *        -> POST {taskApiBase}/v3/async/<op>         （op 如 txt2img）
   *
   * 两家都不允许对提交做重试（会产生重复计费任务），所以这里固定 attempts=1。
   * 轮询走 `GET /p/<provider>/v1/async/result?id=…`，见 handle()。
   */
  async function handleAsyncSubmit({ req, res, rest, provider, pool, controller }) {
    const endpoints = resolveEndpoints(provider);
    const taskBase = (endpoints.taskApiBase || "").replace(/\/+$/, "");
    if (!taskBase) {
      sendJson(res, 501, errorBody(`${provider.id} 没有配置 taskApiBase，无法提交异步任务。`, "invalid_request_error", "no_task_base"));
      return true;
    }
    if (req.method !== "POST") {
      sendJson(res, 405, errorBody(`方法不支持：${req.method} ${rest}`, "invalid_request_error", "method_not_allowed"));
      return true;
    }

    // /v1/async/txt2img  ->  tail = txt2img
    const tail = rest.replace(/^\/v1\/async\/?/, "");
    if (!tail) {
      sendJson(res, 400, errorBody("缺少任务路径，例如 /v1/async/wavespeed-ai/z-image/turbo", "invalid_request_error", "missing_task_path"));
      return true;
    }
    const path = provider.id === "novita" ? `/v3/async/${tail}` : `/${tail}`;

    let body;
    try {
      body = await readJsonBody(req);
    } catch (cause) {
      sendJson(
        res,
        cause?.status ?? 400,
        errorBody(cause?.message ?? "请求体不合法。", "invalid_request_error", "bad_body"),
      );
      return true;
    }

    if (!pool.healthy().length) {
      sendJson(
        res,
        503,
        errorBody(`${provider.id} 号池里没有可用账号（未注册 / 冷却中 / 已失效）。`, "upstream_error", "no_account"),
      );
      return true;
    }

    // attempts=1：提交不可重放
    const { res: upstream, account } = await callWithFailover({
      providerId: provider.id,
      path,
      body,
      method: "POST",
      signal: controller.signal,
      maxAttempts: 1,
      base: taskBase,
    });

    res.setHeader("X-Gateway-Provider", provider.id);
    res.setHeader("X-Gateway-Account", String(account.id));
    await relayUpstream(res, upstream, `${provider.id}-async`);
    return true;
  }

  async function handle(req, res, url, { auth } = {}) {
    const match = /^\/p\/([a-z0-9_-]+)(\/v1\/.+|\/mcp)?$/i.exec(url.pathname);
    if (!match) return false;
    const providerId = match[1].toLowerCase();
    const pool = pools.get(providerId);
    if (!pool) {
      sendJson(res, 404, errorBody(`未知 provider：${providerId}`, "invalid_request_error", "unknown_provider"));
      return true;
    }
    const provider = pool.provider;
    const rest = (match[2] || "/v1/models").replace(/\/+$/, "") || "/v1/models";

    // 客户端鉴权：网关 Key（和 /v1/* 同一把尺子）。
    const presented = presentedKey(req, url);
    if (!presented) {
      sendJson(
        res,
        401,
        errorBody("缺少 API Key。用网关创建的 sk-dcg-… 作为 Bearer Token。", "authentication_error", "invalid_api_key"),
        { "WWW-Authenticate": "Bearer" },
      );
      return true;
    }
    if (auth && !auth.ok) {
      sendJson(res, 401, errorBody("API Key 无效或已撤销。", "authentication_error", "invalid_api_key"), {
        "WWW-Authenticate": "Bearer",
      });
      return true;
    }

    const controller = new AbortController();
    res.on("close", () => controller.abort());

    try {
      // moonai：自有协议，走专用翻译层（不影响 wavespeed / novita 的透传路径）
      if (provider.relay?.kind === "moonai-llm-proxy") {
        return await handleMoonai({ req, res, rest, provider, pool, controller });
      }

      // zo：/zo/ask + /mcp 专用翻译层（同样不影响其它 provider）
      if (provider.relay?.kind === "zo-ask") {
        return await handleZo({ req, res, rest, provider, pool, controller, url });
      }

      // ---- 模型列表：优先问上游，拿不到就退回描述符里的默认模型 ----
      if (req.method === "GET" && rest === "/v1/models") {
        const listed = await listModels(provider, pool, controller.signal);
        return sendJson(res, 200, { object: "list", data: listed }), true;
      }

      // ---- 异步任务提交：走 provider 的 task 基址（与 LLM 基址不同）----
      if (rest.startsWith("/v1/async/") && rest !== "/v1/async/result") {
        return handleAsyncSubmit({ req, res, rest, provider, pool, controller });
      }

      // ---- 异步任务轮询 ----
      if (req.method === "GET" && rest === "/v1/async/result") {
        const id = url.searchParams.get("id") ?? "";
        if (!id) {
          sendJson(res, 400, errorBody("缺少 id 参数。", "invalid_request_error", "missing_id"));
          return true;
        }
        const endpoints = resolveEndpoints(provider);
        const resultBase = (endpoints.taskApiBase || "").replace(/\/+$/, "");
        const path = provider.id === "wavespeed"
          ? `/predictions/${encodeURIComponent(id)}/result`
          : `/v3/async/task-result?task_id=${encodeURIComponent(id)}`;
        const { res: upstream } = await callWithFailover({
          providerId,
          path,
          method: "GET",
          signal: controller.signal,
          base: resultBase,
        });
        await relayUpstream(res, upstream, `${provider.id}-result`);
        return true;
      }

      // ---- 其它一律 POST 透传 ----
      if (req.method !== "POST") {
        sendJson(res, 405, errorBody(`方法不支持：${req.method} ${rest}`, "invalid_request_error", "method_not_allowed"));
        return true;
      }

      const body = await readJsonBody(req);

      // Anthropic 兼容：翻译成 OpenAI 形状再发上游，回程再翻译回来。
      let upstreamPath = rest;
      let payload = body;
      let anthropic = false;
      if (rest === "/v1/messages") {
        anthropic = true;
        upstreamPath = "/v1/chat/completions";
        payload = messagesToChat(body, { model: body.model });
      }

      const { res: upstream, account } = await callWithFailover({
        providerId,
        path: upstreamPath,
        body: payload,
        method: "POST",
        signal: controller.signal,
      });

      if (!upstream.ok) {
        const text = await upstream.text().catch(() => "");
        sendJson(
          res,
          upstream.status,
          errorBody(`${provider.label} 上游返回 ${upstream.status}：${text.slice(0, 300)}`, "upstream_error", `http_${upstream.status}`),
        );
        return true;
      }

      res.setHeader("X-Gateway-Provider", provider.id);
      res.setHeader("X-Gateway-Account", String(account.id));

      if (anthropic && !(body.stream === true)) {
        const completion = await upstream.json().catch(() => null);
        if (!completion) {
          sendJson(res, 502, errorBody("上游返回了无法解析的 completion。", "upstream_error"));
          return true;
        }
        sendJson(res, 200, chatToMessage(completion, { model: body.model, requestId: `msg_${Date.now().toString(16)}` }));
        return true;
      }

      await relayUpstream(res, upstream);
      return true;
    } catch (cause) {
      if (controller.signal.aborted) return true;
      const status = cause?.status ?? 502;
      sendJson(
        res,
        status,
        errorBody(cause?.message ?? "provider 反代失败。", status < 500 ? "invalid_request_error" : "upstream_error", cause?.code ?? null),
      );
      return true;
    }
  }

  /**
   * 上游 /models 列表。
   *
   * 和对话请求一样换号：401/403 的账号会被标记不健康并进冷却，然后试下一个。
   * 只有所有账号都问不到时才退回描述符里的白名单 / 默认模型 —— 这样
   * `/v1/models` 永远不会因为一把坏 key 就整片不可用。
   */
  async function listModels(provider, pool, signal) {
    const endpoints = resolveEndpoints(provider);
    const base = (endpoints.apiBase || "").replace(/\/+$/, "");
    if (base) {
      const exclude = [];
      const attempts = Math.min(3, Math.max(1, pool.size || 1));
      for (let attempt = 0; attempt < attempts; attempt++) {
        const account = pool.pick({ exclude });
        if (!account) break;
        exclude.push(account.id);
        try {
          const res = await upstreamFetch(provider, account, {
            path: provider.modelsPath || "/v1/models",
            method: "GET",
            signal,
          });
          if (res.ok) {
            const payload = await res.json().catch(() => null);
            const rows = Array.isArray(payload?.data)
              ? payload.data
              : Array.isArray(payload?.models)
                ? payload.models
                : null;
            if (rows?.length) {
              pool.recordResult(account.id, { ok: true });
              return rows.map((row) =>
                typeof row === "string"
                  ? { id: row, object: "model", owned_by: provider.id }
                  : {
                      id: String(row.id ?? row.name ?? ""),
                      object: "model",
                      owned_by: row.owned_by ?? provider.id,
                      ...(row.created ? { created: row.created } : {}),
                    },
              );
            }
            pool.recordResult(account.id, { ok: true });
            break;
          }
          // 鉴权/额度类失败：冷却并换号；其它状态码直接退回兜底
          if (RETRYABLE.has(res.status)) {
            pool.recordResult(account.id, {
              ok: false,
              error: `models HTTP ${res.status}`,
              cooldownMs: res.status === 429 ? 60_000 : 30_000,
            });
            continue;
          }
          break;
        } catch {
          continue;
        }
      }
    }
    // 描述符里声明了模型白名单就优先用它：有些上游没有 /models
    // （或需要付费档才开放），靠 catalog 保证 /v1/models 可用。
    if (Array.isArray(provider.models) && provider.models.length) {
      return provider.models.map((id) => ({ id, object: "model", owned_by: provider.id }));
    }
    return provider.defaultModel
      ? [{ id: provider.defaultModel, object: "model", owned_by: provider.id }]
      : [];
  }

  return { handle, callWithFailover, upstreamFetch };
}

function presentedKey(req, url) {
  const header = req.headers.authorization ?? "";
  if (header.startsWith("Bearer ")) return header.slice(7).trim();
  const apiKey = req.headers["x-api-key"];
  if (typeof apiKey === "string" && apiKey.trim()) return apiKey.trim();
  const query = url?.searchParams?.get("key");
  return query ? query.trim() : "";
}

/** 给 MCP / CLI 用的「反代自检」：拿号池里一把 key 打上游 /models。 */
export async function probeProvider({ provider, pool, timeoutMs = 15_000 }) {
  const endpoints = resolveEndpoints(provider);
  const base = (endpoints.apiBase || "").replace(/\/+$/, "");
  const started = Date.now();
  const account = pool.healthy()[0] ?? pool.accounts[0] ?? null;
  if (!base) return { ok: false, reason: "no_api_base", provider: provider.id };
  if (!account?.apiKey) return { ok: false, reason: "no_account", provider: provider.id };
  try {
    const res = await fetch(`${base}${provider.modelsPath || "/v1/models"}`, {
      headers: { Authorization: `Bearer ${account.apiKey}`, Accept: "application/json" },
      signal: AbortSignal.timeout(timeoutMs),
    });
    const text = await res.text().catch(() => "");
    return {
      ok: res.ok,
      provider: provider.id,
      status: res.status,
      email: account.email,
      body: text.slice(0, 200),
      ms: Date.now() - started,
    };
  } catch (cause) {
    return { ok: false, provider: provider.id, reason: String(cause?.message ?? cause), ms: Date.now() - started };
  }
}

export { hashKey };
