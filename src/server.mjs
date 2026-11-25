// HTTP surface of the gateway.
//
//   /                     admin WebUI (the OpenWebUI fork reaches it through /api/v1/deepai)
//   /api/admin/*          admin JSON API (admin token -> session cookie)
//   /auth-callback        DeepAI OAuth landings
//   /v1/*                 OpenAI-compatible surface for downstream tools
//   /v1/messages          Anthropic-compatible surface (Claude Code and friends)
import { createServer } from "node:http";
import { randomBytes, timingSafeEqual } from "node:crypto";
import { existsSync, mkdirSync, readFileSync, writeFileSync } from "node:fs";
import { dirname, join } from "node:path";

import { chatToMessage, messagesToChat, streamAsAnthropic } from "./anthropic.mjs";
import { extractPresentedKey, hashKey, looksLikeGatewayKey, mintKey } from "./keys.mjs";
import { CALLBACK_PATH, SignInError, beginSignIn, exchangeCode, signInUrl, stateMatches } from "./oauth.mjs";
import { Registrations } from "./register.mjs";
import { AllmodelsPool, importAccount, maskAccount } from "./allmodels-pool.mjs";
import { AllmodelsRegistrations } from "./allmodels-register.mjs";
import { ToolError, createToolRegistry } from "./allmodels-tools.mjs";
import { PoePool } from "./poe-pool.mjs";
import { publicAccount } from "./store.mjs";

const MAX_BODY = 32 * 1024 * 1024;
const SESSION_COOKIE = "dcg_admin";
const PENDING_TTL_MS = 10 * 60 * 1000;

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

const sendHtml = (res, status, html) => {
  const body = Buffer.from(html);
  res.writeHead(status, {
    "Content-Type": "text/html; charset=utf-8",
    "Content-Length": String(body.length),
    "Cache-Control": "no-store",
    "Referrer-Policy": "no-referrer",
    "X-Frame-Options": "DENY",
    "Content-Security-Policy":
      "default-src 'self'; img-src 'self' data:; style-src 'self' 'unsafe-inline'; script-src 'self' 'unsafe-inline'; connect-src 'self'; form-action 'self'",
  });
  res.end(body);
};

const errorBody = (message, type = "invalid_request_error", code = null) => ({
  error: { message, type, ...(code ? { code } : {}) },
});

const sameSecret = (a, b) => {
  const x = Buffer.from(String(a ?? ""));
  const y = Buffer.from(String(b ?? ""));
  return x.length === y.length && x.length > 0 && timingSafeEqual(x, y);
};

const corsHeaders = () => ({
  "Access-Control-Allow-Origin": "*",
  "Access-Control-Allow-Methods": "GET, POST, DELETE, OPTIONS",
  "Access-Control-Allow-Headers": "Authorization, Content-Type, X-API-Key, X-Session-Affinity",
  "Access-Control-Max-Age": "600",
});

function parseCookies(req) {
  const out = {};
  for (const part of String(req.headers.cookie ?? "").split(";")) {
    const index = part.indexOf("=");
    if (index === -1) continue;
    out[part.slice(0, index).trim()] = decodeURIComponent(part.slice(index + 1).trim());
  }
  return out;
}

const parseAliases = (raw) => {
  if (!raw) return {};
  try {
    const parsed = JSON.parse(raw);
    return parsed && typeof parsed === "object" ? parsed : {};
  } catch {
    return {};
  }
};

