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
import { spawnSync } from "node:child_process";
import { dirname, join } from "node:path";

import { chatToMessage, messagesToChat, streamAsAnthropic } from "./anthropic.mjs";
import { extractPresentedKey, hashKey, looksLikeGatewayKey, mintKey } from "./keys.mjs";
import { CALLBACK_PATH, SignInError, beginSignIn, exchangeCode, signInUrl, stateMatches } from "./oauth.mjs";
import { Registrations } from "./register.mjs";
import { AllmodelsPool, importAccount, maskAccount } from "./allmodels-pool.mjs";
import { AllmodelsRegistrations } from "./allmodels-register.mjs";
import { ToolError, createToolRegistry } from "./allmodels-tools.mjs";
import {
  CleveraiPool,
  importAccount as importCleveraiAccount,
  maskAccount as maskCleveraiAccount,
} from "./cleverai-pool.mjs";
import { CleveraiRegistrations } from "./cleverai-register.mjs";
import { CleveraiRelay, resolveEngine as resolveCleveraiEngine } from "./cleverai-relay.mjs";
import { GabaiPool, importAccount as importGabaiAccount, maskAccount as maskGabaiAccount } from "./gabai-pool.mjs";
import { GabaiRegistrations } from "./gabai-register.mjs";
import { GAB_PATHS, GabaiProxy } from "./gabai-proxy.mjs";
import {
  ProxyscrapePool,
  maskAccount as maskProxyscrapeAccount,
  maskProxy as maskProxyscrapeProxy,
} from "./proxyscrape-pool.mjs";
import { ProxyscrapeRegistrations } from "./proxyscrape-register.mjs";
import { applyProxies, proxyShareStatus, withSharedProxies } from "./proxy-share.mjs";
import {
  DEFAULT_FORMAT as DEFAULT_PROXY_FORMAT,
  FORMATS as PROXY_FORMATS,
  ShareStore as ProxyShareStore,
  exportFileName as proxyExportFileName,
  maskShare as maskProxyShare,
  renderProxies,
} from "./proxy-export.mjs";
import { probeProxies } from "./proxy-check.mjs";
import { solveTurnstile, solverStatus } from "./captcha.mjs";
import {
  JevaiPool,
  importAccount as importJevaiAccount,
  maskAccount as maskJevaiAccount,
} from "./jevai-pool.mjs";
import { JevaiRegistrations } from "./jevai-register.mjs";
import { LINE as AMD_LINE, instanceSummary as amdInstanceSummary } from "./amd-pool.mjs";
import { JevaiRelay, explainUpstreamError } from "./jevai-relay.mjs";
import {
  LinePool,
  LineRegistrations,
  LineRelay,
  defineLine,
  explainModelError,
  importAccount as importLineAccount,
  maskAccount as maskLineAccount,
} from "./openai-lines.mjs";
import { PoePool } from "./poe-pool.mjs";
import { createProviderRuntime } from "./providers.mjs";
import { createProviderAdmin } from "./provider-admin.mjs";
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

/**
 * Provider 端点覆盖：`config.providerEndpoints` 优先，其次
 * `DCG_PROVIDER_ENDPOINTS`（形如 {"moonai":{"apiBase":"http://…"}}）。
 * 只用于测试 / 自建反代，正常运行留空。
 */
