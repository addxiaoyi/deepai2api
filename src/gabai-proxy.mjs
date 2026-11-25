// gab.ai 反向代理 + “网页反代出 apikey”。
//
// 两件事：
//   1. 把 `gab.ai/v1/*`（OpenAI Chat Completions / Responses）与 `gab.ai/v1/messages`
//      （Anthropic Messages）映射到网关自己的 `/gab/v1/*`，用号池里的 `gab_…` key
//      轮询 + 故障切换 + 冷却，对外只暴露本项目铸造的 `sk-dcg-…`。
//   2. 用号池账号的 bearer（或账号密码重新登录）在真实站点上 `POST /json/api-keys`
//      取/补 `gab_…` key —— 这就是“网页反代出 apikey”：不手点面板，也不把凭据
//      发给调用方。
//
// 与既有 `/v1/*`（DeepAI 通道）完全分开：路径前缀不同、账号池不同、调度器不同。
import { Readable } from "node:stream";
import { randomUUID } from "node:crypto";

const RETRYABLE = new Set([401, 403, 408, 409, 425, 429, 500, 502, 503, 504]);
const COOLDOWN_AUTH_MS = 10 * 60 * 1000;
const COOLDOWN_RATE_MS = 45 * 1000;

/** gab.ai 的 OpenAI 兼容面：这些路径原样透传，只换 Authorization。 */
export const GAB_PATHS = new Set([
  "/v1/chat/completions",
  "/v1/completions",
  "/v1/responses",
  "/v1/messages",
  "/v1/models",
]);

export class GabaiProxy {
  /**
   * @param {{pool: object, log?: Function, upstreamBase?: string, timeoutMs?: number}} options
   */
  constructor({ pool, log = () => {}, upstreamBase = "https://gab.ai", timeoutMs = 600_000 } = {}) {
    this.pool = pool;
    this.log = log;
    this.upstream = String(upstreamBase).replace(/\/+$/, "");
    this.timeoutMs = timeoutMs;
  }

  /* ------------------------------------------------------------ upstream call */

