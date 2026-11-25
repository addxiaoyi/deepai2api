// Clever AI Hub 反代层：网关 Key → 号池账号 → `omni/response` SSE → OpenAI / Anthropic。
//
// 为什么不能直接把 web 会话丢给下游用：下游是编程工具（Cline / Roo / Continue /
// Claude Code），它们只会说 OpenAI 或 Anthropic 协议，而且要 function calling。
// 这一层负责三件事：
//
//   1. 选号 + 续期：refreshToken 换 Firebase idToken，App Check 令牌过期就重取；
//   2. 协议翻译：`messages[]` ↔ `omni` 的 dialog 结构，`tools[]` ↔ `options.tools`，
//      SSE 里的 `text` / `tool_call` / `tool_result` ↔ OpenAI 的
//      `content` / `tool_calls` / `role:"tool"`；
//   3. 出 Key：号池账号与 `sk-dcg-…` 一一绑定，Key 只在这里兑换成上游会话。
import { spawn } from "node:child_process";
import { existsSync } from "node:fs";
import { dirname, join, resolve } from "node:path";
import { fileURLToPath } from "node:url";
import { randomUUID } from "node:crypto";

const here = dirname(fileURLToPath(import.meta.url));
const repoRoot = resolve(here, "..");

const APP_ID = "1:387451343950:web:7098a29d2a2aa91c751f1f";
const WEB_API_KEY = "AIzaSyAdbIs_1isauoMViqf4rmpGUH5UFb3z_JQ";
// Azure Functions 主机密钥：从 CleverAI 的 web bundle 里读到的，不是用户级凭据，
// 但它仍然是一把能直接打上游的函数钥匙 —— 不放仓库，走环境变量。
// 缺失时上游会回 401，这里提前给出明确提示（否则只会看到 x-functions-key 被拒）。
const FUNCTIONS_KEY = String(process.env.CLEVERAI_FUNCTIONS_KEY ?? "").trim();
const APP_VERSION = "12.2.0";
const SECURETOKEN = "https://securetoken.googleapis.com/v1/token";
const API_BASE = "https://api.cleverai.app/appserver/api/v1";

// 上游的代币计价按 engine 走；未知模型按最便宜的一档算，避免直接吃 no_credit。
export const DEFAULT_MODEL_COST = 2;

// 从 `GET /configs?type=engines` 抄下来的常见档位（2026-11 实测）。
const ENGINE_COSTS = {
  "openai-gpt-6-luna": 2,
  "openai-gpt-5-6-luna": 5,
  "openai-gpt-5-6-terra-instant": 5,
  "openai-gpt-6-1-terra": 8,
  "antropic-calude-sonnet-5-5": 10,
  "google-gemini-3-8-flash": 10,
  "xai-grok-4-7": 10,
  "openai-gpt-6-sol": 15,
  "openai-gpt-6-1-sol": 15,
  "openai-gpt-6-2-sol": 15,
  "meta-muse-wonder-1-0": 20,
  "antropic-calude-opus-5-5": 20,
  "google-gemini-4-neon": 23,
  "google-gemini-4-argon": 30,
  "openai-gpt-5-6-sol": 30,
  "antropic-calude-fable-5-2": 40,
  "openai-gpt-6-2-astra": 60,
  "openai-gpt-6-astra": 75,
};

const MODEL_ALIASES = {
  // 下游习惯的名字 → 上游 engineId。没命中的原样透传。
  "gpt-4o": "openai-gpt-6-luna",
  "gpt-4o-mini": "openai-gpt-6-luna",
  "gpt-4.1": "openai-gpt-6-1-terra",
  "claude-3-5-sonnet": "antropic-calude-sonnet-5-5",
  "claude-sonnet-4": "antropic-calude-sonnet-5-5",
  "gemini-2.5-flash": "google-gemini-3-8-flash",
  "grok-4": "xai-grok-4-7",
  "cleverai/default": "openai-gpt-6-luna",
};

