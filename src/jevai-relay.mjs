// jev-ai.org 反代层：把号池里的 `sk-glm5-…` 包成网关自己的 OpenAI 兼容面。
//
// 上游是 `https://jev-ai.org/api/v1`（`GET /models`、`POST /systemone` 是确定存在的，
// 其它 OpenAI 端点按"原样透传 + 404 说明"处理）。这一层只做三件事：
//   1. 选号 + 换 Authorization（下游永远只看到 `sk-dcg-…`）；
//   2. 原样透传请求体与响应（含 SSE），失败按状态分级冷却并换号；
//   3. 把上游的 "Unknown API endpoint" 翻成明确提示，避免下游以为是鉴权问题。
import { randomUUID } from "node:crypto";

const RETRYABLE = new Set([401, 403, 408, 425, 429, 500, 502, 503, 504]);
const COOLDOWN_AUTH_MS = 10 * 60 * 1000;
const COOLDOWN_RATE_MS = 60 * 1000;

// 上游确实存在的路径（其余按透传处理，但会被上游用 404 + 说明拒绝）
export const JEV_PATHS = new Set([
  "/v1/models",
  "/v1/models/",
  "/v1/systemone",
  "/v1/chat/completions",
  "/v1/completions",
  "/v1/responses",
  "/v1/embeddings",
]);

export class JevaiRelay {
  /**
   * @param {{pool: object, log?: Function, upstreamBase?: string, timeoutMs?: number}} options
   */
  constructor({ pool, log = () => {}, upstreamBase = "https://jev-ai.org", timeoutMs = 300_000 } = {}) {
    this.pool = pool;
    this.log = log;
    this.upstream = String(upstreamBase || "https://jev-ai.org").replace(/\/+$/, "");
    this.timeoutMs = timeoutMs;
  }

  status() {
    return {
      ok: true,
      upstream: `${this.upstream}/api`,
      pool: this.pool?.stats?.() ?? null,
      keyPrefix: "sk-glm5-",
    };
  }

  /** 选号：`exclude` 让重试落到别的账号。 */
  acquire({ exclude = [] } = {}) {
    const account = this.pool.pick({ exclude });
    if (!account) {
      const error = new Error("号池里没有可用账号：先注册一批，或清掉冷却。");
      error.status = 503;
      error.code = "no_account";
      throw error;
    }
    return account;
  }

  /**
   * 透传一次请求。
   *
   * @returns {{response: Response, account: object}}
   */
  async forward(req, path, { body = null, headers = {}, exclude = [], signal = null } = {}) {
    let lastError = null;
    for (let attempt = 0; attempt < 3; attempt++) {
      const account = this.acquire({ exclude });
      exclude.push(account.id);
      const controller = new AbortController();
      const timer = setTimeout(() => controller.abort(), this.timeoutMs);
      const onAbort = () => controller.abort();
      signal?.addEventListener?.("abort", onAbort, { once: true });
      try {
        const response = await fetch(`${this.upstream}/api${path}`, {
          method: req.method,
          headers: {
            "Content-Type": headers["content-type"] ?? "application/json",
            Accept: headers.accept ?? "*/*",
            ...(headers["anthropic-version"] ? { "anthropic-version": headers["anthropic-version"] } : {}),
            Authorization: `Bearer ${account.apiKey}`,
            "User-Agent": headers["user-agent"] ?? "deepai2api-gateway",
          },
          ...(body && req.method !== "GET" && req.method !== "HEAD" ? { body } : {}),
          signal: controller.signal,
        });

        if (response.status === 404 && !response.ok) {
          // 上游对未知路径会回 {"error":{"message":"Unknown API endpoint…"}}
          this.pool.recordResult(account.id, { ok: true });
          return { response, account };
        }
        if (RETRYABLE.has(response.status) && attempt < 2) {
          const text = await response.clone().text().catch(() => "");
          this.pool.recordResult(account.id, {
            ok: false,
            error: `HTTP ${response.status} ${text.slice(0, 120)}`,
            cooldownMs: response.status === 429 ? COOLDOWN_RATE_MS : COOLDOWN_AUTH_MS,
          });
          lastError = new Error(`upstream HTTP ${response.status}`);
          lastError.status = response.status;
          this.log("warn", `jevai: ${account.email} HTTP ${response.status}，换下一个 Key`);
          continue;
        }
        // 4xx 里只有 401/403/429 才算 Key 的问题；404/400 是调用方或上游端点的问题
        const blamed = [401, 403, 429].includes(response.status) || response.status >= 500;
        this.pool.recordResult(account.id, { ok: !blamed });
        return { response, account };
      } catch (error) {
        this.pool.recordResult(account.id, { ok: false, error: `transport: ${error.message}`, cooldownMs: COOLDOWN_RATE_MS });
        lastError = error;
        this.log("warn", `jevai: ${account.email} transport error: ${error.message}`);
      } finally {
        clearTimeout(timer);
        signal?.removeEventListener?.("abort", onAbort);
      }
    }
    const error = new Error(`所有 JEV Key 都失败了（${lastError?.message ?? "unknown"}）`);
    error.status = lastError?.status && lastError.status < 500 ? lastError.status : 503;
    error.code = "all_keys_failed";
    throw error;
  }

  /** 给面板"接入其他工具"用的信息：基址 + 一把可用网关 Key 的前缀。 */
  connection() {
    return {
      openai_base: "/jevai/v1",
      key_prefix: "sk-glm5-",
      keys: this.pool.keys().length,
    };
  }
}

/** 上游 404 的 JSON 里带 "Unknown API endpoint" 时，给下游一句人话。 */
export function explainUpstreamError(status, text) {
  if (status === 404 && /unknown api endpoint/i.test(text ?? "")) {
    return {
      error: {
        message:
          "上游没有这个端点。jev-ai.org 的模型 API 目前确定支持 /api/v1/models 与 /api/v1/systemone；" +
          "chat/completions 若 404 属上游未开放，不是 Key 的问题。",
        type: "invalid_request_error",
        code: "upstream_endpoint_missing",
      },
    };
  }
  return null;
}

export const requestId = () => `jev-${randomUUID()}`;