const providerEndpointOverrides = (config) => {
  const fromConfig = config?.providerEndpoints;
  if (fromConfig && typeof fromConfig === "object") return fromConfig;
  return parseAliases(process.env.DCG_PROVIDER_ENDPOINTS);
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

  /**
   * Provider 运行时（wavespeed / novita / moonai …）：
   * 每个 provider 一份号池 + 一份注册控制器 + 共用反代层。
   * 路由命名空间独立：`/p/<provider>/v1/*` 与 `/api/admin/providers/*`，
   * 与上面的 DeepAI `/v1/*`、`/api/admin/allmodels/*`、`/api/poe/*` 完全不重叠。
   * 环境变量 `DCG_PROVIDERS` 可限定只装哪几个（逗号分隔）。
   */
  const providerRuntime = createProviderRuntime({
    dataDir: config.dataDir,
    port: config.port,
    token: adminToken,
    store,
    config,
    enabled: String(process.env.DCG_PROVIDERS ?? "")
      .split(",")
      .map((id) => id.trim())
      .filter(Boolean),
    // 端点覆盖：配置对象里显式给，或由 DCG_PROVIDER_ENDPOINTS 传 JSON。
    // 测试与自建反代用它把上游指到本地，不污染全局环境变量。
    endpointOverrides: providerEndpointOverrides(config),
    log: (level, message) => log(level === "warn" ? "warn" : "info", message),
  });
  const providerAdmin = createProviderAdmin({ runtime: providerRuntime, store, log });

  /**
   * Clever AI Hub 号池：独立于 DeepAI / Allmodels / provider 运行时。
   *
   * 凭据是 Firebase refreshToken（长期）+ App Check JWT（3600s，转发层自动续），
   * 不是面板 api key。命名空间也独立：`/cleverai/v1/*` 与 `/api/admin/cleverai/*`，
   * 与其它反代面零重叠（docs/CLEVERAI-POOL.md）。
   */
  const cleveraiPool = CleveraiPool.at(config.dataDir, {
    baseUrl: process.env.CLEVERAI_BASE_URL || "https://cleverai.app",
    apiBase: process.env.CLEVERAI_API_BASE || "https://api.cleverai.app",
    engine: process.env.CLEVERAI_CHAT_ENGINE || "openai-gpt-6-luna",
    log: (level, message) => log(level === "warn" ? "warn" : "info", message),
  });

  const cleverai = new CleveraiRegistrations({
    dataDir: config.dataDir,
    port: config.port,
    token: adminToken,
    pool: cleveraiPool,
    store,
    log: (level, message) => log(level === "warn" ? "warn" : "info", message),
    onAccount: ({ account, gatewayKey }) => {
      log(
        "info",
        `cleverai 入池 ${account.email}（号池 ${cleveraiPool.size}，网关 Key ${gatewayKey?.prefix ?? "-"}）`,
      );
    },
  });

  const cleveraiRelay = new CleveraiRelay({
    pool: cleveraiPool,
    store,
    dataDir: config.dataDir,
    engine: cleveraiPool.engine,
    log: (level, message) => log(level === "warn" ? "warn" : "info", message),
  });

  /**
   * gab.ai 号池：凭据是面板签发的 `gab_…` key，上游本身就说 OpenAI / Anthropic，
   * 所以反代层只做换 Authorization + 轮询 + 故障切换，不需要形状翻译。
   * 命名空间 `/gab/v1/*`、`/api/admin/gabai/*`，与其它线零重叠。
   */
  const gabaiPool = GabaiPool.at(config.dataDir, {
    baseUrl: process.env.GABAI_BASE_URL || "https://gab.ai",
    apiBase: process.env.GABAI_API_BASE || "https://gab.ai/v1",
    log: (level, message) => log(level === "warn" ? "warn" : "info", message),
  });

  const gabai = new GabaiRegistrations({
    dataDir: config.dataDir,
    port: config.port,
    token: adminToken,
    pool: gabaiPool,
    store,
    log: (level, message) => log(level === "warn" ? "warn" : "info", message),
    onAccount: ({ account, gatewayKey }) => {
      log("info", `gabai 入池 ${account.email}（号池 ${gabaiPool.size}，网关 Key ${gatewayKey?.prefix ?? "-"}）`);
    },
  });

  const gabaiProxy = new GabaiProxy({
    pool: gabaiPool,
    log: (level, message) => log(level === "warn" ? "warn" : "info", message),
    upstreamBase: process.env.GABAI_UPSTREAM || "https://gab.ai",
  });

  /**
   * proxyscrape.com 号池：唯一一条**产物是代理**的线。
   *
   * 账号领 premium 试用 → 试用给出 `host:port:user:pass` → 写进 `proxies[]` →
   * 任务结束自动 `applyProxies()` 分发成 `data/proxies/<line>.txt`，
   * 其它注册机在没显式传 `--proxy` 时自动吃这些出口。
   */
  const proxyscrapePool = ProxyscrapePool.at(config.dataDir, {
    baseUrl: process.env.PROXYSCRAPE_BASE_URL || "https://dashboard.proxyscrape.com",
    log: (level, message) => log(level === "warn" ? "warn" : "info", message),
  });

  const proxyscrape = new ProxyscrapeRegistrations({
    dataDir: config.dataDir,
    port: config.port,
    token: adminToken,
    pool: proxyscrapePool,
    log: (level, message) => log(level === "warn" ? "warn" : "info", message),
    onAccount: ({ account, proxies }) => {
      log("info", `proxyscrape 入池 ${account.email}（+${proxies} 条代理，池内 ${proxyscrapePool.stats().proxies}）`);
    },
  });

  /**
   * jev-ai.org 号池：产物是 `sk-glm5-…` Key（基址 https://jev-ai.org/api/v1）。
   * 任务结束自动写 `data/keys/jevai.{txt,json}`，其它工具直接接入；
   * 网关自己则通过 `/jevai/v1/*` 把这批 Key 包成一个 OpenAI 兼容面。
   */
  const jevaiPool = JevaiPool.at(config.dataDir, {
    baseUrl: process.env.JEVAIS_BASE_URL || "https://jev-ai.org",
    apiBase: process.env.JEVAIS_API_BASE || "https://jev-ai.org/api/v1",
    log: (level, message) => log(level === "warn" ? "warn" : "info", message),
  });

  const jevai = new JevaiRegistrations({
    dataDir: config.dataDir,
    port: config.port,
    token: adminToken,
    pool: jevaiPool,
    store,
    log: (level, message) => log(level === "warn" ? "warn" : "info", message),
    onAccount: ({ account, gatewayKey }) => {
      log("info", `jevai 入池 ${account.email}（号池 ${jevaiPool.size}，网关 Key ${gatewayKey?.prefix ?? "-"}）`);
    },
  });

  const jevaiRelay = new JevaiRelay({
    pool: jevaiPool,
    log: (level, message) => log(level === "warn" ? "warn" : "info", message),
    upstreamBase: process.env.JEVAIS_UPSTREAM || "https://jev-ai.org",
  });

  /**
   * 六条 OpenAI 兼容线（agnes / cerebras / opencode-zen / requesty / unorouter / pollinations）。
   * 站点差异全在描述里，代码只有一份（src/openai-lines.mjs）。
   */
  const OPENAI_LINES = [
    defineLine({
      id: "agnes",
      name: "Agnes AI",
      baseUrl: "https://api.agnes-ai.cn",
      apiBase: "https://api.agnes-ai.cn/v1",
      keyPrefix: "sk-",
      models: ["agnes-3.0-flash", "agnes-2.5-flash"],
      envPrefix: "AGNES_",
      note: "无限期免费、不限 token、约 20 RPM",
    }),
    defineLine({
      id: "cerebras",
      name: "Cerebras",
      baseUrl: "https://cloud.cerebras.ai",
      apiBase: "https://api.cerebras.ai/v1",
      keyPrefix: "csk-",
      models: ["llama-3.3-70b", "qwen-3-235b-a22b"],
      envPrefix: "CEREBRAS_",
    }),
    defineLine({
      id: "opencode-zen",
      name: "OpenCode Zen",
      baseUrl: "https://opencode.ai",
      apiBase: "https://opencode.ai/zen/v1",
      keyPrefix: "sk-",
      freeSuffix: "-free",
      models: ["mimo-v2.5-free", "nemotron-3-ultra-free"],
      envPrefix: "OPENCODE_ZEN_",
    }),
    defineLine({
      id: "requesty",
      name: "Requesty",
      baseUrl: "https://app.requesty.ai",
      apiBase: "https://router.requesty.ai/v1",
      keyPrefix: "sk-",
      models: ["nvidia/nemotron-3-ultra", "gemma-4-31b"],
      envPrefix: "REQUESTY_",
    }),
    defineLine({
      id: "unorouter",
      name: "UnoRouter",
      baseUrl: "https://unorouter.com",
      apiBase: "https://api.unorouter.com/v1",
      keyPrefix: "sk-",
      freeSuffix: ":free",
      anthropic: true,
      models: ["deepseek-v4-flash:free", "glm-5.2:free"],
      envPrefix: "UNOROUTER_",
    }),
    defineLine({
      id: "giteeai",
      name: "Gitee AI",
      baseUrl: "https://ai.gitee.com",
      apiBase: process.env.GITEEAI_UPSTREAM || "https://ai.gitee.com/v1",
      keyPrefix: "",
      imagesOnly: true,
      models: ["z-image-turbo", "Qwen-Image-Edit"],
      envPrefix: "GITEEAI_",
      note: "文生图（OpenAI Images 形状）；令牌来自 gitee.com，没有机器注册接口",
    }),
    defineLine({
      id: "pollinations",
      name: "Pollinations",
      baseUrl: "https://gen.pollinations.ai",
      apiBase: "https://gen.pollinations.ai/v1",
      keyPrefix: "",
      anonymous: true,
      models: ["openai", "openai-large", "gemini"],
      envPrefix: "POLLINATIONS_",
    }),
  ];

  // 审计脚本按**字面量**核对这张表（scripts/check-loop.mjs），改动时保持同步。
  const LINE_ROUTE_LITERALS = {
    agnes: {
      relay: "/agnes/v1/",
      admin: ["/api/admin/agnes/status", "/api/admin/agnes/pool/import", "/api/admin/agnes/pool/export"],
    },
    cerebras: {
      relay: "/cerebras/v1/",
      admin: ["/api/admin/cerebras/status", "/api/admin/cerebras/pool/import", "/api/admin/cerebras/pool/export"],
    },
    "opencode-zen": {
      relay: "/opencode-zen/v1/",
      admin: [
        "/api/admin/opencode-zen/status",
        "/api/admin/opencode-zen/pool/import",
        "/api/admin/opencode-zen/pool/export",
      ],
    },
    requesty: {
      relay: "/requesty/v1/",
      admin: ["/api/admin/requesty/status", "/api/admin/requesty/pool/import", "/api/admin/requesty/pool/export"],
    },
    unorouter: {
      relay: "/unorouter/v1/",
      admin: ["/api/admin/unorouter/status", "/api/admin/unorouter/pool/import", "/api/admin/unorouter/pool/export"],
    },
    giteeai: {
      relay: "/giteeai/v1/",
      admin: ["/api/admin/giteeai/status", "/api/admin/giteeai/pool/import", "/api/admin/giteeai/pool/export"],
    },
    pollinations: {
      relay: "/pollinations/v1/",
      admin: [
        "/api/admin/pollinations/status",
        "/api/admin/pollinations/pool/import",
        "/api/admin/pollinations/pool/export",
      ],
    },
  };

  const linePools = new Map();
  const lineRegs = new Map();
  const lineRelays = new Map();
  for (const line of OPENAI_LINES) {
    const pool = LinePool.at(line, config.dataDir, {
      log: (level, message) => log(level === "warn" ? "warn" : "info", message),
    });
    linePools.set(line.id, pool);
    lineRegs.set(
      line.id,
      new LineRegistrations({
        line,
        dataDir: config.dataDir,
        pool,
        store,
        log: (level, message) => log(level === "warn" ? "warn" : "info", message),
        onAccount: ({ account, gatewayKey }) => {
          log("info", `${line.id} 入池 ${account.email}（号池 ${pool.size}，网关 Key ${gatewayKey?.prefix ?? "-"}）`);
        },
      }),
    );
    lineRelays.set(line.id, new LineRelay({ line, pool, log: (level, message) => log(level === "warn" ? "warn" : "info", message) }));
  }
  const lineById = (id) => OPENAI_LINES.find((line) => line.id === id) ?? null;

  /**
   * AMD 开发者云（GPU）：产物是"能起 MI300X 实验实例的账号 cookie"。
   * 它不是 OpenAI 兼容面（没有 /v1），所以只接管理面，不接反代。
   */
  const amdPool = LinePool.at(AMD_LINE, config.dataDir, {
    log: (level, message) => log(level === "warn" ? "warn" : "info", message),
  });
  const amdRegistrations = new LineRegistrations({
    line: AMD_LINE,
    dataDir: config.dataDir,
    pool: amdPool,
    store,
    log: (level, message) => log(level === "warn" ? "warn" : "info", message),
    onAccount: ({ account }) => {
      log("info", `amd 入池 ${account.email}（池内 ${amdPool.size}，积分 ${account.credits?.points ?? "?"}）`);
    },
  });

  // 代理订阅：给手机/路由器这类"不方便填管理口令"的设备用的只读链接
  const publicBase = String(config.publicBase ?? "").replace(/\/+$/, "");
  const shareUrlFor = (token) => `${publicBase}/proxy/${token}`;
  const proxyShares = new ProxyShareStore(config.dataDir);




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

  /* ------------------------------------------------- cleverai 反代出 Key */

  /**
   * `/cleverai/v1/*`：把号池账号包成 OpenAI 兼容面，给编程工具用。
   *
   * 与 `/v1/*`（DeepAI lane）和 `/p/<provider>/v1/*`（provider 运行时）是三条
   * 完全独立的路：鉴权都用 sk-dcg- Key，但落到不同的上游与不同的号池。
   * function calling 在 relay 里做了双向翻译，所以 Cline / Continue / Roo
   * 这类工具可以直接把它当 OpenAI 端点用。
   */
  async function handleCleverai(req, res, path, url, auth) {
    if (path === "/cleverai/v1/models" && req.method === "GET") {
      const catalog = [
        { id: "cleverai/default", engine: cleveraiPool.engine },
        { id: "openai-gpt-6-luna", engine: "openai-gpt-6-luna" },
        { id: "antropic-calude-sonnet-5-5", engine: "antropic-calude-sonnet-5-5" },
        { id: "google-gemini-3-8-flash", engine: "google-gemini-3-8-flash" },
        { id: "xai-grok-4-7", engine: "xai-grok-4-7" },
      ];
      return sendJson(res, 200, {
        object: "list",
        data: catalog.map((row) => ({
          id: row.id,
          object: "model",
          created: Math.floor(stats.startedAt / 1000),
          owned_by: "cleverai",
          engine: row.engine,
        })),
      });
    }

    if ((path === "/cleverai/v1/chat/completions" || path === "/cleverai/v1/completions") && req.method === "POST") {
      let body;
      try {
        body = await readJson(req);
      } catch (cause) {
        return fail(res, cause);
      }
      const model = String(body.model ?? "");
      const tools = Array.isArray(body.tools) ? body.tools : null;
      const messages = Array.isArray(body.messages)
        ? body.messages
        : typeof body.prompt === "string"
          ? [{ role: "user", content: body.prompt }]
          : [];
      if (!messages.length) {
        return sendJson(res, 400, errorBody("messages 不能为空。", "invalid_request_error", "bad_request"));
      }
      const controller = new AbortController();
      res.on("close", () => controller.abort());
      const engine = resolveCleveraiEngine(model || cleveraiPool.engine);
      bump(`cleverai/${engine}`);

      if (body.stream === true) {
        stats.streams++;
        res.writeHead(200, {
          "Content-Type": "text/event-stream",
          "Cache-Control": "no-store",
          "X-Accel-Buffering": "no",
        });
        try {
          for await (const chunk of cleveraiRelay.streamOpenAI({
            messages,
            model,
            tools,
            signal: controller.signal,
          })) {
            if (res.writableEnded) break;
            if (!res.write(chunk)) {
              await new Promise((resolve) => {
                res.once("drain", resolve);
                res.once("close", resolve);
              });
            }
          }
        } catch (cause) {
          if (!controller.signal.aborted) {
            log("error", `cleverai stream failed: ${cause?.message ?? cause}`);
            // 头已经发了，只能补一个错误帧再收尾。
            if (!res.writableEnded) {
              res.write(
                `data: ${JSON.stringify({ error: { message: String(cause?.message ?? "upstream error"), type: "upstream_error" } })}\n\n`,
              );
            }
          }
        }
        if (!res.writableEnded) res.end();
        return;
      }

      try {
        const completion = await cleveraiRelay.complete({ messages, model, tools, signal: controller.signal });
        const notice = completion["x_cleverai_notice"];
        return sendJson(res, 200, completion, notice ? { "X-Cleverai-Notice": String(notice) } : {});
      } catch (cause) {
        return fail(res, cause);
      }
    }

    if (path === "/cleverai/v1/usage" && req.method === "GET") {
      return sendJson(res, 200, { ok: true, pool: cleveraiPool.stats(), relay: cleveraiRelay.status() });
    }

    // Anthropic 面：Claude Code / 任何说 /v1/messages 的客户端。
    // 复用既有翻译层（messagesToChat / chatToMessage / streamAsAnthropic），
    // 中间只换成 cleverai 的号池与 tools 映射。
    if (path === "/cleverai/v1/messages" && req.method === "POST") {
      let body;
      try {
        body = await readJson(req);
      } catch (cause) {
        return fail(res, cause);
      }
      const chat = messagesToChat(body, { model: String(body.model ?? cleveraiPool.engine) });
      if (!Array.isArray(chat.messages) || !chat.messages.length) {
        return sendJson(res, 400, errorBody("messages 不能为空。", "invalid_request_error", "bad_request"));
      }
      const controller = new AbortController();
      res.on("close", () => controller.abort());
      const engine = resolveCleveraiEngine(chat.model || cleveraiPool.engine);
      const requestId = `msg_${randomBytes(12).toString("hex")}`;
      bump(`cleverai/${engine}`);

      if (body.stream === true) {
        stats.streams++;
        res.writeHead(200, {
          "Content-Type": "text/event-stream",
          "Cache-Control": "no-store",
          "X-Accel-Buffering": "no",
        });
        const openAiChunks = (async function* () {
          const encoder = new TextEncoder();
          for await (const chunk of cleveraiRelay.streamOpenAI({
            messages: chat.messages,
            model: chat.model,
            tools: chat.tools,
            signal: controller.signal,
          })) {
            yield encoder.encode(chunk);
          }
        })();
        try {
          for await (const event of streamAsAnthropic(openAiChunks, { model: engine, requestId })) {
            if (res.writableEnded) break;
            if (!res.write(event)) {
              await new Promise((resolve) => {
                res.once("drain", resolve);
                res.once("close", resolve);
              });
            }
          }
        } catch (cause) {
          log("error", `cleverai anthropic stream failed: ${cause?.message ?? cause}`);
          if (!res.writableEnded) res.destroy();
          return;
        }
        if (!res.writableEnded) res.end();
        return;
      }

      try {
        const completion = await cleveraiRelay.complete({
          messages: chat.messages,
          model: chat.model,
          tools: chat.tools,
          signal: controller.signal,
        });
        const notice = completion["x_cleverai_notice"];
        return sendJson(
          res,
          200,
          chatToMessage(completion, { model: engine, requestId }),
          notice ? { "X-Cleverai-Notice": String(notice) } : {},
        );
      } catch (cause) {
        return fail(res, cause);
      }
    }

    return sendJson(
      res,
      404,
      errorBody(`No cleverai route for ${req.method} ${path}.`, "invalid_request_error", "not_found"),
    );
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
      amd: {
        pool: amdPool.stats(),
        instances: amdInstanceSummary(amdPool),
        running: amdRegistrations.job?.status === "running",
        apiBase: AMD_LINE.apiBase,
        job: amdRegistrations.job
          ? {
              id: amdRegistrations.job.id,
              status: amdRegistrations.job.status,
              created: amdRegistrations.job.created ?? 0,
              failed: amdRegistrations.job.failed ?? 0,
            }
          : null,
      },
      lines: Object.fromEntries(
        OPENAI_LINES.map((line) => {
          const pool = linePools.get(line.id);
          const registrations = lineRegs.get(line.id);
          return [
            line.id,
            {
              name: line.name,
              apiBase: line.apiBase,
              schema: line.schema,
              relayPrefix: line.relayPrefix,
              anonymous: line.anonymous === true,
              pool: pool.stats(),
              running: registrations.job?.status === "running",
              job: registrations.job
                ? {
                    id: registrations.job.id,
                    status: registrations.job.status,
                    requested: registrations.job.requested,
                    created: registrations.job.created ?? 0,
                    failed: registrations.job.failed ?? 0,
                    keys: registrations.job.keys ?? 0,
                    startedAt: registrations.job.startedAt,
                    finishedAt: registrations.job.finishedAt ?? null,
                  }
                : null,
            },
          ];
        }),
      ),
      jevai: {
        pool: jevaiPool.stats(),
        running: jevai.job?.status === "running",
        apiBase: jevaiPool.apiBase,
        job: jevai.job
          ? {
              id: jevai.job.id,
              status: jevai.job.status,
              requested: jevai.job.requested,
              created: jevai.job.created ?? 0,
              failed: jevai.job.failed ?? 0,
              keys: jevai.job.keys ?? 0,
              startedAt: jevai.job.startedAt,
              finishedAt: jevai.job.finishedAt ?? null,
            }
          : null,
      },
      proxyscrape: {
        pool: proxyscrapePool.stats(),
        running: proxyscrape.job?.status === "running",
        share: proxyShareStatus(config.dataDir),
        job: proxyscrape.job
          ? {
              id: proxyscrape.job.id,
              status: proxyscrape.job.status,
              requested: proxyscrape.job.requested,
              created: proxyscrape.job.created ?? 0,
              proxies: proxyscrape.job.proxies ?? 0,
              failed: proxyscrape.job.failed ?? 0,
              startedAt: proxyscrape.job.startedAt,
              finishedAt: proxyscrape.job.finishedAt ?? null,
            }
          : null,
      },
      // 面板读 /api/admin/state 时就能看到这两条线，不用额外请求。
      gabai: {
        pool: gabaiPool.stats(),
        running: gabai.job?.status === "running",
        job: gabai.job
          ? {
              id: gabai.job.id,
              status: gabai.job.status,
              requested: gabai.job.requested,
              created: gabai.job.created ?? 0,
              failed: gabai.job.failed ?? 0,
              startedAt: gabai.job.startedAt,
              finishedAt: gabai.job.finishedAt ?? null,
            }
          : null,
      },
      cleverai: {
        pool: cleveraiPool.stats(),
        running: cleverai.job?.status === "running",
        engine: cleveraiPool.engine,
        appCheck: cleveraiRelay.status().appCheck,
        job: cleverai.job
          ? {
              id: cleverai.job.id,
              status: cleverai.job.status,
              requested: cleverai.job.requested,
              created: cleverai.job.created ?? 0,
              failed: cleverai.job.failed ?? 0,
              appcheck: cleverai.job.appcheck ?? null,
              startedAt: cleverai.job.startedAt,
              finishedAt: cleverai.job.finishedAt ?? null,
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


    /* ------------------------------------------------ cleverai 号池协议 */
    // 全部挂在 /api/admin/cleverai/* 下，与 allmodels / poe / providers 不重叠。
    // 协议见 docs/CLEVERAI-POOL.md（`dcg.pool/cleverai@1`）。
    if (path === "/api/admin/cleverai/status" && req.method === "GET") {
      return sendJson(res, 200, { ok: true, ...cleverai.status(), relay: cleveraiRelay.status() });
    }

    if (path === "/api/admin/cleverai/register" && req.method === "POST") {
      let body = {};
      try {
        body = await readJson(req);
      } catch (cause) {
        return fail(res, cause);
      }
      const shared = withSharedProxies({ dataDir: config.dataDir, line: "cleverai", proxies: body.proxies });
      try {
        return sendJson(res, 202, {
          ok: true,
          proxiesSource: shared.source,
          ...cleverai.start({ ...body, proxies: shared.proxies }),
        });
      } catch (cause) {
        return fail(res, cause);
      }
    }

    if (path === "/api/admin/cleverai/register/stop" && req.method === "POST") {
      return sendJson(res, 200, { ok: true, ...cleverai.stop() });
    }

    if (path === "/api/admin/cleverai/pool" && req.method === "GET") {
      const limit = Math.min(500, Math.max(1, Number(params.get("limit") ?? 200)));
      return sendJson(res, 200, {
        ok: true,
        schema: cleveraiPool.toJSON().schema,
        baseUrl: cleveraiPool.baseUrl,
        apiBase: cleveraiPool.apiBase,
        engine: cleveraiPool.engine,
        updatedAt: cleveraiPool.toJSON().updatedAt,
        stats: cleveraiPool.stats(),
        accounts: cleveraiPool.accounts.slice(-limit).map(maskCleveraiAccount),
      });
    }

    if (path === "/api/admin/cleverai/pool/import" && req.method === "POST") {
      let body;
      try {
        body = await readJson(req);
      } catch (cause) {
        return fail(res, cause);
      }
      const email = String(body.email ?? "").trim();
      if (!email || !(body.refreshToken ?? body.refresh_token)) {
        return sendJson(res, 400, errorBody("需要 email 与 refreshToken。", "invalid_request_error", "bad_request"));
      }
      const { account, created, gatewayKey } = importCleveraiAccount({
        pool: cleveraiPool,
        store,
        record: body,
        mintKey: body.mintKey !== false,
      });
      log("info", `cleverai 入池 ${email}（号池 ${cleveraiPool.size}）`);
      return sendJson(res, created ? 201 : 200, {
        ok: true,
        created,
        account: maskCleveraiAccount(account),
        gatewayKey: gatewayKey ? { id: gatewayKey.id, label: gatewayKey.label, prefix: gatewayKey.prefix, plaintext: gatewayKey.plaintext } : null,
        pool: cleveraiPool.stats(),
      });
    }

    if (path === "/api/admin/cleverai/pool/export" && (req.method === "POST" || req.method === "GET")) {
      const format = ["jsonl", "csv", "pool"].includes(String(params.get("format") ?? ""))
        ? String(params.get("format"))
        : "jsonl";
      let result;
      try {
        result = cleverai.export({ format });
      } catch (cause) {
        return fail(res, cause);
      }
      if (params.get("download") === "1") {
        const body = readFileSync(result.file);
        const name = result.file.split(/[\\/]/).pop() ?? "cleverai-pool.jsonl";
        res.writeHead(200, {
          "Content-Type": format === "csv" ? "text/csv; charset=utf-8" : "application/json",
          "Content-Length": String(body.length),
          "Content-Disposition": `attachment; filename="${name}"`,
          "Cache-Control": "no-store",
        });
        return res.end(body);
      }
      return sendJson(res, 200, { ok: true, ...result });
    }

    const cleveraiHealth = /^\/api\/admin\/cleverai\/pool\/([^/]+)\/health$/.exec(path);
    if (cleveraiHealth && req.method === "POST") {
      let body = {};
      try {
        body = await readJson(req);
      } catch {
        body = {};
      }
      const updated = cleveraiPool.recordResult(cleveraiHealth[1], {
        ok: body.ok !== false,
        error: body.error ?? null,
        cooldownMs: Number(body.cooldownMs ?? 0),
      });
      if (!updated) return sendJson(res, 404, errorBody("号池里没有这个账号。", "invalid_request_error", "not_found"));
      return sendJson(res, 200, { ok: true, account: maskCleveraiAccount(updated), pool: cleveraiPool.stats() });
    }

    const cleveraiDelete = /^\/api\/admin\/cleverai\/pool\/([^/]+)$/.exec(path);
    if (cleveraiDelete && req.method === "DELETE") {
      if (!cleveraiPool.remove(cleveraiDelete[1])) {
        return sendJson(res, 404, errorBody("号池里没有这个账号。", "invalid_request_error", "not_found"));
      }
      return sendJson(res, 200, { ok: true, pool: cleveraiPool.stats() });
    }

    if (path === "/api/admin/cleverai/keys" && req.method === "GET") {
      return sendJson(res, 200, {
        ok: true,
        keys: cleveraiPool.accounts.map((account) => ({
          email: account.email,
          uid: account.uid,
          engine: account.engine,
          status: account.status,
          wallet: account.wallet ?? {},
          gatewayKey: account.gatewayKey
            ? { id: account.gatewayKey.id, label: account.gatewayKey.label, prefix: account.gatewayKey.prefix }
            : null,
        })),
      });
    }

    if (path === "/api/admin/cleverai/appcheck" && req.method === "POST") {
      try {
        const token = await cleveraiRelay.vault.get({ force: true });
        return sendJson(res, 200, { ok: true, token, relay: cleveraiRelay.status() });
      } catch (cause) {
        return fail(res, cause);
      }
    }

    /* ------------------------------------------------------ gabai 号池协议 */
    // 全部挂在 /api/admin/gabai/* 与 /gab/v1/* 下，与其它线不重叠。
    if (path === "/api/admin/gabai/status" && req.method === "GET") {
      return sendJson(res, 200, { ok: true, ...gabai.status() });
    }

    if (path === "/api/admin/gabai/register" && req.method === "POST") {
      let body = {};
      try {
        body = await readJson(req);
      } catch (cause) {
        return fail(res, cause);
      }
      const shared = withSharedProxies({ dataDir: config.dataDir, line: "gabai", proxies: body.proxies });
      try {
        return sendJson(res, 202, {
          ok: true,
          proxiesSource: shared.source,
          ...gabai.start({ ...body, proxies: shared.proxies }),
        });
      } catch (cause) {
        return fail(res, cause);
      }
    }

    if (path === "/api/admin/gabai/register/stop" && req.method === "POST") {
      return sendJson(res, 200, { ok: true, ...gabai.stop() });
    }

    if (path === "/api/admin/gabai/pool" && req.method === "GET") {
      const limit = Math.min(500, Math.max(1, Number(params.get("limit") ?? 200)));
      return sendJson(res, 200, {
        ok: true,
        schema: gabaiPool.toJSON().schema,
        baseUrl: gabaiPool.baseUrl,
        apiBase: gabaiPool.apiBase,
        stats: gabaiPool.stats(),
        accounts: gabaiPool.accounts.slice(-limit).map(maskGabaiAccount),
      });
    }

    if (path === "/api/admin/gabai/pool/import" && req.method === "POST") {
      let body;
      try {
        body = await readJson(req);
      } catch (cause) {
        return fail(res, cause);
      }
      const email = String(body.email ?? "").trim();
      const apiKey = String(body.apiKey ?? body.api_key ?? "").trim();
      if (!email || !apiKey) {
        return sendJson(res, 400, errorBody("需要 email 与 apiKey。", "invalid_request_error", "bad_request"));
      }
      const { account, created, gatewayKey } = importGabaiAccount({
        pool: gabaiPool,
        store,
        record: body,
        mintKey: body.mintKey !== false,
      });
      log("info", `gabai 入池 ${email}（号池 ${gabaiPool.size}）`);
      return sendJson(res, created ? 201 : 200, {
        ok: true,
        created,
        account: maskGabaiAccount(account),
        gatewayKey: gatewayKey
          ? { id: gatewayKey.id, label: gatewayKey.label, prefix: gatewayKey.prefix, plaintext: gatewayKey.plaintext }
          : null,
        pool: gabaiPool.stats(),
      });
    }

    if (path === "/api/admin/gabai/pool/export" && (req.method === "POST" || req.method === "GET")) {
      const format = ["jsonl", "csv", "pool"].includes(String(params.get("format") ?? ""))
        ? String(params.get("format"))
        : "jsonl";
      let result;
      try {
        result = gabai.export({ format });
      } catch (cause) {
        return fail(res, cause);
      }
      if (params.get("download") === "1") {
        const payload = readFileSync(result.file);
        const name = result.file.split(/[\\/]/).pop() ?? "gabai-pool.jsonl";
        res.writeHead(200, {
          "Content-Type": format === "csv" ? "text/csv; charset=utf-8" : "application/json",
          "Content-Length": String(payload.length),
          "Content-Disposition": `attachment; filename="${name}"`,
          "Cache-Control": "no-store",
        });
        return res.end(payload);
      }
      return sendJson(res, 200, { ok: true, ...result });
    }

    const gabaiMint = /^\/api\/admin\/gabai\/pool\/([^/]+)\/mint$/.exec(path);
    if (gabaiMint && req.method === "POST") {
      const account = gabaiPool.getAccount(gabaiMint[1]);
      if (!account) return sendJson(res, 404, errorBody("号池里没有这个账号。", "invalid_request_error", "not_found"));
      try {
        const result = await gabaiProxy.mintApiKey(account, { name: `dcg-${new Date().toISOString().slice(0, 10)}` });
        return sendJson(res, 200, { ok: true, ...result, account: maskGabaiAccount(gabaiPool.getAccount(account.id)) });
      } catch (cause) {
        return fail(res, cause);
      }
    }

    const gabaiDelete = /^\/api\/admin\/gabai\/pool\/([^/]+)$/.exec(path);
    if (gabaiDelete && req.method === "DELETE") {
      if (!gabaiPool.remove(gabaiDelete[1])) {
        return sendJson(res, 404, errorBody("号池里没有这个账号。", "invalid_request_error", "not_found"));
      }
      return sendJson(res, 200, { ok: true, pool: gabaiPool.stats() });
    }

    /* ------------------------------------------------ proxyscrape 号池 + 出口分发 */
    // 这条线的产物是代理：账号池 + 代理池，外加"把代理分发给别的注册机"。
    if (path === "/api/admin/proxyscrape/status" && req.method === "GET") {
      return sendJson(res, 200, { ok: true, ...proxyscrape.status(), share: proxyShareStatus(config.dataDir) });
    }

    if (path === "/api/admin/proxyscrape/register" && req.method === "POST") {
      let body = {};
      try {
        body = await readJson(req);
      } catch (cause) {
        return fail(res, cause);
      }
      const shared = withSharedProxies({ dataDir: config.dataDir, line: "proxyscrape", proxies: body.proxies });
      try {
        return sendJson(res, 202, {
          ok: true,
          proxiesSource: shared.source,
          ...proxyscrape.start({ ...body, proxies: shared.proxies }),
        });
      } catch (cause) {
        return fail(res, cause);
      }
    }

    if (path === "/api/admin/proxyscrape/register/stop" && req.method === "POST") {
      return sendJson(res, 200, { ok: true, ...proxyscrape.stop() });
    }

    if (path === "/api/admin/proxyscrape/pool" && req.method === "GET") {
      const limit = Math.min(500, Math.max(1, Number(params.get("limit") ?? 200)));
      return sendJson(res, 200, {
        ok: true,
        schema: proxyscrapePool.toJSON().schema,
        baseUrl: proxyscrapePool.baseUrl,
        stats: proxyscrapePool.stats(),
        accounts: proxyscrapePool.accounts.slice(-limit).map(maskProxyscrapeAccount),
        proxies: proxyscrapePool.proxies.slice(-limit).map(maskProxyscrapeProxy),
      });
    }

    if (path === "/api/admin/proxyscrape/pool/import" && req.method === "POST") {
      let body;
      try {
        body = await readJson(req);
      } catch (cause) {
        return fail(res, cause);
      }
      const email = String(body.email ?? "").trim();
      if (!email) {
        return sendJson(res, 400, errorBody("需要 email。", "invalid_request_error", "bad_request"));
      }
      const result = proxyscrapePool.add(body);
      const share = applyProxies({ pool: proxyscrapePool, dataDir: config.dataDir });
      log("info", `proxyscrape 入池 ${email}（+${result.proxies} 条代理，池内 ${proxyscrapePool.stats().proxies}）`);
      return sendJson(res, result.created ? 201 : 200, {
        ok: true,
        created: result.created,
        proxies: result.proxies,
        account: maskProxyscrapeAccount(result.account),
        share,
        pool: proxyscrapePool.stats(),
      });
    }

    if (path === "/api/admin/proxyscrape/pool/export" && (req.method === "POST" || req.method === "GET")) {
      const format = ["proxies", "jsonl", "csv", "pool"].includes(String(params.get("format") ?? ""))
        ? String(params.get("format"))
        : "proxies";
      let result;
      try {
        result = proxyscrape.export({ format });
      } catch (cause) {
        return fail(res, cause);
      }
      if (params.get("download") === "1") {
        const payload = readFileSync(result.file);
        const name = result.file.split(/[\\/]/).pop() ?? "proxyscrape.txt";
        res.writeHead(200, {
          "Content-Type": format === "csv" ? "text/csv; charset=utf-8" : "text/plain; charset=utf-8",
          "Content-Length": String(payload.length),
          "Content-Disposition": `attachment; filename="${name}"`,
          "Cache-Control": "no-store",
        });
        return res.end(payload);
      }
      return sendJson(res, 200, { ok: true, ...result });
    }

    if (path === "/api/admin/proxyscrape/proxies" && req.method === "GET") {
      return sendJson(res, 200, {
        ok: true,
        stats: proxyscrapePool.stats(),
        proxies: proxyscrapePool.proxies.map(maskProxyscrapeProxy),
      });
    }

    const proxyscrapeProxyDelete = /^\/api\/admin\/proxyscrape\/proxies\/(.+)$/.exec(path);
    if (proxyscrapeProxyDelete && req.method === "DELETE") {
      const endpoint = decodeURIComponent(proxyscrapeProxyDelete[1]);
      if (!proxyscrapePool.removeProxy(endpoint)) {
        return sendJson(res, 404, errorBody("代理池里没有这条。", "invalid_request_error", "not_found"));
      }
      applyProxies({ pool: proxyscrapePool, dataDir: config.dataDir });
      return sendJson(res, 200, { ok: true, pool: proxyscrapePool.stats() });
    }

    const proxyscrapeAccountDelete = /^\/api\/admin\/proxyscrape\/pool\/([^/]+)$/.exec(path);
    if (proxyscrapeAccountDelete && req.method === "DELETE") {
      if (!proxyscrapePool.removeAccount(proxyscrapeAccountDelete[1])) {
        return sendJson(res, 404, errorBody("号池里没有这个账号。", "invalid_request_error", "not_found"));
      }
      return sendJson(res, 200, { ok: true, pool: proxyscrapePool.stats() });
    }

    // 出口分发：把代理写进 data/proxies/<line>.txt，其它注册机自动吃
    if (path === "/api/admin/proxies" && req.method === "GET") {
      return sendJson(res, 200, {
        ok: true,
        pool: proxyscrapePool.stats(),
        share: proxyShareStatus(config.dataDir),
      });
    }

    if (path === "/api/admin/proxies/apply" && req.method === "POST") {
      let body = {};
      try {
        body = await readJson(req);
      } catch {
        body = {};
      }
      const lines = Array.isArray(body.lines) && body.lines.length ? body.lines.map(String) : undefined;
      const share = applyProxies({
        pool: proxyscrapePool,
        dataDir: config.dataDir,
        ...(lines ? { lines } : {}),
        limitPerLine: Number(body.limitPerLine ?? 0) || 0,
      });
      log("info", `proxies: 已分发 ${share.total} 条代理到 ${share.lines.length} 条线`);
      return sendJson(res, 200, { ok: true, ...share });
    }

    /* ------------------------------------------- 代理导出 / 订阅（给别的设备） */

    if (path === "/api/admin/proxies/formats" && req.method === "GET") {
      return sendJson(res, 200, {
        ok: true,
        defaultFormat: DEFAULT_PROXY_FORMAT,
        formats: Object.entries(PROXY_FORMATS).map(([id, meta]) => ({ id, ...meta })),
      });
    }

    if ((path === "/api/admin/proxies/export" || path === "/api/admin/proxies/download") && (req.method === "GET" || req.method === "POST")) {
      const format = PROXY_FORMATS[String(params.get("format") ?? "")]
        ? String(params.get("format"))
        : DEFAULT_PROXY_FORMAT;
      const aliveOnly = ["1", "true", "yes"].includes(String(params.get("alive") ?? "").toLowerCase());
      const limit = Math.max(0, Number(params.get("limit") ?? 0) || 0);
      let rows = proxyscrapePool.usable().map((proxy) => ({
        scheme: proxy.scheme,
        host: proxy.host,
        port: proxy.port,
        username: proxy.username,
        password: proxy.password,
        status: proxy.status,
        cooldownUntil: proxy.cooldownUntil ?? 0,
      }));
      if (aliveOnly) {
        const { alive } = await probeProxies(rows, { timeoutMs: 4000, concurrency: 8 });
        rows = alive.map((item) => item.proxy);
      }
      if (limit > 0) rows = rows.slice(0, limit);
      const body = renderProxies(rows, format, { name: String(params.get("name") ?? "proxyscrape") });
      const name = proxyExportFileName(format);
      if (params.get("download") === "1") {
        const payload = Buffer.from(body, "utf8");
        res.writeHead(200, {
          "Content-Type": PROXY_FORMATS[format].contentType,
          "Content-Length": String(payload.length),
          "Content-Disposition": `attachment; filename="${name}"`,
          "Cache-Control": "no-store",
        });
        return res.end(payload);
      }
      return sendJson(res, 200, {
        ok: true,
        format,
        rows: rows.length,
        aliveOnly,
        body,
        formats: Object.keys(PROXY_FORMATS),
      });
    }

    if (path === "/api/admin/proxies/check" && req.method === "POST") {
      let body = {};
      try {
        body = await readJson(req);
      } catch {
        body = {};
      }
      const timeoutMs = Math.min(15000, Math.max(500, Number(body.timeoutMs ?? 4000) || 4000));
      const concurrency = Math.min(32, Math.max(1, Number(body.concurrency ?? 8) || 8));
      const rows = proxyscrapePool.usable().map((proxy) => ({
        endpoint: proxy.endpoint,
        scheme: proxy.scheme,
        host: proxy.host,
        port: proxy.port,
        username: proxy.username,
        password: proxy.password,
      }));
      const { alive, dead } = await probeProxies(rows, { timeoutMs, concurrency });
      for (const item of alive) proxyscrapePool.recordProxy(item.proxy.endpoint, { ok: true });
      for (const item of dead) {
        proxyscrapePool.recordProxy(item.proxy.endpoint, { ok: false, error: item.error ?? "unreachable" });
      }
      log("info", `proxies: 探测 ${rows.length} 条，活 ${alive.length}，死 ${dead.length}`);
      return sendJson(res, 200, {
        ok: true,
        total: rows.length,
        alive: alive.length,
        dead: dead.length,
        pool: proxyscrapePool.stats(),
        results: [...alive, ...dead].map((item) => ({
          endpoint: `${item.proxy.scheme ?? "http"}://${item.proxy.host}:${item.proxy.port}`,
          ok: item.ok,
          ms: item.ms,
          error: item.error ?? null,
        })),
      });
    }

    if (path === "/api/admin/proxies/shares" && req.method === "GET") {
      return sendJson(res, 200, {
        ok: true,
        publicBase,
        shares: proxyShares.list().map((share) => ({
          ...maskProxyShare(share),
          url: share.revoked ? null : shareUrlFor(share.token),
        })),
      });
    }

    if (path === "/api/admin/proxies/share" && req.method === "POST") {
      let body = {};
      try {
        body = await readJson(req);
      } catch {
        body = {};
      }
      const minted = proxyShares.mint({
        name: String(body.name ?? ""),
        format: String(body.format ?? DEFAULT_PROXY_FORMAT),
        note: String(body.note ?? ""),
      });
      return sendJson(res, minted.reused ? 200 : 201, {
        ok: true,
        reused: minted.reused,
        share: maskProxyShare(minted),
        token: minted.token,
        url: shareUrlFor(minted.token),
        format: minted.format,
      });
    }

    const shareDelete = /^\/api\/admin\/proxies\/shares\/([^/]+)$/.exec(path);
    if (shareDelete && req.method === "DELETE") {
      const target = decodeURIComponent(shareDelete[1]);
      const hard = ["1", "true"].includes(String(params.get("purge") ?? "").toLowerCase());
      // 面板只拿得到打码令牌，所以也允许按 name 撤销
      const share = proxyShares.find(target) ?? proxyShares.list().find((row) => row.name === target) ?? null;
      if (!share) {
        return sendJson(res, 404, errorBody("没有这条订阅。", "invalid_request_error", "not_found"));
      }
      const done = hard ? proxyShares.remove(share.token) : proxyShares.revoke(share.token);
      if (!done) {
        return sendJson(res, 404, errorBody("没有这条订阅。", "invalid_request_error", "not_found"));
      }
      return sendJson(res, 200, {
        ok: true,
        purged: hard,
        name: share.name,
        shares: proxyShares.list().map(maskProxyShare),
      });
    }

    /* --------------------------------------------------------- jevai 号池 */
    if (path === "/api/admin/jevai/status" && req.method === "GET") {
      return sendJson(res, 200, { ok: true, ...jevai.status(), relay: jevaiRelay.status() });
    }

    if (path === "/api/admin/jevai/register" && req.method === "POST") {
      let body = {};
      try {
        body = await readJson(req);
      } catch (cause) {
        return fail(res, cause);
      }
      try {
        return sendJson(res, 202, { ok: true, ...jevai.start(body) });
      } catch (cause) {
        return fail(res, cause);
      }
    }

    // 每日签到：jev 的免费额度主要靠这个积累（号越多，日额度越高）
    if (path === "/api/admin/jevai/checkin" && req.method === "POST") {
      let body = {};
      try {
        body = await readJson(req);
      } catch {
        body = {};
      }
      try {
        return sendJson(res, 202, { ok: true, ...jevai.checkin(body) });
      } catch (cause) {
        return fail(res, cause);
      }
    }

    if (path === "/api/admin/jevai/register/stop" && req.method === "POST") {
      return sendJson(res, 200, { ok: true, ...jevai.stop() });
    }

    if (path === "/api/admin/jevai/pool" && req.method === "GET") {
      const limit = Math.min(500, Math.max(1, Number(params.get("limit") ?? 200)));
      return sendJson(res, 200, {
        ok: true,
        schema: jevaiPool.toJSON().schema,
        baseUrl: jevaiPool.baseUrl,
        apiBase: jevaiPool.apiBase,
        stats: jevaiPool.stats(),
        accounts: jevaiPool.accounts.slice(-limit).map(maskJevaiAccount),
      });
    }

    if (path === "/api/admin/jevai/pool/import" && req.method === "POST") {
      let body;
      try {
        body = await readJson(req);
      } catch (cause) {
        return fail(res, cause);
      }
      const email = String(body.email ?? "").trim();
      const apiKey = String(body.apiKey ?? body.api_key ?? "").trim();
      if (!email || !apiKey) {
        return sendJson(res, 400, errorBody("需要 email 与 apiKey。", "invalid_request_error", "bad_request"));
      }
      const { account, created, gatewayKey } = importJevaiAccount({
        pool: jevaiPool,
        store,
        record: body,
        mintKey: body.mintKey !== false,
      });
      const applied = jevai.apply();
      log("info", `jevai 入池 ${email}（号池 ${jevaiPool.size}，Key ${jevaiPool.keys().length}）`);
      return sendJson(res, created ? 201 : 200, {
        ok: true,
        created,
        account: maskJevaiAccount(account),
        gatewayKey: gatewayKey
          ? { id: gatewayKey.id, label: gatewayKey.label, prefix: gatewayKey.prefix, plaintext: gatewayKey.plaintext }
          : null,
        apply: applied,
        pool: jevaiPool.stats(),
      });
    }

    if (path === "/api/admin/jevai/pool/export" && (req.method === "POST" || req.method === "GET")) {
      const format = ["keys", "jsonl", "csv", "pool"].includes(String(params.get("format") ?? ""))
        ? String(params.get("format"))
        : "keys";
      let result;
      try {
        result = jevai.export({ format });
      } catch (cause) {
        return fail(res, cause);
      }
      if (params.get("download") === "1") {
        const payload = readFileSync(result.file);
        const name = result.file.split(/[\\/]/).pop() ?? "jevai-keys.txt";
        res.writeHead(200, {
          "Content-Type": format === "csv" ? "text/csv; charset=utf-8" : "text/plain; charset=utf-8",
          "Content-Length": String(payload.length),
          "Content-Disposition": `attachment; filename="${name}"`,
          "Cache-Control": "no-store",
        });
        return res.end(payload);
      }
      return sendJson(res, 200, { ok: true, ...result });
    }

    if (path === "/api/admin/jevai/keys" && req.method === "GET") {
      return sendJson(res, 200, {
        ok: true,
        apiBase: jevaiPool.apiBase,
        keys: jevaiPool.accounts.map((account) => ({
          email: account.email,
          apiKeyPreview: account.apiKey ? `${account.apiKey.slice(0, 12)}…` : "",
          emailVerified: account.emailVerified,
          gatewayKey: account.gatewayKey
            ? { id: account.gatewayKey.id, label: account.gatewayKey.label, prefix: account.gatewayKey.prefix }
            : null,
        })),
      });
    }

    // 自动接入其它工具：写 data/keys/jevai.{txt,json}，并回接入信息
    if (path === "/api/admin/jevai/apply" && req.method === "POST") {
      try {
        const applied = jevai.apply();
        return sendJson(res, 200, {
          ok: true,
          ...applied,
          connect: jevaiRelay.connection(),
          pool: jevaiPool.stats(),
        });
      } catch (cause) {
        return fail(res, cause);
      }
    }

    if (path === "/api/admin/jevai/connect" && req.method === "GET") {
      const base = String(config.publicBase ?? "").replace(/\/+$/, "");
      const usable = store.listKeys().filter((key) => !key.revoked);
      return sendJson(res, 200, {
        ok: true,
        ...jevaiRelay.connection(),
        gatewayBase: `${base}/jevai/v1`,
        upstreamBase: jevaiPool.apiBase,
        gatewayKeyPrefix: usable[0]?.prefix ?? "",
        gatewayKeys: usable.length,
      });
    }

    const jevaiDelete = /^\/api\/admin\/jevai\/pool\/([^/]+)$/.exec(path);
    if (jevaiDelete && req.method === "DELETE") {
      if (!jevaiPool.remove(jevaiDelete[1])) {
        return sendJson(res, 404, errorBody("号池里没有这个账号。", "invalid_request_error", "not_found"));
      }
      return sendJson(res, 200, { ok: true, pool: jevaiPool.stats() });
    }

    /* ------------------------------------------- 六条 OpenAI 兼容线的管理面 */
    // 一条代码路径服务六条线：/api/admin/<line>/{status,register,register/stop,pool,pool/import,pool/export,keys,apply,connect}
    const lineAdminPrefix = Object.keys(LINE_ROUTE_LITERALS).find((id) => path.startsWith(`/api/admin/${id}/`));
    if (lineAdminPrefix) {
      const line = lineById(lineAdminPrefix);
      const pool = linePools.get(lineAdminPrefix);
      const registrations = lineRegs.get(lineAdminPrefix);
      const relay = lineRelays.get(lineAdminPrefix);
      const rest = path.slice(`/api/admin/${lineAdminPrefix}/`.length);

      if (rest === "status" && req.method === "GET") {
        return sendJson(res, 200, { ok: true, ...registrations.status(), relay: relay.status() });
      }
      if (rest === "register" && req.method === "POST") {
        let body = {};
        try {
          body = await readJson(req);
        } catch (cause) {
          return fail(res, cause);
        }
        try {
          return sendJson(res, 202, { ok: true, ...registrations.start(body) });
        } catch (cause) {
          return fail(res, cause);
        }
      }
      if (rest === "register/stop" && req.method === "POST") {
        return sendJson(res, 200, { ok: true, ...registrations.stop() });
      }
      if (rest === "pool" && req.method === "GET") {
        const limit = Math.min(500, Math.max(1, Number(params.get("limit") ?? 200)));
        return sendJson(res, 200, {
          ok: true,
          line: line.id,
          schema: line.schema,
          apiBase: line.apiBase,
          anonymous: line.anonymous === true,
          freeSuffix: line.freeSuffix || null,
          stats: pool.stats(),
          accounts: pool.accounts.slice(-limit).map(maskLineAccount),
        });
      }
      if (rest === "pool/import" && req.method === "POST") {
        let body;
        try {
          body = await readJson(req);
        } catch (cause) {
          return fail(res, cause);
        }
        const email = String(body.email ?? "").trim();
        const apiKey = String(body.apiKey ?? body.api_key ?? "").trim();
        if (!email || (!apiKey && line.anonymous !== true)) {
          return sendJson(res, 400, errorBody("需要 email 与 apiKey。", "invalid_request_error", "bad_request"));
        }
        const { account, created, gatewayKey } = importLineAccount({
          line,
          pool,
          store,
          record: body,
          mintKey: body.mintKey !== false,
        });
        const applied = registrations.apply();
        return sendJson(res, created ? 201 : 200, {
          ok: true,
          line: line.id,
          created,
          account: maskLineAccount(account),
          gatewayKey: gatewayKey
            ? { id: gatewayKey.id, label: gatewayKey.label, prefix: gatewayKey.prefix, plaintext: gatewayKey.plaintext }
            : null,
          apply: applied,
          pool: pool.stats(),
        });
      }
      if (rest === "pool/export" && (req.method === "POST" || req.method === "GET")) {
        const format = ["keys", "jsonl", "csv", "pool"].includes(String(params.get("format") ?? ""))
          ? String(params.get("format"))
          : "keys";
        let result;
        try {
          result = registrations.export({ format });
        } catch (cause) {
          return fail(res, cause);
        }
        if (params.get("download") === "1") {
          const payload = readFileSync(result.file);
          const name = result.file.split(/[\\/]/).pop() ?? `${line.id}-keys.txt`;
          res.writeHead(200, {
            "Content-Type": format === "csv" ? "text/csv; charset=utf-8" : "text/plain; charset=utf-8",
            "Content-Length": String(payload.length),
            "Content-Disposition": `attachment; filename="${name}"`,
            "Cache-Control": "no-store",
          });
          return res.end(payload);
        }
        return sendJson(res, 200, { ok: true, line: line.id, ...result });
      }
      if (rest === "keys" && req.method === "GET") {
        return sendJson(res, 200, {
          ok: true,
          line: line.id,
          apiBase: line.apiBase,
          keyPrefix: line.keyPrefix,
          keys: pool.accounts.map((account) => ({
            email: account.email,
            apiKeyPreview: account.apiKey ? `${account.apiKey.slice(0, 10)}…` : "",
            emailVerified: account.emailVerified,
            gatewayKey: account.gatewayKey
              ? { id: account.gatewayKey.id, label: account.gatewayKey.label, prefix: account.gatewayKey.prefix }
              : null,
          })),
        });
      }
      if (rest === "apply" && req.method === "POST") {
        try {
          const applied = registrations.apply();
          return sendJson(res, 200, { ok: true, line: line.id, ...applied, connect: relay.status(), pool: pool.stats() });
        } catch (cause) {
          return fail(res, cause);
        }
      }
      if (rest === "connect" && req.method === "GET") {
        const base = String(config.publicBase ?? "").replace(/\/+$/, "");
        const usable = store.listKeys().filter((key) => !key.revoked);
        return sendJson(res, 200, {
          ok: true,
          line: line.id,
          gatewayBase: `${base}${line.relayPrefix}`,
          upstreamBase: line.apiBase,
          keyPrefix: line.keyPrefix,
          anonymous: line.anonymous === true,
          gatewayKeyPrefix: usable[0]?.prefix ?? "",
          keys: pool.keys().length,
        });
      }
      const accountDelete = /^pool\/([^/]+)$/.exec(rest);
      if (accountDelete && req.method === "DELETE") {
        if (!pool.remove(accountDelete[1])) {
          return sendJson(res, 404, errorBody("号池里没有这个账号。", "invalid_request_error", "not_found"));
        }
        return sendJson(res, 200, { ok: true, line: line.id, pool: pool.stats() });
      }
      return sendJson(res, 404, errorBody(`No ${line.id} admin route for ${rest}.`, "invalid_request_error", "not_found"));
    }

    // 六条线的总览（面板侧边栏用）
    if (path === "/api/admin/lines" && req.method === "GET") {
      return sendJson(res, 200, {
        ok: true,
        lines: OPENAI_LINES.map((line) => {
          const pool = linePools.get(line.id);
          const registrations = lineRegs.get(line.id);
          return {
            id: line.id,
            name: line.name,
            schema: line.schema,
            apiBase: line.apiBase,
            relayPrefix: line.relayPrefix,
            keyPrefix: line.keyPrefix,
            freeSuffix: line.freeSuffix || null,
            anonymous: line.anonymous === true,
            anthropic: line.anthropic === true,
            models: line.models,
            stats: pool.stats(),
            running: registrations.job?.status === "running",
            job: registrations.job
              ? {
                  id: registrations.job.id,
                  status: registrations.job.status,
                  requested: registrations.job.requested,
                  created: registrations.job.created ?? 0,
                  failed: registrations.job.failed ?? 0,
                  keys: registrations.job.keys ?? 0,
                  startedAt: registrations.job.startedAt,
                  finishedAt: registrations.job.finishedAt ?? null,
                }
              : null,
          };
        }),
      });
    }

    /* --------------------------------------------------- Turnstile 求解器 */
    // 本地 captcha-solver（tools/captcha-solver）：注册页有人机验证的线都靠它
    if (path === "/api/admin/captcha/status" && req.method === "GET") {
      const status = await solverStatus({ solverUrl: String(params.get("url") ?? "") });
      return sendJson(res, 200, { ok: status.ok, solverUrl: status.url, status });
    }

    if (path === "/api/admin/captcha/solve" && req.method === "POST") {
      let body;
      try {
        body = await readJson(req);
      } catch (cause) {
        return fail(res, cause);
      }
      try {
        const solved = await solveTurnstile({
          url: String(body.url ?? ""),
          sitekey: String(body.sitekey ?? ""),
          action: String(body.action ?? ""),
          cdata: String(body.cdata ?? ""),
          solverUrl: String(body.solverUrl ?? ""),
          timeoutMs: Math.min(600_000, Math.max(5_000, Number(body.timeoutMs ?? 180_000))),
        });
        return sendJson(res, 200, { ok: true, ...solved });
      } catch (cause) {
        return fail(res, cause);
      }
    }

    /* --------------------------------------------- AMD 开发者云（GPU 实例） */
    if (path === "/api/admin/amd/status" && req.method === "GET") {
      return sendJson(res, 200, {
        ok: true,
        ...amdRegistrations.status(),
        instances: amdInstanceSummary(amdPool),
        apiBase: AMD_LINE.apiBase,
      });
    }

    if (path === "/api/admin/amd/register" && req.method === "POST") {
      let body = {};
      try {
        body = await readJson(req);
      } catch (cause) {
        return fail(res, cause);
      }
      try {
        return sendJson(res, 202, { ok: true, ...amdRegistrations.start(body) });
      } catch (cause) {
        return fail(res, cause);
      }
    }

    if (path === "/api/admin/amd/register/stop" && req.method === "POST") {
      return sendJson(res, 200, { ok: true, ...amdRegistrations.stop() });
    }

    if (path === "/api/admin/amd/pool" && req.method === "GET") {
      const limit = Math.min(500, Math.max(1, Number(params.get("limit") ?? 200)));
      return sendJson(res, 200, {
        ok: true,
        line: "amd",
        schema: AMD_LINE.schema,
        apiBase: AMD_LINE.apiBase,
        stats: amdPool.stats(),
        instances: amdInstanceSummary(amdPool),
        accounts: amdPool.accounts.slice(-limit).map(maskLineAccount),
      });
    }

    if (path === "/api/admin/amd/pool/import" && req.method === "POST") {
      let body;
      try {
        body = await readJson(req);
      } catch (cause) {
        return fail(res, cause);
      }
      const email = String(body.email ?? "").trim();
      const cookie = String(body.apiKey ?? body.cookie ?? "").trim();
      if (!email || !cookie) {
        return sendJson(res, 400, errorBody("需要 email 与 cookie（apiKey 字段）。", "invalid_request_error", "bad_request"));
      }
      const { account, created, gatewayKey } = importLineAccount({
        line: AMD_LINE,
        pool: amdPool,
        store,
        record: { ...body, apiKey: cookie },
        mintKey: body.mintKey !== false,
      });
      const applied = amdRegistrations.apply();
      return sendJson(res, created ? 201 : 200, {
        ok: true,
        line: "amd",
        created,
        account: maskLineAccount(account),
        gatewayKey: gatewayKey
          ? { id: gatewayKey.id, label: gatewayKey.label, prefix: gatewayKey.prefix, plaintext: gatewayKey.plaintext }
          : null,
        apply: applied,
        instances: amdInstanceSummary(amdPool),
      });
    }

    if (path === "/api/admin/amd/pool/export" && (req.method === "POST" || req.method === "GET")) {
      const format = ["keys", "jsonl", "csv", "pool"].includes(String(params.get("format") ?? ""))
        ? String(params.get("format"))
        : "keys";
      let result;
      try {
        result = amdRegistrations.export({ format });
      } catch (cause) {
        return fail(res, cause);
      }
      if (params.get("download") === "1") {
        const payload = readFileSync(result.file);
        const name = result.file.split(/[\\/]/).pop() ?? "amd-cookies.txt";
        res.writeHead(200, {
          "Content-Type": "text/plain; charset=utf-8",
          "Content-Length": String(payload.length),
          "Content-Disposition": `attachment; filename="${name}"`,
          "Cache-Control": "no-store",
        });
        return res.end(payload);
      }
      return sendJson(res, 200, { ok: true, line: "amd", ...result });
    }

    // 自动刷课：拿 Devpoints → 换 GPU 小时（AMD 的"通过审核"主要靠学院课程 + 活动）
    if (path === "/api/admin/amd/study" && req.method === "POST") {
      let body = {};
      try {
        body = await readJson(req);
      } catch (cause) {
        return fail(res, cause);
      }
      const email = String(body.email ?? "").trim();
      const account = email ? amdPool.byEmail(email) : amdPool.accounts[0];
      if (!account) {
        return sendJson(res, 400, errorBody("池里没有账号：先导入 cookie。", "invalid_request_error", "no_account"));
      }
      const args = ["--cookie", String(account.apiKey ?? ""), "--email", account.email];
      const num = (value, fallback) => (Number.isFinite(Number(value)) ? String(Number(value)) : String(fallback));
      args.push("--step", num(body.step, 30), "--target", num(body.target, 3600));
      args.push("--pause", num(body.pause, 1), "--max-calls", num(body.maxCalls, 400));
      if (body.course) args.push("--course", String(body.course));
      if (body.limit) args.push("--limit", num(body.limit, 0));
      if (body.force) args.push("--force");
      try {
        return sendJson(res, 202, { ok: true, email: account.email, ...amdRegistrations.runSubcommand("study", { args, requested: 1, mode: "study" }) });
      } catch (cause) {
        return fail(res, cause);
      }
    }

    // 考试：取题 / 交卷 / 自动答题（挂后台任务，日志走 job.lines）
    if (path === "/api/admin/amd/exam" && req.method === "POST") {
      let body = {};
      try {
        body = await readJson(req);
      } catch (cause) {
        return fail(res, cause);
      }
      const email = String(body.email ?? "").trim();
      const account = email ? amdPool.byEmail(email) : amdPool.accounts[0];
      if (!account) {
        return sendJson(res, 400, errorBody("池里没有账号：先导入 cookie。", "invalid_request_error", "no_account"));
      }
      const courseId = String(body.course ?? "").trim();
      if (!courseId) {
        return sendJson(res, 400, errorBody("需要 course（课程 id）。", "invalid_request_error", "bad_request"));
      }
      const args = ["--course", courseId, "--cookie", String(account.apiKey ?? ""), "--email", account.email];
      if (body.child) args.push("--child", String(body.child));
      if (body.auto !== false) args.push("--auto", "--strategy", String(body.strategy ?? "first"));
      args.push("--rounds", String(Number(body.rounds ?? 3)));
      if (body.submit === true) args.push("--submit");
      try {
        return sendJson(res, 202, {
          ok: true,
          email: account.email,
          course: courseId,
          ...amdRegistrations.runSubcommand("exam", { args, requested: 1, mode: "exam" }),
        });
      } catch (cause) {
        return fail(res, cause);
      }
    }

    // 算力券：汇总（vouchers）/ 兑换（exchange）/ 免费模型（models）/ 累加到主号（consolidate）
    for (const [route, sub, mode] of [
      ["vouchers", "vouchers", "vouchers"],
      ["exchange", "exchange", "exchange"],
      ["models", "models", "models"],
      ["consolidate", "consolidate", "consolidate"],
    ]) {
      if (path !== `/api/admin/amd/${route}` || req.method !== "POST") continue;
      let body = {};
      try {
        body = await readJson(req);
      } catch (cause) {
        return fail(res, cause);
      }
      const args = [];
      const email = String(body.email ?? "").trim();
      if (email) args.push("--email", email);
      if (body.master) args.push("--master", String(body.master));
      if (body.redeem === true) args.push("--redeem");
      if (body.dryRun === true) args.push("--dry-run");
      if (body.amount) args.push("--amount", String(Number(body.amount)));
      if (body.extra) args.push("--extra", JSON.stringify(body.extra));
      try {
        return sendJson(res, 202, {
          ok: true,
          route,
          ...amdRegistrations.runSubcommand(sub, { args, requested: 1, mode }),
        });
      } catch (cause) {
        return fail(res, cause);
      }
    }

    if (path === "/api/admin/amd/courses" && req.method === "GET") {
      const email = String(params.get("email") ?? "").trim();
      const account = email ? amdPool.byEmail(email) : amdPool.accounts[0];
      if (!account) {
        return sendJson(res, 400, errorBody("池里没有账号：先导入 cookie。", "invalid_request_error", "no_account"));
      }
      const run = spawnSync(
        amdRegistrations.python,
        ["-m", AMD_LINE.pyModule, "courses", "--json", "--cookie", String(account.apiKey ?? ""), "--email", account.email],
        { cwd: amdRegistrations.toolDir, encoding: "utf8", timeout: 60_000, windowsHide: true },
      );
      if (run.status !== 0) {
        return sendJson(res, 502, errorBody((run.stderr || run.stdout || "courses 调用失败").slice(0, 300), "upstream_error", "courses_failed"));
      }
      let parsed = null;
      try {
        parsed = JSON.parse(run.stdout);
      } catch {
        parsed = { raw: (run.stdout || "").slice(0, 400) };
      }
      return sendJson(res, 200, { ok: true, email: account.email, courses: parsed });
    }

    if (path === "/api/admin/amd/apply" && req.method === "POST") {
      try {
        const applied = amdRegistrations.apply();
        return sendJson(res, 200, { ok: true, line: "amd", ...applied, instances: amdInstanceSummary(amdPool) });
      } catch (cause) {
        return fail(res, cause);
      }
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
        providers: providerRuntime.stats(),
        cleverai: {
          accounts: cleveraiPool.size,
          healthy: cleveraiPool.healthy().length,
          keys: cleveraiPool.accounts.filter((a) => a.gatewayKey).length,
          appCheck: cleveraiRelay.vault.valid(),
          credits: cleveraiPool.stats().credits,
        },
        gabai: {
          accounts: gabaiPool.size,
          healthy: gabaiPool.healthy().length,
          keys: gabaiPool.keyed().length,
        },
        proxyscrape: {
          accounts: proxyscrapePool.size,
          proxies: proxyscrapePool.stats().proxies,
          activeProxies: proxyscrapePool.stats().activeProxies,
        },
        amd: {
          accounts: amdPool.size,
          instances: amdInstanceSummary(amdPool).withInstance,
          points: amdInstanceSummary(amdPool).totalPoints,
        },
        jevai: {
          accounts: jevaiPool.size,
          healthy: jevaiPool.healthy().length,
          keys: jevaiPool.keys().length,
        },
        ...Object.fromEntries(
          OPENAI_LINES.map((line) => {
            const pool = linePools.get(line.id);
            return [
              line.id,
              {
                accounts: pool.size,
                healthy: pool.healthy().length,
                keys: pool.keys().length,
                anonymous: line.anonymous === true,
              },
            ];
          }),
        ),
        requests: stats.requests,
        streams: stats.streams,
        uptime_s: Math.round((Date.now() - stats.startedAt) / 1000),
      });
    }

    if (path.startsWith("/api/admin/") || path.startsWith("/api/poe/")) {
      // Provider 管理面：独立前缀，先于 allmodels / poe 的分支处理
      if (path === "/api/admin/providers" || path.startsWith("/api/admin/providers/")) {
        // 口令或会话都行；worker 回推用的是 Bearer 管理口令，也走这条路。
        if (!requireAdmin(req, res)) return;
        return providerAdmin.handle(req, res, path, url);
      }
      return handleAdmin(req, res, path);
    }

    // Provider 反代面：`/p/<provider>/v1/*`（自带网关 Key 鉴权，见 provider-relay）
    if (path.startsWith("/p/")) {
      const handled = await providerRuntime.relay.handle(req, res, url, { auth: authorizeApi(req, url) });
      if (handled) return;
    }

    // 代理订阅（公开面，用令牌鉴权）：手机/路由器/别的电脑直接拉这个 URL。
    // 令牌只能读到代理，读不到账号、Key 或其它管理数据；可随时撤销。
    const proxyShareMatch = /^\/proxy\/([A-Za-z0-9_-]{8,})$/.exec(path);
    if (proxyShareMatch && (req.method === "GET" || req.method === "HEAD")) {
      const token = proxyShareMatch[1];
      const share = proxyShares.find(token);
      if (!share || share.revoked) {
        return sendJson(res, 404, errorBody("订阅不存在或已撤销。", "invalid_request_error", "not_found"));
      }
      const requested = String(url.searchParams.get("format") ?? "");
      const format = PROXY_FORMATS[requested] ? requested : share.format;
      const rows = proxyscrapePool.usable().map((proxy) => ({
        scheme: proxy.scheme,
        host: proxy.host,
        port: proxy.port,
        username: proxy.username,
        password: proxy.password,
        status: proxy.status,
        cooldownUntil: proxy.cooldownUntil ?? 0,
      }));
      const body = renderProxies(rows, format, { name: share.name });
      proxyShares.touch(token, { format });
      const download = ["1", "true"].includes(String(url.searchParams.get("download") ?? "").toLowerCase());
      res.writeHead(200, {
        "Content-Type": PROXY_FORMATS[format].contentType,
        "Content-Length": String(Buffer.byteLength(body)),
        "Cache-Control": "no-store",
        // 订阅客户端（Clash / sing-box）用这个决定多久回拉一次
        "Profile-Update-Interval": "6",
        "X-Proxy-Count": String(rows.length),
        ...(download ? { "Content-Disposition": `attachment; filename="${proxyExportFileName(format)}"` } : {}),
      });
      return res.end(req.method === "HEAD" ? "" : body);
    }

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

    // 六条 OpenAI 兼容线的反代面：/<line>/v1/*（鉴权在上面的 auth 里做过）
    const relayLine = OPENAI_LINES.find((line) => path.startsWith(line.relayPrefix));
    if (relayLine && path.startsWith(relayLine.relayPrefix)) {
      const upstreamPath = `/${path.slice(relayLine.relayPrefix.length - 1)}`; // "/v1/chat/completions"
      const relay = lineRelays.get(relayLine.id);
      let body = null;
      if (req.method !== "GET" && req.method !== "HEAD") {
        try {
          body = Buffer.from(await readBody(req));
        } catch (cause) {
          return fail(res, cause);
        }
      }
      stats.requests++;
      let result;
      try {
        const controller = new AbortController();
        res.on("close", () => controller.abort());
        result = await relay.forward(req, upstreamPath, { body, headers: req.headers, signal: controller.signal });
      } catch (cause) {
        return fail(res, cause);
      }
      const { response, account } = result;
      if (!response.ok) {
        const text = await response.text().catch(() => "");
        const explained = explainModelError(relayLine, response.status, text);
        if (explained) {
          return sendJson(res, response.status, explained, { "X-Line-Account": String(account.id) });
        }
        return sendJson(
          res,
          response.status,
          errorBody(text.slice(0, 400) || `上游 HTTP ${response.status}`, "upstream_error"),
          { "X-Line-Account": String(account.id) },
        );
      }
      const contentType = response.headers.get("content-type") ?? "application/json";
      if (contentType.includes("text/event-stream")) stats.streams++;
      res.writeHead(200, {
        "Content-Type": contentType,
        "Cache-Control": "no-store",
        "X-Accel-Buffering": "no",
        "X-Line": relayLine.id,
        "X-Line-Account": String(account.id),
      });
      if (!response.body) return res.end();
      try {
        for await (const chunk of response.body) {
          if (res.writableEnded) break;
          if (!res.write(chunk)) {
            await new Promise((resolve) => {
              res.once("drain", resolve);
              res.once("close", resolve);
            });
          }
        }
      } catch (cause) {
        log("error", `${relayLine.id} stream failed: ${cause?.message ?? cause}`);
        if (!res.writableEnded) res.destroy();
        return;
      }
      if (!res.writableEnded) res.end();
      return;
    }

    // jev-ai.org 反代面：/jevai/v1/*。上游说 OpenAI 形状（models / systemone …），
    // 所以只换 Authorization + 轮询 + 故障切换；未知端点会翻成人话。
    if (path === "/jevai" || path.startsWith("/jevai/")) {
      const upstreamPath = path.slice("/jevai".length) || "/v1/models";
      if (!upstreamPath.startsWith("/v1/")) {
        return sendJson(res, 404, errorBody(`No jevai route for ${path}.`, "invalid_request_error", "not_found"));
      }
      let body = null;
      if (req.method !== "GET" && req.method !== "HEAD") {
        try {
          body = Buffer.from(await readBody(req));
        } catch (cause) {
          return fail(res, cause);
        }
      }
      stats.requests++;
      let result;
      try {
        result = await jevaiRelay.forward(req, upstreamPath, {
          body,
          headers: req.headers,
          signal: (() => {
            const controller = new AbortController();
            res.on("close", () => controller.abort());
            return controller.signal;
          })(),
        });
      } catch (cause) {
        return fail(res, cause);
      }
      const { response, account } = result;
      if (!response.ok) {
        const text = await response.text().catch(() => "");
        const explained = explainUpstreamError(response.status, text);
        if (explained) {
          return sendJson(res, response.status, explained, { "X-Jevai-Account": String(account.id) });
        }
        return sendJson(res, response.status, errorBody(text.slice(0, 400) || `上游 HTTP ${response.status}`, "upstream_error"), {
          "X-Jevai-Account": String(account.id),
        });
      }
      const contentType = response.headers.get("content-type") ?? "application/json";
      if (contentType.includes("text/event-stream")) stats.streams++;
      res.writeHead(200, {
        "Content-Type": contentType,
        "Cache-Control": "no-store",
        "X-Accel-Buffering": "no",
        "X-Jevai-Account": String(account.id),
      });
      if (!response.body) {
        return res.end();
      }
      try {
        for await (const chunk of response.body) {
          if (res.writableEnded) break;
          if (!res.write(chunk)) {
            await new Promise((resolve) => {
              res.once("drain", resolve);
              res.once("close", resolve);
            });
          }
        }
      } catch (cause) {
        log("error", `jevai stream failed: ${cause?.message ?? cause}`);
        if (!res.writableEnded) res.destroy();
        return;
      }
      if (!res.writableEnded) res.end();
      return;
    }

    // gab.ai 反代面：/gab/v1/*。上游本来就是 OpenAI / Anthropic 形状，直通，
    // 只把 Authorization 换成号池里的 `gab_…` key，并做轮询 + 故障切换。
    if (path === "/gab/v1/models" || path.startsWith("/gab/v1/")) {
      const gabPath = path.slice("/gab".length);
      if (!GAB_PATHS.has(gabPath)) {
        return sendJson(res, 404, errorBody(`No gab route for ${path}.`, "invalid_request_error", "not_found"));
      }
      let body = null;
      if (req.method !== "GET" && req.method !== "HEAD") {
        try {
          body = await GabaiProxy.readBody(req);
        } catch (cause) {
          return fail(res, cause);
        }
      }
      stats.requests++;
      return gabaiProxy.forward(req, res, gabPath, { body, headers: req.headers });
    }

    // Clever AI Hub 面：/cleverai/v1/*（与 /v1/* 和 /p/* 互不重叠）
    if (path === "/cleverai" || path.startsWith("/cleverai/")) {
      return handleCleverai(req, res, path, url, auth);
    }


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

  return { server, adminToken, allmodels, allmodelsPool, providers: providerRuntime, cleverai, cleveraiPool, cleveraiRelay };
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