export const resolveEngine = (model) => {
  const raw = String(model ?? "").trim();
  if (!raw) return "openai-gpt-6-luna";
  return MODEL_ALIASES[raw] ?? MODEL_ALIASES[raw.toLowerCase()] ?? raw.replace(/^cleverai\//, "");
};

/**
 * 上游的"占位回答"识别。
 *
 * 实测（见 docs/CLEVERAI-POOL.md 的取证表）：这些 engine 的模型层对普通账号
 * 只会回一句按品牌定制的套话，不是真正的模型输出 —— OpenAI 系回 "please update"，
 * Anthropic/Gemini 系回 "heavy traffic"，而且流前都带 "**Clever AI** is analyzing"。
 * 反代仍然照常转发，但要把它标出来，别让下游把套话当成模型答案。
 */
const PLACEHOLDER_PATTERNS = [
  { code: "update_required", re: /please update to the latest version/i },
  { code: "heavy_traffic", re: /experiencing heavy traffic/i },
  { code: "analyzing", re: /clever ai\s*[^\n]{0,4}\s*is\s+_?analyzing_?\s+your request/i },
];

export function detectPlaceholder(text) {
  const blob = String(text ?? "");
  if (!blob.trim()) return "";
  for (const { code, re } of PLACEHOLDER_PATTERNS) {
    if (re.test(blob)) return code;
  }
  return "";
}

const now = () => Date.now();

/* ----------------------------------------------------------------- 令牌 */

class TokenVault {
  /**
   * App Check JWT 由浏览器产出，网关这边只缓存与续期。
   *
   * 续期走 python worker 的 `appcheck` 子命令（它会开一次 Camoufox 读 SPA 自己
   * 换的那枚令牌）。一枚令牌 3600s，整批账号共用。
   */
  constructor({ toolDir, python, log = () => {} }) {
    this.toolDir = toolDir;
    this.python = python;
    this.log = log;
    this.token = "";
    this.expiresAt = 0;
    this.pending = null;
  }

  seed(token, ttlSeconds = 3300) {
    if (!token) return;
    this.token = String(token);
    this.expiresAt = now() + Math.max(60, Number(ttlSeconds) || 3300) * 1000;
  }

  valid() {
    return Boolean(this.token) && this.expiresAt - 120_000 > now();
  }

  async get({ force = false } = {}) {
    if (!force && this.valid()) return this.token;
    if (this.pending) return this.pending;
    this.pending = this.#mint().finally(() => {
      this.pending = null;
    });
    return this.pending;
  }

  async #mint() {
    const cli = join(this.toolDir, "cleverai_reg", "cli.py");
    if (!existsSync(cli)) {
      throw new Error(`找不到注册工具：${this.toolDir}（可用 CLEVERAI_REGISTER_DIR 指定）`);
    }
    this.log("info", "cleverai: 续期 App Check 令牌（浏览器取一次，整批复用）");
    const output = await new Promise((done, fail) => {
      const child = spawn(this.python, ["-m", "cleverai_reg", "--json", "appcheck"], {
        cwd: this.toolDir,
        windowsHide: true,
        env: { ...process.env, PYTHONUTF8: "1", PYTHONIOENCODING: "utf-8", PYTHONUNBUFFERED: "1" },
      });
      let buffer = "";
      child.stdout.setEncoding("utf8");
      child.stdout.on("data", (chunk) => {
        buffer += chunk;
      });
      child.on("error", fail);
      child.on("close", () => done(buffer));
    });
    for (const line of String(output).split(/\r?\n/)) {
      const trimmed = line.trim();
      if (!trimmed.startsWith("{")) continue;
      try {
        const parsed = JSON.parse(trimmed);
        if (parsed.ok && parsed.token) {
          this.token = String(parsed.token);
          this.expiresAt = now() + Number(parsed.stats?.expires_in ?? 3300) * 1000;
          return this.token;
        }
      } catch {
        /* 非 JSON 行是日志，忽略 */
      }
    }
    throw new Error("App Check 令牌续期失败：worker 没有返回 token");
  }
}

/* ------------------------------------------------------------ 号池账号 */

async function refreshFirebase(account, { proxyUrl = "" } = {}) {
  if (!account.refreshToken) throw new Error("账号没有 refreshToken");
  const response = await fetch(`${SECURETOKEN}?key=${WEB_API_KEY}`, {
    method: "POST",
    headers: { "Content-Type": "application/x-www-form-urlencoded" },
    body: new URLSearchParams({ grant_type: "refresh_token", refresh_token: account.refreshToken }),
  });
  if (!response.ok) {
    const detail = await response.text();
    const error = new Error(`firebase refresh failed (HTTP ${response.status})`);
    error.detail = detail.slice(0, 200);
    error.status = response.status;
    throw error;
  }
  const payload = await response.json();
  return {
    idToken: String(payload.id_token ?? ""),
    refreshToken: String(payload.refresh_token ?? account.refreshToken),
  };
}