export function createGateway({ config, lane, store, scheduler, log = () => {} }) {
  const adminToken = ensureAdminToken(config, log);
  const aliases = parseAliases(process.env.DEEPAI_MODEL_ALIASES);
  const stats = { requests: 0, streams: 0, byModel: new Map(), startedAt: Date.now() };
  const pending = new Map(); // oauth state -> { verifier, challenge, startedAt }

  /**
   * Bulk signup: the Python worker in tools/deepai-reg creates the account and
   * POSTs it back here, so the pool, the scheduler and the catalog all refresh
   * exactly like a manual OAuth link does.
   */
  const registrations = new Registrations({
    dataDir: config.dataDir,
    port: config.port,
    token: adminToken,
    log: (level, message) => log(level === "warn" ? "warn" : "info", message),
    onAccount: (account) => {
      const saved = store.upsertAccount({
        apiKey: account.apiKey,
        username: account.username,
        isPro: account.isPro === true,
        label: account.label,
        connect: null,
      });
      lane.resetCatalog?.();
      log("info", `注册入池 ${account.email || account.username}（账号池 ${store.accounts.length}）`);
      return saved;
    },
  });

  /**
   * Allmodels 号池：accounts registered on dash.allmodels.io, each with the
   * dashboard API key it produced and a gateway key minted for it. The pool file
   * is the shared protocol (`docs/ALLMODELS-POOL.md`); the controller below is
   * the only writer while a batch is running.
   */
  const allmodelsPool = AllmodelsPool.at(config.dataDir, {
    baseUrl: process.env.ALLMODELS_BASE_URL || "https://dash.allmodels.io",
    apiBase: process.env.ALLMODELS_API_BASE || "https://api.allmodels.io/v1",
    log: (level, message) => log(level === "warn" ? "warn" : "info", message),
  });

  const allmodels = new AllmodelsRegistrations({
    dataDir: config.dataDir,
    port: config.port,
    token: adminToken,
    pool: allmodelsPool,
    store,
    log: (level, message) => log(level === "warn" ? "warn" : "info", message),
    onAccount: ({ account, gatewayKey }) => {
      log(
        "info",
        `allmodels 入池 ${account.email}（号池 ${allmodelsPool.size}，网关 Key ${gatewayKey?.prefix ?? "-"}）`,
      );
    },
  });

  // 工具层：MCP（src/allmodels-mcp.mjs）与 HTTP 桥共用同一份 schema + handler。
  const allmodelsTools = createToolRegistry({
    pool: allmodelsPool,
    store,
    registrations: allmodels,
    config,
    log,
  });

  /**
   * Poe 号池：和 DeepAI 池并行但完全独立 —— 凭据是 p-b cookie，不是 api key，
   * 生命周期也不同（email_verified → phone_required → active）。写入口有两处：
   * worker 注册完自己 POST /api/poe/accounts，或操作者手动补登。
   */
  const poePool = new PoePool(config.dataDir);
  const poe = new Registrations({
    dataDir: config.dataDir,
    port: config.port,
    token: adminToken,
    spec: {
      name: "poe",
      module: "poe_reg",
      jobFile: "poe-register-job.json",
      retryFlag: false,
      smsFlag: true,
      mapAccount: null,
    },
    log: (level, message) => log(level === "warn" ? "warn" : "info", message),
    onAccount: () => {},
  });

  const bump = (model) => {
    stats.requests++;
    stats.byModel.set(model, (stats.byModel.get(model) ?? 0) + 1);
  };

  /* ---------------------------------------------------------------- sessions */

  const sessionOf = (req) => {
    const token = parseCookies(req)[SESSION_COOKIE];
    return token ? store.touchSession(token) : null;
  };
  const isAdmin = (req) => sessionOf(req) !== null;

  /**
   * The admin token presented as `Authorization: Bearer …` or `X-Admin-Token`.
   * Used by the Open WebUI fork, which calls this API server-to-server.
   */
  const presentedAdminToken = (req) => {
    const header = req.headers.authorization ?? "";
    const bearer = header.startsWith("Bearer ") ? header.slice(7).trim() : "";
    const explicit = req.headers["x-admin-token"];
    return sameSecret(bearer, adminToken) || sameSecret(bearer, config.token) || (typeof explicit === "string" && sameSecret(explicit, adminToken));
  };

  const startSession = (res) => {
    const token = randomBytes(32).toString("base64url");
    store.addSession(token);
    res.setHeader("Set-Cookie", `${SESSION_COOKIE}=${token}; Path=/; HttpOnly; SameSite=Lax; Max-Age=${30 * 24 * 3600}`);
    return token;
  };

  const endSession = (req, res) => {
    const token = parseCookies(req)[SESSION_COOKIE];
    if (token) store.dropSession(token);
    res.setHeader("Set-Cookie", `${SESSION_COOKIE}=; Path=/; HttpOnly; SameSite=Lax; Max-Age=0`);
  };

  /* ------------------------------------------------------------------- auth */

  /**
   * An explicitly presented credential always decides on its own: a bad key is
   * never rescued by a browser session that happens to travel with it.
   */
  const authorizeApi = (req, url) => {
    const presented = extractPresentedKey(req, url);
    if (presented) {
      if (sameSecret(presented, adminToken) || sameSecret(presented, config.token)) return { ok: true, kind: "admin" };
      if (!looksLikeGatewayKey(presented)) return { ok: false, reason: "format" };
      const hash = hashKey(presented);
      let key = store.findByHash(hash);
      if (!key) {
        // A key minted by the MCP server / CLI lands in the same state file;
        // pick it up before calling this credential unknown.
        store.reloadFromDisk();
        key = store.findByHash(hash);
      }
      if (!key) return { ok: false, reason: "unknown" };
      return { ok: true, kind: "key", keyId: key.id };
    }
    if (isAdmin(req)) return { ok: true, kind: "session" };
    return { ok: false, reason: "missing" };
  };

  /** Count the request against the key's limits; returns null when allowed. */
  const enforceKeyLimits = (auth) => {
    if (auth.kind !== "key") return null;
    const verdict = store.touchKey(auth.keyId);
    if (verdict.allowed) return null;
    const reason = verdict.reason === "rate_limit" ? "该 Key 触发每分钟调用上限。" : "该 Key 已用完当日调用额度。";
    return {
      status: 429,
      body: errorBody(reason, "rate_limit_error", verdict.reason),
      headers: verdict.retryAfterMs ? { "Retry-After": String(Math.ceil(verdict.retryAfterMs / 1000)) } : {},
    };
  };

  const requireAdmin = (req, res) => {
    if (isAdmin(req)) return true;
    // Server-to-server callers (the Open WebUI fork in a container) have no
    // browser session, so the admin token itself is accepted as a bearer.
    if (presentedAdminToken(req)) return true;
    sendJson(res, 401, errorBody("管理台登录已失效，请重新输入管理口令。", "authentication_error", "admin_required"));
    return false;
  };

  /* ------------------------------------------------------------------ bodies */

  const readBody = (req) =>
    new Promise((resolve, reject) => {
      const chunks = [];
      let size = 0;
      req.on("data", (chunk) => {
        size += chunk.length;
        if (size > MAX_BODY) {
          reject(Object.assign(new Error("Request body too large."), { status: 413 }));
          req.destroy();
          return;
        }
        chunks.push(chunk);
      });
      req.on("end", () => resolve(Buffer.concat(chunks)));
      req.on("error", reject);
    });

  const readJson = async (req) => {
    let payload;
    try {
      payload = JSON.parse((await readBody(req)).toString("utf8"));
    } catch (cause) {
      if (cause?.status === 413) throw cause;
      throw Object.assign(new Error("Request body is not valid JSON."), { status: 400 });
    }
    if (!payload || typeof payload !== "object" || Array.isArray(payload)) {
      throw Object.assign(new Error("Request body must be a JSON object."), { status: 400 });
    }
    return payload;
  };

  const fail = (res, cause, fallback = "Gateway failure.") =>
    sendJson(
      res,
      cause?.status ?? 502,
      errorBody(cause?.message ?? fallback, cause?.status && cause.status < 500 ? "invalid_request_error" : "upstream_error", cause?.code ?? null),
    );

  /* ----------------------------------------------------------------- models */

  const openAiModel = (m) => ({
    id: m.id,
    object: "model",
    created: Math.floor(stats.startedAt / 1000),
    owned_by: "deepai",
    name: m.name ?? m.id,
    context_window: m.contextWindow,
    max_tokens: m.maxTokens,
    reasoning: m.reasoning === true,
    input: Array.isArray(m.input) ? m.input : ["text"],
    premier: m.premier === true,
    theos_per_request: m.theosPerRequest ?? null,
    ...(m.api ? { api: m.api } : {}),
  });

  async function resolveModel(requested) {
    const catalog = await lane.getCatalog().catch(() => []);
    const ids = new Set(catalog.map((m) => m.id));
    const cleaned = typeof requested === "string" ? requested.replace(/^deepai\//, "").trim() : "";
    if (cleaned && ids.has(cleaned)) return { model: cleaned, fallback: false };
    const aliased = aliases[cleaned];
    if (aliased && ids.has(aliased)) return { model: aliased, fallback: false };
    if (ids.has(config.defaultModel)) return { model: config.defaultModel, fallback: true };
    return { model: cleaned || config.defaultModel, fallback: true };
  }

  /* ------------------------------------------------------------------ relay */

  async function relay(res, upstream, extraHeaders = {}) {
    const contentType = upstream.headers.get("content-type") ?? "application/json";
    const headers = {
      "Content-Type": contentType,
      "Cache-Control": "no-store",
      "X-Accel-Buffering": "no",
      ...extraHeaders,
      ...(upstream.headers.has("retry-after") ? { "Retry-After": upstream.headers.get("retry-after") } : {}),
    };
    if (!upstream.body) {
      res.writeHead(upstream.status, headers);
      res.end();
      return;
    }
    res.writeHead(upstream.status, headers);
    try {
      for await (const chunk of upstream.body) {
        if (res.writableEnded) break;
        if (!res.write(chunk)) {
          await new Promise((resolve) => {
            res.once("drain", resolve);
            res.once("close", resolve);
          });
        }
      }
    } catch (cause) {
      if (!res.writableEnded) {
        log("error", `stream failed: ${cause?.message ?? cause}`);
        res.destroy();
        return;
      }
    }
    if (!res.writableEnded) res.end();
  }

  /**
   * Take up to three candidate accounts and fail over between them. A refusal
   * before the response body starts is retryable; once bytes flow, it is not.
   */
  async function callWithFailover({ path, payloadBuffer, model, keyId, signal, affinity }) {
    const candidates = scheduler.candidates({ keyId, model });
    if (!candidates.length) {
      const error = new Error("没有可用的 DeepAI 账号（未连接、冷却中或已失效）。");
      error.status = 503;
      error.code = "no_account";
      throw error;
    }

    let lastError;
    for (const account of candidates) {
      try {
        const upstream = await lane.call(path, payloadBuffer, { account, model, signal, affinity });
        scheduler.pin({ keyId, model, accountId: account.id });
        scheduler.record({ accountId: account.id, ok: true });
        return { upstream, account };
      } catch (cause) {
        if (signal?.aborted) throw cause;
        scheduler.record({ accountId: account.id, ok: false, error: cause });
        lastError = cause;
        log("warn", `account ${account.label ?? account.id} failed (${cause?.code ?? cause?.status}): ${cause?.message}`);
        // Only account-level refusals are worth trying elsewhere.
        const retryable = [401, 403, 429, 502, 503, 504].includes(Number(cause?.status)) || cause?.code === "auth_required";
        if (!retryable) throw cause;
      }
    }

    // Every candidate refused: that is a gateway-side account problem, not the
    // caller's key, so say 503 instead of echoing a 401 from upstream.
    const exhausted = new Error(
      `所有 DeepAI 账号都拒绝了这次请求（${candidates.length} 个，最后一个：${lastError?.code ?? lastError?.status ?? "unknown"}）。`,
    );
    exhausted.status = 503;
    exhausted.code = "all_accounts_failed";
    exhausted.cause = lastError;
    throw exhausted;
  }

  async function forward(res, req, { path, payload, model, auth }) {
    if (!store.accounts.length) {
      return sendJson(res, 401, errorBody("网关还没有连接 DeepAI 账号，请先在管理台登录。", "authentication_error", "no_account"));
    }
    const resolved = await resolveModel(model);
    payload.model = resolved.model;
    bump(resolved.model);
    if (resolved.fallback) log("warn", `model ${model ? JSON.stringify(model) : "(missing)"} -> ${resolved.model} (fallback)`);

    const controller = new AbortController();
    res.on("close", () => controller.abort());
    const affinity = typeof req.headers["x-session-affinity"] === "string" ? req.headers["x-session-affinity"] : undefined;

    let result;
    try {
      result = await callWithFailover({
        path,
        payloadBuffer: Buffer.from(JSON.stringify(payload)),
        model: resolved.model,
        keyId: auth.keyId,
        signal: controller.signal,
        affinity: path === "/chat/completions" ? affinity : undefined,
      });
    } catch (cause) {
      if (controller.signal.aborted) return;
      return fail(res, cause);
    }

    const contentType = result.upstream.headers.get("content-type") ?? "application/json";
    if (payload.stream === true || contentType.includes("text/event-stream")) stats.streams++;
    await relay(res, result.upstream, {
      ...(resolved.fallback ? { "X-Gateway-Model-Fallback": resolved.model } : {}),
      "X-Gateway-Account": String(result.account.id),
    });
  }

  /* ------------------------------------------------------------- completion */

  async function handleChatCompletions(req, res, auth) {
    let payload;
    try {
      payload = await readJson(req);
    } catch (cause) {
      return fail(res, cause);
    }
    if (payload.stream !== true) payload.stream = false;
    const affinity = req.headers["x-session-affinity"];
    if (payload.prompt_cache_key === undefined && typeof affinity === "string" && /^[A-Za-z0-9._:-]{1,64}$/.test(affinity)) {
      payload.prompt_cache_key = affinity;
    }
    return forward(res, req, { path: "/chat/completions", payload, model: payload.model, auth });
  }

  async function handleResponses(req, res, auth) {
    let payload;
    try {
      payload = await readJson(req);
    } catch (cause) {
      return fail(res, cause);
    }
    return forward(res, req, { path: "/responses", payload, model: payload.model, auth });
  }

  async function handleLegacyCompletions(req, res, auth) {
    let source;
    try {
      source = await readJson(req);
    } catch (cause) {
      return fail(res, cause);
    }
    const prompt = Array.isArray(source.prompt) ? source.prompt.join("\n") : String(source.prompt ?? "");
    const payload = {
      messages: [{ role: "user", content: prompt }],
      stream: source.stream === true,
      ...(source.max_tokens ? { max_tokens: source.max_tokens } : {}),
      ...(source.temperature !== undefined ? { temperature: source.temperature } : {}),
      ...(source.top_p !== undefined ? { top_p: source.top_p } : {}),
      ...(source.stop !== undefined ? { stop: source.stop } : {}),
    };
    return forward(res, req, { path: "/chat/completions", payload, model: source.model, auth });
  }

  /** Anthropic /v1/messages: translate in, translate out (JSON and SSE). */
  async function handleMessages(req, res, auth) {
    let body;
    try {
      body = await readJson(req);
    } catch (cause) {
      return fail(res, cause);
    }
    const resolved = await resolveModel(body.model);
    bump(resolved.model);
    const chat = messagesToChat(body, { model: resolved.model });

    const controller = new AbortController();
    res.on("close", () => controller.abort());

    let result;
    try {
      result = await callWithFailover({
        path: "/chat/completions",
        payloadBuffer: Buffer.from(JSON.stringify(chat)),
        model: resolved.model,
        keyId: auth.keyId,
        signal: controller.signal,
      });
    } catch (cause) {
      if (controller.signal.aborted) return;
      return fail(res, cause);
    }

    const contentType = result.upstream.headers.get("content-type") ?? "application/json";
    const requestId = `msg_${randomBytes(12).toString("hex")}`;

    if (chat.stream === true || contentType.includes("text/event-stream")) {
      stats.streams++;
      res.writeHead(200, {
        "Content-Type": "text/event-stream",
        "Cache-Control": "no-store",
        "X-Accel-Buffering": "no",
      });
      try {
        for await (const chunk of streamAsAnthropic(result.upstream.body, { model: resolved.model, requestId })) {
          if (res.writableEnded) break;
          if (!res.write(chunk)) {
            await new Promise((resolve) => {
              res.once("drain", resolve);
              res.once("close", resolve);
            });
          }
        }
      } catch (cause) {
        log("error", `anthropic stream failed: ${cause?.message ?? cause}`);
        res.destroy();
        return;
      }
      if (!res.writableEnded) res.end();
      return;
    }

    const completion = await result.upstream.json().catch(() => null);
    if (!completion) return sendJson(res, 502, errorBody("Upstream returned an unreadable completion.", "upstream_error"));
    sendJson(res, 200, chatToMessage(completion, { model: resolved.model, requestId }));
  }

  async function handleSearch(req, res, path) {
    let payload;
    try {
      payload = await readJson(req);
    } catch (cause) {
      return fail(res, cause);
    }
    try {
      const candidates = scheduler.candidates({});
      if (!candidates.length) {
        return sendJson(res, 503, errorBody("没有可用的 DeepAI 账号。", "upstream_error", "no_account"));
      }
      const result = await lane.json(path, payload, { account: candidates[0] });
      sendJson(res, 200, result);
    } catch (cause) {
      fail(res, cause, "Search failed.");
    }
  }

  /* ------------------------------------------------------------- oauth flow */

  async function startSignIn(res) {
    const attempt = beginSignIn();
    pending.set(attempt.state, attempt);
    for (const [state, value] of pending) {
      if (Date.now() - value.startedAt > PENDING_TTL_MS) pending.delete(state);
    }
    const url = signInUrl({
      state: attempt.state,
      challenge: attempt.challenge,
      port: config.port,
      origin: config.signInOrigin,
    });
    sendJson(res, 200, { url, callback: `http://127.0.0.1:${config.port}${CALLBACK_PATH}` });
  }

  async function completeSignIn({ code, state }) {
    const attempt = state ? pending.get(state) : lastPending();
    if (!attempt) throw new SignInError("没有进行中的登录（或已超时）。请重新发起登录。");
    pending.delete(attempt.state);

    const credentials = await exchangeCode({
      code,
      verifier: attempt.verifier,
      origin: config.apiOrigin,
      appVersion: config.appVersion,
      fetchImpl: (url, init) => lane.fetch(url, init),
    });
    const account = store.upsertAccount({ ...credentials, source: "oauth" });
    log("info", `linked DeepAI account ${credentials.username || "(unknown)"}`);
    lane.resetCatalog();
    return account;
  }

  const lastPending = () => {
    const entries = [...pending.values()].sort((a, b) => b.startedAt - a.startedAt);
    return entries[0] ?? null;
  };

  /* ------------------------------------------------------------- admin state */

  async function adminState() {
    // 号池 / Key 可能被 MCP server 或 CLI 改过，面板要看到最新的一份。
    store.reloadFromDisk();
    allmodelsPool.reload();
    const models = await lane.getCatalog().catch(() => []);
    const primary = store.account;
    const usage = primary ? await lane.getAllowance(primary).catch(() => null) : null;
    return {
      ok: true,
      account: publicAccount(primary),
      accounts: store.accounts.map(publicAccount),
      keys: store.listKeys(),
      models: models.map(openAiModel),
      usage,
      lane: lane.base.replace(/\/deepai_code$/, ""),
      scheduler: scheduler.status(),
      allmodels: {
        pool: allmodelsPool.stats(),
        running: allmodels.job?.status === "running",
        job: allmodels.job
          ? {
              id: allmodels.job.id,
              status: allmodels.job.status,
              requested: allmodels.job.requested,
              created: allmodels.job.created ?? 0,
              failed: allmodels.job.failed ?? 0,
              startedAt: allmodels.job.startedAt,
              finishedAt: allmodels.job.finishedAt ?? null,
            }
          : null,
      },
      stats: {
        requests: stats.requests,
        streams: stats.streams,
        by_model: Object.fromEntries(stats.byModel),
        slot: lane.slot,
      },
    };
  }

  async function handleAdmin(req, res, path) {
    const params = new URL(req.url ?? "/", `http://${req.headers.host ?? "127.0.0.1"}`).searchParams;
    if (path === "/api/admin/login" && req.method === "POST") {
      let body;
      try {
        body = await readJson(req);
      } catch (cause) {
        return fail(res, cause);
      }
      if (!sameSecret(String(body.token ?? "").trim(), adminToken)) {
        return sendJson(res, 401, errorBody("管理口令不正确。", "authentication_error"));
      }
      startSession(res);
      return sendJson(res, 200, { ok: true });
    }

    if (path === "/api/admin/logout" && req.method === "POST") {
      endSession(req, res);
      return sendJson(res, 200, { ok: true });
    }

    if (!requireAdmin(req, res)) return;

    if (path === "/api/admin/state" && req.method === "GET") return sendJson(res, 200, await adminState());
    if (path === "/api/admin/login-start" && req.method === "POST") return startSignIn(res);

    /* ------------------------------------------------------------- bulk signup */

    // Accepts an account created anywhere (the WebUI panel, the CLI, a script).
    // Idempotent on apiKey, so a worker retrying a push is harmless.
    if (path === "/api/admin/accounts" && req.method === "POST") {
      let body;
      try {
        body = await readJson(req);
      } catch (cause) {
        return fail(res, cause);
      }
      const apiKey = typeof body.apiKey === "string" ? body.apiKey.trim() : "";
      if (!apiKey || apiKey.length < 16 || /[\r\n]/.test(apiKey)) {
        return sendJson(res, 400, errorBody("apiKey 不合法。", "invalid_request_error"));
      }
      const saved = store.upsertAccount({
        apiKey,
        username: typeof body.username === "string" ? body.username : "",
        isPro: body.isPro === true,
        label: typeof body.label === "string" && body.label ? body.label : body.email,
        connect: null,
      });
      lane.resetCatalog();
      log("info", `账号入池 ${saved.username || saved.id}（账号池 ${store.accounts.length}）`);
      return sendJson(res, 201, { ok: true, account: publicAccount(saved), accounts: store.accounts.length });
    }

    if (path === "/api/admin/register" && req.method === "GET") {
      return sendJson(res, 200, { ok: true, ...registrations.status() });
    }

    // Filtered log tail for the panel; `download=1` streams it as a .log file.
    if (path === "/api/admin/register/log" && req.method === "GET") {
      const params = new URL(req.url ?? "/", "http://127.0.0.1").searchParams;
      const level = ["debug", "info", "warn", "error"].includes(params.get("level"))
        ? params.get("level")
        : "";
      if (params.get("download") === "1") {
        const body = Buffer.from(registrations.logText({ level }), "utf8");
        res.writeHead(200, {
          "Content-Type": "text/plain; charset=utf-8",
          "Content-Length": String(body.length),
          "Content-Disposition": `attachment; filename="deepai-register-${level || "all"}.log"`,
          "Cache-Control": "no-store",
        });
        res.end(body);
        return;
      }
      const limit = Number(params.get("limit") ?? 120);
      return sendJson(res, 200, {
        ok: true,
        level: level || "all",
        lines: registrations.logSlice({ level, limit }),
      });
    }

    if (path === "/api/admin/register/domains" && req.method === "GET") {
      const params = new URL(req.url ?? "/", "http://127.0.0.1").searchParams;
      return sendJson(res, 200, registrations.domains({ force: params.get("force") === "1" }));
    }

    if (path === "/api/admin/register" && req.method === "POST") {
      let body = {};
      try {
        body = await readJson(req);
      } catch (cause) {
        return fail(res, cause);
      }
      try {
        const snapshot = registrations.start(body);
        return sendJson(res, 202, { ok: true, ...snapshot });
      } catch (cause) {
        return sendJson(res, cause.status ?? 400, errorBody(cause.message, "invalid_request_error", "register_error"));
      }
    }

    if (path === "/api/admin/register/stop" && req.method === "POST") {
      return sendJson(res, 200, { ok: true, ...registrations.stop() });
    }

    /* --------------------------------------------------- allmodels 号池协议 */

    // Everything below speaks the pool protocol in docs/ALLMODELS-POOL.md:
    // register a batch, import an account the worker created elsewhere, inspect
    // or mask the pool, and export accounts + keys in one file.

    if (path === "/api/admin/allmodels/status" && req.method === "GET") {
      return sendJson(res, 200, { ok: true, ...allmodels.status() });
    }

    // 给"编程工具 / 插件 / 工作流"的 HTTP 入口：列工具、按名字调用。
    if (path === "/api/admin/allmodels/tools" && req.method === "GET") {
      return sendJson(res, 200, { ok: true, tools: allmodelsTools.list(), openai: allmodelsTools.openai() });
    }

    const toolCall = /^\/api\/admin\/allmodels\/tools\/([A-Za-z0-9_]+)$/.exec(path);
    if (toolCall && req.method === "POST") {
      let body = {};
      try {
        body = await readJson(req);
      } catch (cause) {
        return fail(res, cause);
      }
      const args =
        body && typeof body === "object" && body.arguments && typeof body.arguments === "object"
          ? body.arguments
          : (body ?? {});
      try {
        const result = await allmodelsTools.call(toolCall[1], args);
        return sendJson(res, 200, { ok: true, tool: toolCall[1], result });
      } catch (cause) {
        return sendJson(
          res,
          cause instanceof ToolError ? 400 : 502,
          errorBody(cause.message, "invalid_request_error", cause.code ?? "tool_failed"),
        );
      }
    }

    if (path === "/api/admin/allmodels/register" && req.method === "POST") {
      let body = {};
      try {
        body = await readJson(req);
      } catch (cause) {
        return fail(res, cause);
      }
      try {
        return sendJson(res, 202, { ok: true, ...allmodels.start(body) });
      } catch (cause) {
        return sendJson(res, cause.status ?? 400, errorBody(cause.message, "invalid_request_error", "register_error"));
      }
    }

    if (path === "/api/admin/allmodels/register/stop" && req.method === "POST") {
      return sendJson(res, 200, { ok: true, ...allmodels.stop() });
    }

    if (path === "/api/admin/allmodels/pool" && req.method === "GET") {
      allmodelsPool.reload();
      const limit = Math.min(500, Math.max(1, Number(params.get("limit") ?? 200)));
      const rows = allmodelsPool.accounts.slice(-limit).map(maskAccount);
      return sendJson(res, 200, {
        ok: true,
        schema: allmodelsPool.toJSON().schema,
        baseUrl: allmodelsPool.baseUrl,
        apiBase: allmodelsPool.apiBase,
        updatedAt: allmodelsPool.toJSON().updatedAt,
        stats: allmodelsPool.stats(),
        accounts: rows,
      });
    }

    // The worker's push target, and the manual import path. Idempotent on apiKey.
    if (path === "/api/admin/allmodels/pool/import" && req.method === "POST") {
      let body;
      try {
        body = await readJson(req);
      } catch (cause) {
        return fail(res, cause);
      }
      const apiKey = typeof body.apiKey === "string" ? body.apiKey.trim() : "";
      const email = typeof body.email === "string" ? body.email.trim().toLowerCase() : "";
      if (!apiKey || apiKey.length < 16 || /[\r\n]/.test(apiKey)) {
        return sendJson(res, 400, errorBody("apiKey 不合法。", "invalid_request_error", "bad_api_key"));
      }
      if (!/^[^\s@]+@[^\s@]+\.[^\s@]+$/.test(email)) {
        return sendJson(res, 400, errorBody("email 不合法。", "invalid_request_error", "bad_email"));
      }
      const result = importAccount({
        pool: allmodelsPool,
        store,
        record: { ...body, apiKey, email },
        mintKey: body.mintKey !== false,
      });
      log("info", `allmodels 入池 ${email}（号池 ${allmodelsPool.size}）`);
      return sendJson(res, 201, {
        ok: true,
        created: result.created,
        account: maskAccount(result.account),
        gatewayKey: result.gatewayKey
          ? { id: result.gatewayKey.id, prefix: result.gatewayKey.prefix, label: result.gatewayKey.label }
          : null,
        pool: allmodelsPool.stats(),
      });
    }

    if (path === "/api/admin/allmodels/pool/export" && (req.method === "POST" || req.method === "GET")) {
      const format = ["jsonl", "csv", "pool"].includes(params.get("format"))
        ? params.get("format")
        : "jsonl";
      const download = params.get("download") === "1";
      const result = allmodels.export({ format });
      if (download) {
        const body = readFileSync(result.file);
        const name = result.file.split(/[\\/]/).pop() ?? "allmodels-pool.jsonl";
        res.writeHead(200, {
          "Content-Type": format === "csv" ? "text/csv; charset=utf-8" : "application/json; charset=utf-8",
          "Content-Length": String(body.length),
          "Content-Disposition": `attachment; filename="${name}"`,
          "Cache-Control": "no-store",
        });
        res.end(body);
        return;
      }
      return sendJson(res, 200, { ok: true, ...result });
    }

    const poolHealth = /^\/api\/admin\/allmodels\/pool\/([^/]+)\/health$/.exec(path);
    if (poolHealth && req.method === "POST") {
      let body = {};
      try {
        body = await readJson(req);
      } catch (cause) {
        return fail(res, cause);
      }
      const updated = allmodelsPool.recordResult(poolHealth[1], {
        ok: body.ok !== false,
        error: body.error ?? null,
        cooldownMs: Number(body.cooldownMs ?? 0),
      });
      if (!updated) return sendJson(res, 404, errorBody("号池里没有这个账号。", "invalid_request_error", "not_found"));
      return sendJson(res, 200, { ok: true, account: maskAccount(updated), pool: allmodelsPool.stats() });
    }

    const poolDelete = /^\/api\/admin\/allmodels\/pool\/([^/]+)$/.exec(path);
    if (poolDelete && req.method === "DELETE") {
      if (!allmodelsPool.remove(poolDelete[1])) {
        return sendJson(res, 404, errorBody("号池里没有这个账号。", "invalid_request_error", "not_found"));
      }
      return sendJson(res, 200, { ok: true, pool: allmodelsPool.stats() });
    }

    if (path === "/api/admin/allmodels/keys" && req.method === "GET") {
      allmodelsPool.reload();
      return sendJson(res, 200, {
        ok: true,
        keys: allmodelsPool.accounts.map((account) => ({
          email: account.email,
          keyPrefix: account.keyPrefix,
          keyLast4: account.keyLast4,
          keySource: account.keySource,
          status: account.status,
          gatewayKey: account.gatewayKey
            ? { id: account.gatewayKey.id, label: account.gatewayKey.label, prefix: account.gatewayKey.prefix }
            : null,
        })),
      });
    }


    if (path === "/api/admin/exchange" && req.method === "POST") {
      let body;
      try {
        body = await readJson(req);
      } catch (cause) {
        return fail(res, cause);
      }
      try {
        await completeSignIn({ code: String(body.code ?? "").trim(), state: body.state });
      } catch (cause) {
        return sendJson(res, cause instanceof SignInError ? 400 : 502, errorBody(cause.message, "authentication_error"));
      }
      return sendJson(res, 200, { ok: true, state: await adminState() });
    }

    if (path === "/api/admin/unlink" && req.method === "POST") {
      store.clearAccount();
      lane.resetCatalog();
      return sendJson(res, 200, { ok: true, state: await adminState() });
    }

    const accountDelete = /^\/api\/admin\/accounts\/([^/]+)$/.exec(path);
    if (accountDelete && req.method === "DELETE") {
      if (!store.removeAccount(accountDelete[1])) return sendJson(res, 404, errorBody("账号不存在。", "invalid_request_error"));
      lane.resetCatalog();
      return sendJson(res, 200, await adminState());
    }

    if (path === "/api/admin/keys" && req.method === "POST") {
      let body;
      try {
        body = await readJson(req);
      } catch (cause) {
        return fail(res, cause);
      }
      const minted = mintKey();
      const key = store.addKey({ label: body.label, hash: minted.hash, prefix: minted.prefix, limits: body.limits });
      log("info", `minted key ${key.prefix}… (${key.label})`);
      return sendJson(res, 201, { ok: true, key: minted.plaintext, record: key, state: await adminState() });
    }

    if (path === "/api/admin/keys" && req.method === "GET") {
      return sendJson(res, 200, { ok: true, keys: store.listKeys() });
    }

    const revoke = /^\/api\/admin\/keys\/([^/]+)\/revoke$/.exec(path);
    if (revoke && req.method === "POST") {
      if (!store.revokeKey(revoke[1])) return sendJson(res, 404, errorBody("Key 不存在。", "invalid_request_error"));
      return sendJson(res, 200, await adminState());
    }

    const remove = /^\/api\/admin\/keys\/([^/]+)\/delete$/.exec(path);
    if (remove && req.method === "POST") {
      if (!store.deleteKey(remove[1])) return sendJson(res, 404, errorBody("Key 不存在。", "invalid_request_error"));
      return sendJson(res, 200, await adminState());
    }

    /* --------------------------------------------------------------- poe 号池 */

    if (path === "/api/poe/accounts" && req.method === "GET") {
      return sendJson(res, 200, {
        ok: true,
        accounts: poePool.list(Number(new URL(req.url ?? "/", "http://127.0.0.1").searchParams.get("limit") ?? 100)),
        counts: poePool.counts(),
      });
    }

    // Write entry used by the worker's `--push-url`, and by the WebUI's manual add.
    if (path === "/api/poe/accounts" && req.method === "POST") {
      let body;
      try {
        body = await readJson(req);
      } catch (cause) {
        return fail(res, cause);
      }
      try {
        const { created, account } = poePool.upsert(body);
        poePool.flush();
        log("info", `poe 入池 ${account.email}（${account.status}，号池 ${poePool.accounts.length}）`);
        return sendJson(res, created ? 201 : 200, {
          ok: true,
          created,
          account: PoePool.public(account),
          counts: poePool.counts(),
        });
      } catch (cause) {
        return sendJson(res, 400, errorBody(cause.message, "invalid_request_error"));
      }
    }

    const poeAccount = /^\/api\/poe\/accounts\/([^/]+)$/.exec(path);
    if (poeAccount && req.method === "DELETE") {
      if (!poePool.remove(poeAccount[1])) return sendJson(res, 404, errorBody("账号不存在。", "invalid_request_error"));
      poePool.flush();
      return sendJson(res, 200, { ok: true, counts: poePool.counts() });
    }

    if (path === "/api/poe/accounts/status" && req.method === "POST") {
      let body;
      try {
        body = await readJson(req);
      } catch (cause) {
        return fail(res, cause);
      }
      if (!poePool.mark(body.email, body.status)) {
        return sendJson(res, 404, errorBody("账号不存在或状态非法。", "invalid_request_error"));
      }
      poePool.flush();
      return sendJson(res, 200, { ok: true, counts: poePool.counts() });
    }

    if (path === "/api/poe/health" && req.method === "GET") {
      return sendJson(res, 200, { ok: true, counts: poePool.counts(), total: poePool.accounts.length });
    }

    if (path === "/api/poe/register" && req.method === "GET") {
      return sendJson(res, 200, { ok: true, ...poe.status() });
    }

    if (path === "/api/poe/register" && req.method === "POST") {
      let body = {};
      try {
        body = await readJson(req);
      } catch (cause) {
        return fail(res, cause);
      }
      try {
        return sendJson(res, 202, { ok: true, ...poe.start(body) });
      } catch (cause) {
        return sendJson(res, cause.status ?? 400, errorBody(cause.message, "invalid_request_error", "register_error"));
      }
    }

    if (path === "/api/poe/register/stop" && req.method === "POST") {
      return sendJson(res, 200, { ok: true, ...poe.stop() });
    }

    if (path === "/api/poe/register/log" && req.method === "GET") {
      const params = new URL(req.url ?? "/", "http://127.0.0.1").searchParams;
      const level = ["debug", "info", "warn", "error"].includes(params.get("level"))
        ? params.get("level")
        : "";
      if (params.get("download") === "1") {
        const body = Buffer.from(poe.logText({ level }), "utf8");
        res.writeHead(200, {
          "Content-Type": "text/plain; charset=utf-8",
          "Content-Length": String(body.length),
          "Content-Disposition": `attachment; filename="poe-register-${level || "all"}.log"`,
          "Cache-Control": "no-store",
        });
        res.end(body);
        return;
      }
      return sendJson(res, 200, {
        ok: true,
        level: level || "all",
        lines: poe.logSlice({ level, limit: Number(params.get("limit") ?? 120) }),
      });
    }

    return sendJson(res, 404, errorBody(`No admin route for ${req.method} ${path}.`, "invalid_request_error", "not_found"));
  }

  /* ------------------------------------------------------------------ routes */

  async function route(req, res) {
    const url = new URL(req.url ?? "/", `http://${req.headers.host ?? "127.0.0.1"}`);
    const path = url.pathname.replace(/\/+$/, "") || "/";

    if (req.method === "OPTIONS") {
      res.writeHead(204, corsHeaders());
      res.end();
      return;
    }
    for (const [k, v] of Object.entries(corsHeaders())) res.setHeader(k, v);

    // The gateway has no UI of its own: Open WebUI (the fork) is the UI, and it
    // reaches the admin API through /api/v1/deepai.
    if (path === "/") {
      const target = config.webuiUrl;
      if (target) {
        res.writeHead(302, { Location: target, "Cache-Control": "no-store" });
        res.end();
        return;
      }
      return sendHtml(res, 200, renderLandingPage(config));
    }

    if (path === CALLBACK_PATH && req.method === "GET") {
      const code = url.searchParams.get("code") ?? "";
      const state = url.searchParams.get("state") ?? "";
      const attempt = pending.get(state);
      if (!attempt || !stateMatches(attempt.state, state)) {
        return sendHtml(res, 400, renderCallbackPage({ ok: false, title: "登录校验失败", message: "回调的 state 不匹配，或这次登录已超时。请在 Open WebUI 里重新发起登录。" }));
      }
      if (!code) {
        return sendHtml(res, 400, renderCallbackPage({ ok: false, title: "登录回调缺少 code", message: "DeepAI 授权页没有带回 code，请重新发起登录。" }));
      }
      try {
        const account = await completeSignIn({ code, state });
        return sendHtml(res, 200, renderCallbackPage({ ok: true, title: "登录成功", message: `已连接 DeepAI 账号 ${account.username || ""}，可以关闭这个标签页了，模型现在可用。` }));
      } catch (cause) {
        return sendHtml(res, 400, renderCallbackPage({ ok: false, title: "登录未完成", message: cause?.message ?? "请重新发起登录。" }));
      }
    }

    if (path === "/health" || path === "/healthz") {
      const primary = store.account;
      return sendJson(res, 200, {
        ok: true,
        account: primary ? { username: primary.username, isPro: primary.isPro } : null,
        accounts: store.accounts.length,
        healthy_accounts: scheduler.candidates({}).length,
        keys: store.listKeys().filter((k) => !k.revoked).length,
        poe: { total: poePool.accounts.length, ...poePool.counts() },
        lane: lane.base,
        instance: lane.instance,
        check_in: primary ? lane.checkInFor(primary.id) : null,
        allmodels: {
          accounts: allmodelsPool.size,
          healthy: allmodelsPool.healthy().length,
          keys: allmodelsPool.accounts.filter((a) => a.gatewayKey).length,
        },
        requests: stats.requests,
        streams: stats.streams,
        uptime_s: Math.round((Date.now() - stats.startedAt) / 1000),
      });
    }

    if (path.startsWith("/api/admin/") || path.startsWith("/api/poe/")) return handleAdmin(req, res, path);

    // ---- authenticated surfaces ----
    const auth = authorizeApi(req, url);
    if (!auth.ok) {
      const message =
        auth.reason === "missing"
          ? "缺少 API Key。用管理台创建的 sk-dcg-… 作为 Bearer Token。"
          : auth.reason === "format"
            ? "API Key 格式不对，应形如 sk-dcg-…。"
            : "API Key 无效或已撤销。";
      return sendJson(res, 401, errorBody(message, "authentication_error", "invalid_api_key"), { "WWW-Authenticate": "Bearer" });
    }

    if (path === "/v1/models" && req.method === "GET") {
      try {
        const catalog = await lane.getCatalog();
        return sendJson(res, 200, { object: "list", data: catalog.map(openAiModel) });
      } catch (cause) {
        return fail(res, cause, "Catalog unavailable.");
      }
    }

    const modelMatch = /^\/v1\/models\/(.+)$/.exec(path);
    if (modelMatch && req.method === "GET") {
      const id = decodeURIComponent(modelMatch[1]);
      try {
        const catalog = await lane.getCatalog();
        const found = catalog.find((m) => m.id === id || m.id === id.replace(/^deepai\//, ""));
        if (!found) return sendJson(res, 404, errorBody(`Unknown model ${id}.`, "invalid_request_error", "model_not_found"));
        return sendJson(res, 200, openAiModel(found));
      } catch (cause) {
        return fail(res, cause, "Catalog unavailable.");
      }
    }

    const limited = enforceKeyLimits(auth);
    if (limited) return sendJson(res, limited.status, limited.body, limited.headers);

    if (path === "/v1/chat/completions" && req.method === "POST") return handleChatCompletions(req, res, auth);
    if (path === "/v1/completions" && req.method === "POST") return handleLegacyCompletions(req, res, auth);
    if (path === "/v1/responses" && req.method === "POST") return handleResponses(req, res, auth);
    if (path === "/v1/messages" && req.method === "POST") return handleMessages(req, res, auth);
    if ((path === "/v1/web-search" || path === "/v1/web_search" || path === "/v1/search") && req.method === "POST") {
      return handleSearch(req, res, "/web_search");
    }
    if (path === "/v1/find-service" && req.method === "POST") return handleSearch(req, res, "/find_service");

    if ((path === "/v1/usage" || path === "/theos") && req.method === "GET") {
      try {
        const account = store.account;
        if (!account) return sendJson(res, 401, errorBody("未连接账号。", "authentication_error", "no_account"));
        return sendJson(res, 200, await lane.getAllowance(account));
      } catch (cause) {
        return fail(res, cause, "Allowance unavailable.");
      }
    }

    // sub2api-style billing view for one key.
    if ((path === "/v1/billing" || path === "/v1/sub2api/billing") && req.method === "GET") {
      const keyId = auth.keyId ?? url.searchParams.get("key_id");
      const key = keyId ? store.findKeyById(keyId) : null;
      return sendJson(res, 200, {
        object: "billing",
        key: key ? { id: key.id, label: key.label, prefix: key.prefix, limits: key.limits } : null,
        usage: {
          requests: key?.requests ?? 0,
          prompt_tokens: key?.promptTokens ?? 0,
          completion_tokens: key?.completionTokens ?? 0,
        },
        account: store.account ? { username: store.account.username, isPro: store.account.isPro } : null,
      });
    }

    if (path === "/v1/stats" && req.method === "GET") {
      return sendJson(res, 200, {
        requests: stats.requests,
        streams: stats.streams,
        by_model: Object.fromEntries(stats.byModel),
        slot: lane.slot,
        accounts: scheduler.status(),
      });
    }

    if (path === "/v1/embeddings") {
      return sendJson(res, 501, errorBody("The DeepAI Code lane exposes no embeddings endpoint.", "not_implemented"));
    }

    return sendJson(res, 404, errorBody(`No route for ${req.method} ${path}.`, "invalid_request_error", "not_found"));
  }

  const server = createServer((req, res) => {
    const started = Date.now();
    route(req, res)
      .catch((cause) => {
        log("error", `${req.method} ${req.url} failed: ${cause?.stack ?? cause}`);
        if (!res.headersSent) sendJson(res, cause?.status ?? 500, errorBody(cause?.message ?? "Gateway failure.", "server_error"));
        else res.destroy();
      })
      .finally(() => {
        if (config.log === "debug") log("debug", `${req.method} ${req.url} ${res.statusCode} ${Date.now() - started}ms`);
      });
  });

  return { server, adminToken, allmodels, allmodelsPool };
}

/**
 * OAuth landing page. Deliberately tiny: the gateway has no UI of its own —
 * Open WebUI is the interface, and it drives account linking from its settings.
 */
export function renderCallbackPage({ ok, title, message }) {
  const color = ok ? "#22d3a6" : "#f0495a";
  return `<!doctype html><html lang="zh-CN"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1"><title>${escapeHtml(title)}</title>
<style>
  body { margin:0; min-height:100vh; display:grid; place-items:center; background:#0b0d12;
         color:#e6e8ee; font:14px/1.7 system-ui,-apple-system,"Segoe UI","Noto Sans SC",sans-serif; }
  main { max-width:460px; padding:32px; text-align:center; }
  h1 { font-size:19px; margin:0 0 10px; color:${color}; }
  p { color:#8b93a7; margin:0 0 8px; }
  code { color:#7c5cff; font-size:12px; }
</style></head><body><main>
  <h1>${escapeHtml(title)}</h1>
  <p>${escapeHtml(message)}</p>
  <p><code>DeepAI Code Gateway</code></p>
</main></body></html>`;
}

/** Shown at "/" when no Open WebUI URL is configured: an operator card, not a UI. */
function renderLandingPage(config) {
  const token = config.token ? "" : "（见网关启动日志，或 node src/index.mjs --show-admin-token）";
  return `<!doctype html><html lang="zh-CN"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1"><title>DeepAI Code Gateway</title>
<style>
  body { margin:0; min-height:100vh; display:grid; place-items:center; background:#0b0d12;
         color:#e6e8ee; font:14px/1.8 system-ui,-apple-system,"Segoe UI","Noto Sans SC",sans-serif; }
  main { max-width:560px; padding:32px; }
  h1 { font-size:20px; margin:0 0 6px; }
  p  { color:#8b93a7; margin:0 0 14px; }
  ul { color:#c7cbdb; padding-left:18px; margin:0 0 14px; }
  code { color:#22d3a6; font-size:12.5px; }
  a { color:#7c5cff; }
  .box { background:#141821; border:1px solid #262c38; border-radius:12px; padding:16px 18px; }
</style></head><body><main>
  <h1>DeepAI Code Gateway</h1>
  <p>OpenAI 兼容反代 · 界面在 Open WebUI，本服务只提供 API。</p>
  <div class="box">
    <ul>
      <li>OpenAI 兼容：<code>${escapeHtml(config.publicBase ?? "")}/v1</code></li>
      <li>Anthropic 兼容：<code>${escapeHtml(config.publicBase ?? "")}/v1/messages</code></li>
      <li>管理接口：<code>/api/admin/*</code>（被 Open WebUI 的 DeepAI 设置页调用）</li>
      <li>健康检查：<a href="/health">/health</a></li>
      <li>管理口令：${escapeHtml(token)}</li>
    </ul>
    <p style="margin:0">把 <code>DEEPAI_WEBUI_URL</code> 指向 Open WebUI（例如
    <code>http://127.0.0.1:3000</code>），这个地址就会直接跳转到界面。</p>
  </div>
</main></body></html>`;
}

function escapeHtml(value) {
  return String(value ?? "").replace(/[&<>"']/g, (ch) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" })[ch]);
}

function ensureAdminToken(config, log) {
  if (config.token) return config.token;
  const path = join(config.home, "userdata", "admin-token");
  try {
    mkdirSync(dirname(path), { recursive: true });
    if (existsSync(path)) {
      const existing = readFileSync(path, "utf8").trim();
      if (existing) return existing;
    }
    const token = randomBytes(24).toString("base64url");
    writeFileSync(path, token, { mode: 0o600 });
    return token;
  } catch (error) {
    log("warn", `could not persist admin token: ${error.message}`);
    return randomBytes(24).toString("base64url");
  }
}