  async #fetch(account, path, { method = "GET", headers = {}, body = null, signal = null } = {}) {
    const init = {
      method,
      headers: {
        ...headers,
        authorization: `Bearer ${account.apiKey}`,
        "content-type": headers["content-type"] ?? "application/json",
      },
      signal,
    };
    if (body && method !== "GET" && method !== "HEAD") init.body = body;
    return fetch(`${this.upstream}${path}`, init);
  }

  /**
   * 把一次上游响应原样搬回调用方。头里补 `x-gab-account`，方便排障。
   * 只在「响应头还没发出去」之前做故障切换，所以流式响应不会被截断。
   */
  async forward(req, res, path, { body, headers } = {}) {
    const exclude = [];
    const affinity = String(headers["x-gab-session-id"] ?? headers["x-session-id"] ?? "").slice(0, 120);
    let lastError = null;

    for (let attempt = 0; attempt < 3; attempt++) {
      const account = this.pool.pick({ exclude, affinity: attempt === 0 ? affinity : "" });
      if (!account) break;
      exclude.push(account.id);

      const controller = new AbortController();
      const timer = setTimeout(() => controller.abort(), this.timeoutMs);
      let upstream;
      try {
        upstream = await this.#fetch(account, path, {
          method: req.method,
          headers: {
            "content-type": headers["content-type"] ?? "application/json",
            accept: headers.accept ?? "*/*",
            ...(headers["anthropic-version"] ? { "anthropic-version": headers["anthropic-version"] } : {}),
            ...(headers["x-gab-coding-tier"] ? { "x-gab-coding-tier": headers["x-gab-coding-tier"] } : {}),
            ...(headers["x-gab-agent-client"] ? { "x-gab-agent-client": headers["x-gab-agent-client"] } : {}),
            ...(headers["x-request-id"] ? { "x-request-id": headers["x-request-id"] } : {}),
          },
          body,
          signal: controller.signal,
        });
      } catch (error) {
        clearTimeout(timer);
        lastError = error;
        this.pool.recordResult(account.id, { ok: false, error: `transport: ${error.message}`, cooldownMs: COOLDOWN_RATE_MS });
        this.log("warn", `gabai: ${account.email} transport error: ${error.message}`);
        continue;
      }

      // 401/403 might be the Plus gate rather than a dead key: read the JSON once.
      if (upstream.status === 403 && !res.headersSent) {
        const probe = await upstream.clone().text().catch(() => "");
        const plus = /plus_required/.test(probe);
        if (plus) {
          this.pool.recordResult(account.id, { ok: false, plusBlocked: true });
          this.log("warn", `gabai: ${account.email} 需要 Plus（已标记 apiBlocked）`);
          clearTimeout(timer);
          // 换下一个能用的账号；没有就如实回 403。
          lastError = Object.assign(new Error("plus_required"), { status: 403, body: probe });
          continue;
        }
      }

      if (RETRYABLE.has(upstream.status) && attempt < 2 && !res.headersSent) {
        const text = await upstream.clone().text().catch(() => "");
        const cooldown = upstream.status === 429 ? COOLDOWN_RATE_MS : COOLDOWN_AUTH_MS;
        this.pool.recordResult(account.id, {
          ok: false,
          error: `HTTP ${upstream.status} ${text.slice(0, 160)}`,
          cooldownMs: cooldown,
        });
        lastError = Object.assign(new Error(`upstream HTTP ${upstream.status}`), { status: upstream.status, body: text });
        this.log("warn", `gabai: ${account.email} HTTP ${upstream.status}，切换下一个账号`);
        clearTimeout(timer);
        continue;
      }

      // 正常（或不可重试的错误）——原样回给调用方。
      clearTimeout(timer);
      this.pool.recordResult(account.id, { ok: upstream.ok });
      return this.#pipe(res, upstream, account);
    }

    return this.#fail(res, lastError);
  }

  #pipe(res, upstream, account) {
    res.statusCode = upstream.status;
    for (const [name, value] of upstream.headers) {
      if (["content-encoding", "content-length", "transfer-encoding", "connection"].includes(name)) continue;
      try {
        res.setHeader(name, value);
      } catch {
        // 非法头单个丢掉，不影响整体
      }
    }
    res.setHeader("x-gab-account", account.id);
    res.setHeader("access-control-expose-headers", "x-gab-account, x-request-id");
    if (!upstream.body) {
      res.end();
      return;
    }
    Readable.fromWeb(upstream.body)
      .on("error", (error) => {
        this.log("warn", `gabai: stream aborted: ${error.message}`);
        res.destroy();
      })
      .pipe(res);
  }

  #fail(res, error) {
    if (res.headersSent) {
      res.destroy();
      return;
    }
    const status = error?.status && error.status >= 400 ? error.status : 503;
    const body = {
      error: {
        message:
          error?.message === "plus_required"
            ? "号池里没有可用的 gab.ai Plus 账号，开发者 API 拒绝访问。"
            : error?.message ?? "没有可用的 gab.ai 账号。",
        type: status === 503 ? "service_unavailable" : "upstream_error",
        code: error?.message === "plus_required" ? "plus_required" : "all_gabai_accounts_failed",
      },
    };
    const text = JSON.stringify(body);
    res.statusCode = status;
    res.setHeader("content-type", "application/json; charset=utf-8");
    res.end(text);
  }

  /* --------------------------------------------------------------- mint key */

  /**
   * 在真实站点上给一个号池账号取/补 `gab_…` key。
   * 顺序：现有 bearer → refresh token → 账号密码重新登录。
   * 成功后把凭据与新 key 写回号池条目（幂等按 email 合并）。
   */
  async mintApiKey(account, { name = "", save = true } = {}) {
    if (!account) throw Object.assign(new Error("号池里没有这个账号"), { status: 404 });

    const creds = await this.#credentials(account);
    const label = String(name || account.apiKeyName || `gw-${new Date().toISOString().slice(0, 10)}`).slice(0, 60);
    const response = await fetch(`${this.upstream}/json/api-keys`, {
      method: "POST",
      headers: {
        "content-type": "application/json",
        authorization: `Bearer ${creds.access}`,
        "x-platform": "web",
        origin: this.upstream,
        referer: `${this.upstream}/app`,
      },
      body: JSON.stringify({ name: label }),
    });
    const text = await response.text();
    if (response.status >= 400) {
      throw Object.assign(new Error(`api key 创建失败 HTTP ${response.status}: ${text.slice(0, 200)}`), {
        status: response.status,
      });
    }
    let payload = {};
    try {
      payload = JSON.parse(text);
    } catch {
      payload = {};
    }
    const apiKey = String(payload?.apiKey?.key ?? "");
    if (!apiKey.startsWith("gab_")) {
      throw Object.assign(new Error("站点没有返回 gab_ 开头的 key"), { status: 502 });
    }

    if (save) {
      this.pool.update(account.id, {
        apiKey,
        apiKeyId: String(payload?.apiKey?.id ?? ""),
        apiKeyName: String(payload?.apiKey?.name ?? label),
        accessToken: creds.access,
        refreshToken: creds.refresh,
        expiresAt: creds.expiresAt,
        apiBlocked: "",
        healthy: true,
        cooldownUntil: 0,
        lastError: null,
      });
    }
    return {
      email: account.email,
      apiKey,
      apiKeyId: String(payload?.apiKey?.id ?? ""),
      apiKeyName: String(payload?.apiKey?.name ?? label),
      through: creds.through,
    };
  }

  /** 拿到一个能用的 bearer；失败抛 401。 */
  async #credentials(account) {
    if (account.accessToken && (!account.expiresAt || Date.parse(account.expiresAt) > Date.now() + 60_000)) {
      const probe = await fetch(`${this.upstream}/json/user/me/slim`, {
        headers: { authorization: `Bearer ${account.accessToken}`, "x-platform": "web" },
      }).catch(() => null);
      if (probe?.ok) return { access: account.accessToken, refresh: account.refreshToken ?? "", expiresAt: account.expiresAt ?? "", through: "bearer" };
    }

    if (account.refreshToken) {
      const refreshed = await fetch(`${this.upstream}/json/auth/refresh-token`, {
        method: "POST",
        headers: { "content-type": "application/json", "x-platform": "web" },
        body: JSON.stringify({ refreshToken: account.refreshToken, clientInfo: { platform: "web" } }),
      }).catch(() => null);
      if (refreshed?.ok) {
        const data = await refreshed.json().catch(() => ({}));
        const access = String(data?.tokens?.access ?? "");
        if (access) {
          return {
            access,
            refresh: String(data?.tokens?.refresh ?? account.refreshToken),
            expiresAt: String(data?.tokens?.expiresAt ?? ""),
            through: "refresh",
          };
        }
      }
    }

    if (account.password) {
      const login = await fetch(`${this.upstream}/json/auth/login/email`, {
        method: "POST",
        headers: { "content-type": "application/json", "x-platform": "web" },
        body: JSON.stringify({ email: account.email, password: account.password, clientInfo: { platform: "web" } }),
      }).catch(() => null);
      if (login?.ok) {
        const data = await login.json().catch(() => ({}));
        const access = String(data?.tokens?.access ?? "");
        if (access) {
          return {
            access,
            refresh: String(data?.tokens?.refresh ?? ""),
            expiresAt: String(data?.tokens?.expiresAt ?? ""),
            through: "login",
          };
        }
      }
    }

    throw Object.assign(new Error("该账号没有可用登录态，无法补发 gab_ key"), { status: 401 });
  }

  /** 把一次原始请求的 body 读成 Buffer（反代需要原样透传）。 */
  static async readBody(req, limit = 32 * 1024 * 1024) {
    const chunks = [];
    let size = 0;
    for await (const chunk of req) {
      size += chunk.length;
      if (size > limit) throw Object.assign(new Error("请求体过大"), { status: 413 });
      chunks.push(chunk);
    }
    return Buffer.concat(chunks);
  }

  static requestId() {
    return randomUUID();
  }
}