/** 函数密钥缺失时立刻报清楚，而不是让上游回一个含糊的 401。 */
export function requireFunctionsKey() {
  if (!FUNCTIONS_KEY) {
    const error = new Error(
      "缺少 CLEVERAI_FUNCTIONS_KEY：从 CleverAI web bundle 里取 x-functions-key 的值，写进环境变量后再启动。",
    );
    error.status = 500;
    error.code = "missing_functions_key";
    throw error;
  }
  return FUNCTIONS_KEY;
}

function upstreamHeaders(account, idToken, appCheck) {
  const device = account.device && Object.keys(account.device).length
    ? account.device
    : { deviceType: "DESKTOP", platform: "web" };
  return {
    Authorization: `Bearer ${idToken}`,
    "x-functions-key": FUNCTIONS_KEY,
    "Accept-Language": "en",
    Version: APP_VERSION,
    "X-Version": APP_VERSION,
    "X-App-ID": APP_ID,
    "X-UID": account.uid || "UNKNOWN",
    "X-Platform": "web",
    "X-DEVICE": JSON.stringify(device),
    "X-Firebase-AppCheck": appCheck,
    "Content-Type": "application/json",
    Accept: "text/event-stream",
    "Cache-Control": "no-cache",
    "X-Requested-With": "XMLHttpRequest",
    "X-Experience-Type": "DEFAULT",
  };
}

/* ------------------------------------------------------- 消息与工具翻译 */

/** OpenAI `messages[]` → 上游 dialog `messages[]`。 */
export function toDialogMessages(messages = [], { module = "CHAT" } = {}) {
  const out = [];
  for (const message of messages) {
    const role = String(message?.role ?? "user");
    const text = typeof message?.content === "string" ? message.content : textFromParts(message?.content);
    if (role === "tool") {
      // OpenAI 的 tool 结果带 tool_call_id；上游用 tool_result 内容项表达。
      out.push({
        id: randomUUID(),
        type: "ai",
        module,
        contents: [
          {
            type: "tool_result",
            tool_call_id: String(message?.tool_call_id ?? ""),
            name: String(message?.name ?? ""),
            text: text || "",
          },
        ],
        createdAt: now(),
        platform: { type: "web", appVersion: APP_VERSION, appBuildNumber: 1 },
      });
      continue;
    }
    const contents = [];
    if (text) contents.push({ type: "text", text, item_id: randomUUID() });
    for (const call of message?.tool_calls ?? []) {
      contents.push({
        type: "tool_call",
        id: String(call.id ?? randomUUID()),
        name: String(call.function?.name ?? ""),
        arguments: safeJson(call.function?.arguments),
      });
    }
    if (!contents.length) continue;
    out.push({
      id: randomUUID(),
      type: role === "assistant" ? "ai" : "human",
      module,
      contents,
      createdAt: now(),
      platform: { type: "web", appVersion: APP_VERSION, appBuildNumber: 1 },
    });
  }
  return out;
}

function textFromParts(parts) {
  if (!Array.isArray(parts)) return "";
  return parts
    .map((part) => (typeof part === "string" ? part : String(part?.text ?? part?.content ?? "")))
    .filter(Boolean)
    .join("\n");
}

function safeJson(value) {
  if (value && typeof value === "object") return value;
  try {
    return JSON.parse(String(value ?? "{}"));
  } catch {
    return {};
  }
}

/** OpenAI `tools[]` → 上游 `options.tools[]`（上游就是函数描述列表）。 */
export function toUpstreamTools(tools) {
  if (!Array.isArray(tools)) return [];
  return tools
    .map((tool) => {
      const fn = tool?.function ?? tool;
      if (!fn?.name) return null;
      return {
        name: String(fn.name),
        description: String(fn.description ?? ""),
        parameters: fn.parameters ?? { type: "object", properties: {} },
        ...(tool?.type && tool.type !== "function" ? { type: String(tool.type) } : {}),
      };
    })
    .filter(Boolean);
}

/** 上游 SSE delta → 归一化增量。 */
export function normalizeDelta(event) {
  const delta = event?.delta ?? {};
  const out = { text: "", thinking: "", toolCalls: [], finish: null };
  for (const item of delta.content ?? []) {
    if (!item || typeof item !== "object") continue;
    if (item.type === "text") out.text += String(item.text ?? "");
    else if (item.type === "thinking_text") out.thinking += String(item.text ?? "");
    else if (item.type === "tool_call") {
      out.toolCalls.push({
        id: String(item.id ?? randomUUID()),
        name: String(item.name ?? item.tool ?? ""),
        arguments: typeof item.arguments === "string" ? item.arguments : JSON.stringify(item.arguments ?? {}),
      });
    }
  }
  const finish = event?.finishReason;
  if (finish === "stop") out.finish = out.toolCalls.length ? "tool_calls" : "stop";
  else if (finish === "error") out.finish = "error";
  else if (finish === "length") out.finish = "length";
  return out;
}

