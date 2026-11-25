// Provider 管理接口 —— `/api/admin/providers/*`。
//
// 与既有 `/api/admin/allmodels/*`、`/api/api/poe/*` 并列，路由前缀独立，互不覆盖。
// 语义与 allmodels 面板一致（注册 / 号池 / 导入 / 健康 / 导出 / 自检），
// 但**每个 provider 一份号池**，所以 URL 里必须带 provider id。
import { PROVIDERS, getProvider, providerSummary } from "./provider-catalog.mjs";

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

const readJsonBody = async (req, limit = 2 * 1024 * 1024) => {
  const chunks = [];
  let size = 0;
  for await (const chunk of req) {
    size += chunk.length;
    if (size > limit) {
      const error = new Error("请求体过大。");
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
    const error = new Error("请求体不是合法 JSON。");
    error.status = 400;
    throw error;
  }
};

/**
 * @param {{runtime: ReturnType<import("./providers.mjs").createProviderRuntime>,
 *          store: object, log?: Function}} options
 */
export function createProviderAdmin({ runtime, store, log = () => {} }) {
  /** 处理 `/api/admin/providers...`；返回 true 表示已处理。 */
  async function handle(req, res, path, url) {
    // ---- 列表：所有 provider 的概览 ----
    if (path === "/api/admin/providers" && req.method === "GET") {
      return sendJson(res, 200, { ok: true, providers: runtime.list(), schemas: PROVIDERS.map((p) => `dcg.pool/${p.id}@1`) }), true;
    }

    const match = /^\/api\/admin\/providers\/([a-z0-9_-]+)(\/.*)?$/i.exec(path);
    if (!match) return false;
    const providerId = match[1].toLowerCase();
    let resolved;
    try {
      resolved = runtime.requireProvider(providerId);
    } catch (error) {
      sendJson(res, error.status ?? 404, errorBody(error.message, "invalid_request_error", error.code ?? "unknown_provider"));
      return true;
    }
    const { provider, pool, registrations } = resolved;
    const rest = (match[2] || "").replace(/\/+$/, "");

    try {
      /* ------------------------------------------------------------ 概览 */
      if (rest === "" && req.method === "GET") {
        return sendJson(res, 200, {
          ok: true,
          ...providerSummary(provider),
          pool: pool.stats(),
          job: registrations.job,
          toolPresent: registrations.toolPresent,
          python: registrations.python,
          toolDir: registrations.toolDir,
          endpoints: runtime.endpoints(providerId),
        }), true;
      }

      /* ------------------------------------------------------------ 注册 */
      if (rest === "/register" && req.method === "GET") {
        return sendJson(res, 200, { ok: true, provider: providerId, job: registrations.job, running: registrations.job?.status === "running" }), true;
      }
      if (rest === "/register" && req.method === "POST") {
        const body = await readJsonBody(req);
        const started = registrations.start(body);
        return sendJson(res, 202, { ok: true, ...started }), true;
      }
      if (rest === "/register/stop" && req.method === "POST") {
        return sendJson(res, 200, { ok: true, ...registrations.stop() }), true;
      }

      /* ------------------------------------------------------------ 号池 */
      if (rest === "/pool" && req.method === "GET") {
        pool.reload();
        const limit = Math.min(500, Math.max(1, Number(url?.searchParams?.get("limit") ?? 200) || 200));
        const rows = pool.accounts.slice(-limit).map(runtime.maskAccount);
        return sendJson(res, 200, {
          ok: true,
          provider: providerId,
          schema: pool.toJSON().schema,
          panelBase: pool.panelBase,
          apiBase: pool.apiBase,
          updatedAt: pool.toJSON().updatedAt,
          stats: pool.stats(),
          accounts: rows,
        }), true;
      }

      if (rest === "/pool/import" && req.method === "POST") {
        const body = await readJsonBody(req);
        const record = body.account && typeof body.account === "object" ? body.account : body;
        const email = String(record.email ?? "").trim();
        if (!email && !record.apiKey && !record.accessToken) {
          return sendJson(res, 400, errorBody("账号至少要有 email / apiKey / accessToken 之一。", "invalid_request_error", "missing_account")), true;
        }
        const result = runtime.importAccount({
          pool,
          store,
          record: { ...record, provider: providerId },
          mintKey: body.mintKey !== false,
        });
        log("info", `${providerId} 入池 ${email || result.account.id}（号池 ${pool.size}）`);
        return sendJson(res, 200, {
          ok: true,
          created: result.created,
          account: runtime.maskAccount(result.account),
          gatewayKey: result.gatewayKey ? { id: result.gatewayKey.id, prefix: result.gatewayKey.prefix, plaintext: result.gatewayKey.plaintext } : null,
          pool: pool.stats(),
        }), true;
      }

      if (rest === "/pool/export" && (req.method === "POST" || req.method === "GET")) {
        let format = url?.searchParams?.get("format") ?? "jsonl";
        if (req.method === "POST") {
          const body = await readJsonBody(req).catch(() => ({}));
          format = body.format ?? format;
        }
        if (!["jsonl", "csv", "pool"].includes(format)) {
          return sendJson(res, 400, errorBody(`不支持的导出格式：${format}`, "invalid_request_error", "bad_format")), true;
        }
        const result = registrations.export({ format });
        if (url?.searchParams?.get("download") === "1") {
          const { readFileSync } = await import("node:fs");
          const payload = readFileSync(result.file);
          res.writeHead(200, {
            "Content-Type": format === "csv" ? "text/csv; charset=utf-8" : "application/json; charset=utf-8",
            "Content-Length": String(payload.length),
            "Content-Disposition": `attachment; filename="${result.file.split(/[\\/]/).pop()}"`,
            "Cache-Control": "no-store",
          });
          res.end(payload);
          return true;
        }
        return sendJson(res, 200, { ok: true, ...result }), true;
      }

      const health = /^\/pool\/([^/]+)\/health$/.exec(rest);
      if (health && req.method === "POST") {
        const body = await readJsonBody(req);
        const updated = pool.recordResult(health[1], {
          ok: body.ok === true,
          error: body.error ?? null,
          cooldownMs: Number(body.cooldownMs ?? 0) || 0,
        });
        if (!updated) return sendJson(res, 404, errorBody("号池里没有这个账号。", "invalid_request_error", "not_found")), true;
        return sendJson(res, 200, { ok: true, account: runtime.maskAccount(updated), pool: pool.stats() }), true;
      }

      const remove = /^\/pool\/([^/]+)$/.exec(rest);
      if (remove && req.method === "DELETE") {
        if (!pool.remove(remove[1])) {
          return sendJson(res, 404, errorBody("号池里没有这个账号。", "invalid_request_error", "not_found")), true;
        }
        return sendJson(res, 200, { ok: true, pool: pool.stats() }), true;
      }

      /* ------------------------------------------------------------ Key 视图 */
      if (rest === "/keys" && req.method === "GET") {
        pool.reload();
        return sendJson(res, 200, {
          ok: true,
          provider: providerId,
          keys: pool.accounts.map((account) => ({
            id: account.id,
            email: account.email,
            apiKeyPreview: account.apiKey ? `${account.apiKey.slice(0, 8)}…` : "",
            apiKeyId: account.keyId ?? "",
            organizationId: account.organizationId ?? "",
            accessTokenExpiresAt: account.accessTokenExpiresAt ?? 0,
            hasRefreshToken: Boolean(account.refreshToken),
            gatewayKeyPrefix: account.gatewayKey?.prefix ?? "",
            status: account.status,
          })),
        }), true;
      }

      /* ------------------------------------------------------------ 自检 */
      if (rest === "/probe" && req.method === "POST") {
        const result = await runtime.probe(providerId);
        return sendJson(res, result.ok ? 200 : 502, { ok: result.ok, ...result }), true;
      }

      /* ------------------------------------------------------------ 工具层 */
      if (rest === "/tools" && req.method === "GET") {
        return sendJson(res, 200, {
          ok: true,
          provider: providerId,
          openaiBase: `/p/${providerId}/v1`,
          tools: [
            { name: "provider_pool_list", description: "列出该 provider 号池（脱敏）" },
            { name: "provider_register_start", description: "起一轮批量注册" },
            { name: "provider_register_stop", description: "停止当前注册任务" },
            { name: "provider_pool_export", description: "导出账号 + 面板 Key + 网关 Key" },
          ],
        }), true;
      }

      return sendJson(res, 404, errorBody(`No provider route for ${req.method} ${path}.`, "invalid_request_error", "not_found")), true;
    } catch (error) {
      const status = error.status && error.status >= 400 ? error.status : 500;
      log("warn", `${providerId} admin error: ${error.message}`);
      return sendJson(res, status, errorBody(error.message, status < 500 ? "invalid_request_error" : "server_error", error.code ?? null)), true;
    }
  }

  return { handle };
}

export { getProvider };