/* ---------------------------------------------------------------- relay */

export class CleveraiRelay {
  /**
   * @param {{pool: object, store?: object, dataDir?: string, log?: Function,
   *          toolDir?: string, python?: string, engine?: string}} options
   */
  constructor({ pool, store = null, dataDir = "", log = () => {}, toolDir, python, engine } = {}) {
    this.pool = pool;
    this.store = store;
    this.dataDir = dataDir;
    this.log = log;
    this.toolDir = toolDir || process.env.CLEVERAI_REGISTER_DIR || join(repoRoot, "tools", "cleverai-reg");
    this.python = python || process.env.CLEVERAI_PYTHON || (process.platform === "win32" ? "python" : "python3");
    this.defaultEngine = engine || pool?.engine || "openai-gpt-6-luna";
    this.vault = new TokenVault({ toolDir: this.toolDir, python: this.python, log });
    const seed = process.env.CLEVERAI_APPCHECK_TOKEN;
    if (seed) this.vault.seed(seed, Number(process.env.CLEVERAI_APPCHECK_TTL ?? 3300));
  }

  status() {
    return {
      ok: true,
      pool: this.pool?.stats?.() ?? null,
      appCheck: { ready: this.vault.valid(), expiresIn: Math.max(0, Math.round((this.vault.expiresAt - now()) / 1000)) },
      engine: this.defaultEngine,
      toolDir: this.toolDir,
      python: this.python,
    };
  }

  /** 一轮对话的 credit 成本：按 engine 查表，未知按最便宜一档。 */
  costFor(engine) {
    const id = String(engine ?? "").replace(/^cleverai\//, "");
    return ENGINE_COSTS[id] ?? DEFAULT_MODEL_COST;
  }

  /**
   * 账号套餐。上游按这个字段校验权限，发错反而被拒；没有订阅记录就是 FREE。
   */
  userType(account) {
    const explicit = account?.subscription ?? account?.userType;
    if (explicit === "PRO" || explicit === "PRO_TRIAL" || explicit === "FREE") return explicit;
    return "FREE";
  }

  /** 选号 + 保证 idToken / AppCheck 都新鲜。 */
  async acquire({ exclude = [], model = "" } = {}) {
    const account = this.pool.pick({ exclude });
    if (!account) {
      const error = new Error("号池里没有可用账号：先把 cleverai 账号注册进池，或清掉冷却。");
      error.status = 503;
      error.code = "no_account";
      throw error;
    }
    let idToken = account.idToken || "";
    const stale = !idToken || jwtExpiry(idToken) - 60_000 < now();
    if (stale) {
      try {
        const fresh = await refreshFirebase(account);
        idToken = fresh.idToken;
        this.pool.update(account.id, { idToken: fresh.idToken, refreshToken: fresh.refreshToken || account.refreshToken });
      } catch (error) {
        this.pool.recordResult(account.id, { ok: false, error: `refresh: ${error.message}`, cooldownMs: 10 * 60_000 });
        const wrapped = new Error(`账号 ${account.email} 的 refreshToken 失效：${error.message}`);
        wrapped.status = 502;
        wrapped.code = "account_dead";
        throw wrapped;
      }
    }
    const appCheck = await this.vault.get();
    return { account, idToken, appCheck, engine: resolveEngine(model || this.defaultEngine) };
  }

  /**
   * 一次对话。返回归一化事件的异步迭代器：
   *   {type:"text", text} | {type:"tool_call", call} | {type:"done", finish, wallet, engine}
   */
  async *chat({ messages, model = "", tools = null, stream = true, signal = null, walletCost = DEFAULT_MODEL_COST, exclude = [] } = {}) {
    const { account, idToken, appCheck, engine } = await this.acquire({ exclude, model });
    const dialog = toDialogMessages(messages);
    if (!dialog.length) {
      const error = new Error("messages 为空");
      error.status = 400;
      throw error;
    }
    const cost = walletCost ?? this.costFor(engine);
    const body = {
      messages: dialog,
      thread: { id: randomUUID() },
      appId: APP_ID,
      platform: "web",
      engine,
      module: "CHAT",
      // SPA 在没选过滤条件时发的是字符串 "auto"（不是空数组），照抄更稳。
      filters: "auto",
      options: {
        // 账号的套餐：没买就是 FREE。发 PRO 会让上游按 PRO 权限校验，反而更容易被拒。
        type: this.userType(account),
        language: "en",
        tools: toUpstreamTools(tools),
      },
      model: engine,
      deviceType: "unknown",
      sequenceNumber: 0,
      wallet: { ...(account.wallet ?? {}), cost },
      device: account.device ?? { deviceType: "DESKTOP", platform: "web" },
      isDemo: false,
      metadata: { outputProtocol: "mixed-v1", requestId: randomUUID(), capabilities: ["mcp-app-v1"] },
    };

    let response;
    try {
      response = await fetch(`${API_BASE}/omni/response`, {
        method: "POST",
        headers: upstreamHeaders(account, idToken, appCheck),
        body: JSON.stringify(body),
        signal: signal ?? undefined,
      });
    } catch (error) {
      this.pool.recordResult(account.id, { ok: false, error: `transport: ${error.message}`, cooldownMs: 60_000 });
      throw error;
    }

    if (!response.ok) {
      const detail = await response.text().catch(() => "");
      await this.#handleFailure(account, response.status, detail);
      const error = new Error(`上游拒绝（HTTP ${response.status}）`);
      error.status = response.status;
      error.detail = detail.slice(0, 300);
      throw error;
    }

    let finish = "stop";
    const pendingCalls = [];
    let answer = "";
    for await (const event of iterateSse(response.body, signal)) {
      const delta = normalizeDelta(event);
      if (delta.thinking) {
        // 上游的 thinking 只在需要时透出，OpenAI 侧没有对应字段，丢掉更稳。
      }
      if (delta.text) {
        answer += delta.text;
        yield { type: "text", text: delta.text, engine, account: account.email };
      }
      for (const call of delta.toolCalls) {
        pendingCalls.push(call);
        yield { type: "tool_call", call, engine, account: account.email };
      }
      const wallet = event?.wallet;
      if (wallet) this.pool.recordWallet(account.id, wallet);
      if (delta.finish) {
        finish = delta.finish;
        break;
      }
      if (event?.finishReason) {
        finish = event.finishReason === "error" ? "error" : delta.finish || finish;
        if (event.finishReason === "error") break;
      }
    }
    const notice = detectPlaceholder(answer);
    if (notice) {
      this.pool.recordNotice(account.id, notice);
      this.log("warn", `cleverai: ${account.email} 收到上游占位回答（${notice}），已标记`);
    }
    this.pool.recordResult(account.id, { ok: finish !== "error", error: finish === "error" ? "upstream error" : null, cooldownMs: finish === "error" ? 120_000 : 0 });
    if (notice) yield { type: "notice", code: notice, engine, account: account.email };
    yield { type: "done", finish, engine, account: account.email, notice, wallet: this.pool.getAccount(account.id)?.wallet ?? null };
  }

  async #handleFailure(account, status, detail) {
    const text = String(detail || "").slice(0, 400);
    if (status === 401 && /app.?check/i.test(text)) {
      this.log("warn", "cleverai: App Check 令牌被拒，强制续期");
      try {
        await this.vault.get({ force: true });
      } catch (error) {
        this.log("warn", `cleverai: 续期失败 ${error.message}`);
      }
      this.pool.recordResult(account.id, { ok: false, error: "app_check_rejected", cooldownMs: 30_000 });
      return;
    }
    if (status === 401) {
      this.pool.update(account.id, { idToken: "" });
      this.pool.recordResult(account.id, { ok: false, error: "unauthorized", cooldownMs: 30_000 });
      return;
    }
    if (status === 429) {
      this.pool.recordResult(account.id, { ok: false, error: "rate_limited", cooldownMs: 5 * 60_000 });
      return;
    }
    if (/no credit|insufficient|paywall/i.test(text)) {
      this.pool.recordResult(account.id, { ok: false, error: "no_credit", cooldownMs: 30 * 60_000 });
      return;
    }
    this.pool.recordResult(account.id, { ok: false, error: `HTTP ${status}`, cooldownMs: 60_000 });
  }

  /* ------------------------------------------------- OpenAI 形状的出口 */

  async complete({ messages, model, tools, signal, walletCost } = {}) {
    const started = now();
    const textParts = [];
    const calls = [];
    let engine = "";
    let finish = "stop";
    let notice = "";
    for await (const event of this.chat({ messages, model, tools, signal, walletCost })) {
      if (event.type === "text") textParts.push(event.text);
      else if (event.type === "tool_call") calls.push(event.call);
      else if (event.type === "notice") notice = event.code;
      else if (event.type === "done") {
        engine = event.engine;
        finish = event.finish;
        notice = event.notice || notice;
      }
    }
    const message = { role: "assistant", content: textParts.join("") || (calls.length ? null : "") };
    if (calls.length) {
      message.tool_calls = calls.map((call, index) => ({
        id: call.id,
        type: "function",
        index,
        function: { name: call.name, arguments: call.arguments || "{}" },
      }));
    }
    return {
      id: `chatcmpl-${randomUUID()}`,
      object: "chat.completion",
      created: Math.floor(started / 1000),
      model: engine || resolveEngine(model),
      choices: [{ index: 0, message, finish_reason: calls.length ? "tool_calls" : finish === "error" ? "stop" : finish }],
      usage: { prompt_tokens: 0, completion_tokens: 0, total_tokens: 0 },
      ...(notice ? { "x_cleverai_notice": notice } : {}),
    };
  }

  async *streamOpenAI({ messages, model, tools, signal, walletCost } = {}) {
    const id = `chatcmpl-${randomUUID()}`;
    const created = Math.floor(now() / 1000);
    let engine = resolveEngine(model);
    let index = 0;
    let finish = "stop";
    const toolIndex = new Map();

    yield sseChunk({ id, created, model: engine, delta: { role: "assistant", content: "" } });
    for await (const event of this.chat({ messages, model, tools, signal, walletCost })) {
      if (event.type === "text") {
        yield sseChunk({ id, created, model: engine, delta: { content: event.text } });
      } else if (event.type === "tool_call") {
        if (!toolIndex.has(event.call.id)) {
          toolIndex.set(event.call.id, toolIndex.size);
          yield sseChunk({
            id,
            created,
            model: engine,
            delta: {
              tool_calls: [
                {
                  index: toolIndex.get(event.call.id),
                  id: event.call.id,
                  type: "function",
                  function: { name: event.call.name, arguments: "" },
                },
              ],
            },
          });
        }
        yield sseChunk({
          id,
          created,
          model: engine,
          delta: {
            tool_calls: [
              { index: toolIndex.get(event.call.id), function: { arguments: event.call.arguments || "{}" } },
            ],
          },
        });
        index++;
      } else if (event.type === "notice") {
        // SSE 注释帧：客户端会忽略，抓包/日志里能一眼看出这是上游占位回答。
        yield `: cleverai-notice=${event.code}\n\n`;
      } else if (event.type === "done") {
        engine = event.engine;
        finish = toolIndex.size ? "tool_calls" : event.finish;
      }
    }
    yield sseChunk({ id, created, model: engine, delta: {}, finish_reason: finish });
    yield "data: [DONE]\n\n";
  }
}

function sseChunk({ id, created, model, delta, finish_reason = null }) {
  return (
    "data: " +
    JSON.stringify({
      id,
      object: "chat.completion.chunk",
      created,
      model,
      choices: [{ index: 0, delta, finish_reason }],
    }) +
    "\n\n"
  );
}

async function* iterateSse(body, signal) {
  if (!body) return;
  const decoder = new TextDecoder();
  let buffer = "";
  for await (const chunk of body) {
    if (signal?.aborted) return;
    buffer += decoder.decode(chunk, { stream: true });
    let boundary;
    while ((boundary = buffer.indexOf("\n\n")) !== -1) {
      const block = buffer.slice(0, boundary);
      buffer = buffer.slice(boundary + 2);
      for (const line of block.split("\n")) {
        const trimmed = line.trim();
        if (!trimmed.startsWith("data:")) continue;
        const payload = trimmed.slice(5).trim();
        if (!payload) continue;
        try {
          const event = JSON.parse(payload);
          if (event && typeof event === "object") yield event;
        } catch {
          /* 半包/心跳行忽略 */
        }
      }
    }
  }
}

function jwtExpiry(token) {
  try {
    const payload = String(token).split(".")[1] ?? "";
    const json = JSON.parse(Buffer.from(payload, "base64url").toString("utf8"));
    return Number(json.exp ?? 0) * 1000;
  } catch {
    return 0;
  }
}
