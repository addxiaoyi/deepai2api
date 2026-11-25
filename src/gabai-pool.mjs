// gab.ai 号池（`data/gabai-pool.json`）— 与 Allmodels / DeepAI 号池并列的独立文件。
//
// 为什么单开一个文件而不是塞进 gateway-state.json：
//   1. 账号凭据形状完全不同（gab_… developer key + bearer token + plus 标记）；
//   2. 调度语义不同（gab.ai 的 key 有 plus 门槛，要单独标记上游拒绝）；
//   3. 现有 `Store` 的 accounts[] 是 DeepAI 通道账号，混进去会污染调度器。
//
// 因此本模块只写自己的文件、只发自己的网关 Key（同样 `sk-dcg-…`），
// 复用 `keys.mjs` 与 `store.addKey()`，相互之间零耦合。
//
// Schema: docs/GABAI-POOL.md (`dcg.pool/gabai@1`)。
import { existsSync, mkdirSync, readFileSync, renameSync, writeFileSync } from "node:fs";
import { dirname, resolve } from "node:path";
import { randomUUID } from "node:crypto";

import { mintKey as mintGatewaySecret } from "./keys.mjs";

export const POOL_SCHEMA = "dcg.pool/gabai@1";
export const POOL_VERSION = 1;
export const DEFAULT_BASE_URL = "https://gab.ai";
export const DEFAULT_API_BASE = "https://gab.ai/v1";

const clamp = (value, min, max) => Math.min(max, Math.max(min, Number(value) || 0));

export class GabaiPool {
  /**
   * @param {string} path   pool file, normally `<dataDir>/gabai-pool.json`
   * @param {{baseUrl?: string, apiBase?: string, log?: Function}} options
   */
  constructor(path, { baseUrl = DEFAULT_BASE_URL, apiBase = DEFAULT_API_BASE, log = () => {} } = {}) {
    this.path = resolve(path);
    this.baseUrl = baseUrl;
    this.apiBase = apiBase;
    this.log = log;
    this.state = this.#load();
    this.cursor = 0;
  }

  static at(dataDir, options) {
    return new GabaiPool(resolve(dataDir, "gabai-pool.json"), options);
  }

  #load() {
    const empty = { schema: POOL_SCHEMA, version: POOL_VERSION, provider: "gabai", accounts: [] };
    if (!existsSync(this.path)) return empty;
    try {
      const parsed = JSON.parse(readFileSync(this.path, "utf8"));
      const accounts = Array.isArray(parsed.accounts) ? parsed.accounts.map(normalizeAccount) : [];
      return {
        schema: POOL_SCHEMA,
        version: POOL_VERSION,
        provider: "gabai",
        baseUrl: parsed.baseUrl || this.baseUrl,
        apiBase: parsed.apiBase || this.apiBase,
        updatedAt: parsed.updatedAt ?? null,
        accounts,
      };
    } catch (error) {
      this.log("warn", `gabai: pool file unreadable (${error.message}), starting empty`);
      return empty;
    }
  }

  /* ---------------------------------------------------------------- reading */

  get accounts() {
    return this.state.accounts;
  }

  get size() {
    return this.state.accounts.length;
  }

  getAccount(id) {
    return this.state.accounts.find((a) => a.id === id) ?? null;
  }

  byEmail(email) {
    const needle = String(email ?? "").trim().toLowerCase();
    return this.state.accounts.find((a) => a.email.toLowerCase() === needle) ?? null;
  }

  /**
   * 可承接流量的账号：active + healthy + 不在冷却 + 有可用 key。
   * `apiBlocked` 为真（Plus 门槛）的账号只用于「出 key / 面板操作」，不参与模型转发。
   */
  healthy(now = Date.now()) {
    return this.state.accounts.filter(
      (a) =>
        a.status === "active" &&
        a.healthy !== false &&
        (a.cooldownUntil ?? 0) <= now &&
        Boolean(a.apiKey) &&
        !a.apiBlocked,
    );
  }

  /** 全部有 key 的账号（含 Plus 受限的），用于导出/出 key。 */
  keyed() {
    return this.state.accounts.filter((a) => a.status === "active" && Boolean(a.apiKey));
  }

  /**
   * Round-robin over healthy accounts; `exclude` skips ids already tried in this
   * request so a retry lands elsewhere. `affinity` keeps one conversation on one
   * account for a while (sticky sessions).
   */
  pick({ exclude = [], now = Date.now(), affinity = "" } = {}) {
    const candidates = this.healthy(now).filter((a) => !exclude.includes(a.id));
    if (!candidates.length) return null;
    if (affinity) {
      const sticky = this.state.affinity?.[affinity];
      const bound = sticky ? candidates.find((a) => a.id === sticky.id && sticky.until > now) : null;
      if (bound) return bound;
    }
    const picked = candidates[this.cursor % candidates.length];
    this.cursor = (this.cursor + 1) % Math.max(1, candidates.length);
    if (affinity) {
      this.state.affinity = this.state.affinity ?? {};
      this.state.affinity[affinity] = { id: picked.id, until: now + 30 * 60 * 1000 };
    }
    return picked;
  }

  stats() {
    const now = Date.now();
    const out = {
      schema: POOL_SCHEMA,
      total: this.state.accounts.length,
      active: 0,
      cooling: 0,
      unhealthy: 0,
      plusBlocked: 0,
      keys: 0,
      requests: 0,
      updatedAt: this.state.updatedAt ?? null,
      last24h: 0,
    };
    const dayAgo = now - 24 * 3600 * 1000;
    for (const account of this.state.accounts) {
      if (account.status === "active") out.active++;
      if (account.healthy === false) out.unhealthy++;
      if ((account.cooldownUntil ?? 0) > now) out.cooling++;
      if (account.apiBlocked) out.plusBlocked++;
      if (account.apiKey) out.keys++;
      out.requests += account.requests ?? 0;
      if (account.createdAt && Date.parse(account.createdAt) >= dayAgo) out.last24h++;
    }
    return out;
  }

  /* ---------------------------------------------------------------- writing */

  /** 合并一个账号：先去重 apiKey，再按 email；重导入刷新凭据但保留计数与已发网关 Key。 */
  add(record) {
    const incoming = normalizeAccount(record);
    const existing =
      (incoming.apiKey && this.state.accounts.find((a) => a.apiKey && a.apiKey === incoming.apiKey)) ||
      (incoming.email && this.byEmail(incoming.email)) ||
      null;
    const now = new Date().toISOString();

    if (existing) {
      const keep = {
        id: existing.id,
        requests: existing.requests ?? 0,
        failures: existing.failures ?? 0,
        createdAt: existing.createdAt,
        gatewayKey: existing.gatewayKey ?? null,
      };
      Object.assign(existing, incoming, keep, {
        id: existing.id,
        createdAt: keep.createdAt ?? incoming.createdAt ?? now,
        updatedAt: now,
        status: incoming.status === "revoked" ? "revoked" : existing.status === "revoked" ? "revoked" : incoming.status,
      });
      this.save();
      return { account: existing, created: false };
    }

    const created = {
      ...incoming,
      id: incoming.id || randomUUID(),
      createdAt: incoming.createdAt || now,
      updatedAt: now,
      requests: 0,
      failures: 0,
      healthy: true,
      cooldownUntil: 0,
      lastError: null,
      gatewayKey: incoming.gatewayKey ?? null,
    };
    this.state.accounts.push(created);
    this.save();
    return { account: created, created: true };
  }

  update(id, patch) {
    const account = this.getAccount(id);
    if (!account) return null;
    Object.assign(account, patch, { updatedAt: new Date().toISOString() });
    this.save();
    return account;
  }

  remove(id) {
    const before = this.state.accounts.length;
    this.state.accounts = this.state.accounts.filter((a) => a.id !== id);
    if (this.state.accounts.length === before) return false;
    if (this.state.affinity) {
      for (const [affinity, bound] of Object.entries(this.state.affinity)) {
        if (bound?.id === id) delete this.state.affinity[affinity];
      }
    }
    this.save();
    return true;
  }

  /** 记录一次上游调用结果；失败累计并进入冷却，Plus 门槛单独标记。 */
  recordResult(id, { ok, error = null, cooldownMs = 0, plusBlocked = false, now = Date.now() } = {}) {
    const account = this.getAccount(id);
    if (!account) return null;
    account.requests = (account.requests ?? 0) + 1;
    account.lastUsedAt = new Date(now).toISOString();
    if (plusBlocked) {
      account.apiBlocked = "plus_required";
      account.healthy = true;
      account.lastError = "plus_required";
    } else if (ok) {
      account.healthy = true;
      account.lastError = null;
      account.cooldownUntil = 0;
    } else {
      account.failures = (account.failures ?? 0) + 1;
      account.lastError = String(error ?? "unknown").slice(0, 200);
      account.healthy = false;
      account.cooldownUntil = cooldownMs > 0 ? now + cooldownMs : 0;
    }
    this.save();
    return account;
  }

  /* ---------------------------------------------------------------- exports */

  records() {
    return this.state.accounts.map((account) => ({ ...account }));
  }

  exportTo(file, { format = "jsonl" } = {}) {
    const target = resolve(file);
    mkdirSync(dirname(target), { recursive: true });
    const rows = this.records();
    if (format === "csv") {
      const header = [
        "email",
        "password",
        "api_key",
        "api_blocked",
        "plus",
        "engine",
        "gateway_key",
        "status",
        "created_at",
      ];
      const escape = (value) => `"${String(value ?? "").replaceAll('"', '""')}"`;
      const body = rows.map((row) =>
        [
          row.email,
          row.password,
          row.apiKey,
          row.apiBlocked ?? "",
          row.plus === true ? "yes" : row.plus === false ? "no" : "",
          row.engine,
          row.gatewayKey?.plaintext ?? "",
          row.status,
          row.createdAt,
        ]
          .map(escape)
          .join(","),
      );
      writeFileSync(target, [header.join(","), ...body].join("\n") + "\n", { mode: 0o600 });
    } else if (format === "pool") {
      writeFileSync(target, JSON.stringify(this.toJSON(), null, 2), { mode: 0o600 });
    } else {
      const body = rows.map((row) => JSON.stringify(row)).join("\n");
      writeFileSync(target, body ? body + "\n" : "", { mode: 0o600 });
    }
    return { file: target, rows: rows.length, format };
  }

  toJSON() {
    return {
      ...this.state,
      baseUrl: this.baseUrl,
      apiBase: this.apiBase,
      updatedAt: this.state.updatedAt,
    };
  }

  /** 立即落盘：结构变化必须扛得住崩溃。 */
  save() {
    this.state.updatedAt = new Date().toISOString();
    mkdirSync(dirname(this.path), { recursive: true });
    const tmp = `${this.path}.${randomUUID()}.tmp`;
    writeFileSync(tmp, JSON.stringify(this.toJSON(), null, 2), { mode: 0o600 });
    renameSync(tmp, this.path);
  }
}

/** 把 worker 推来的记录规格化成条目形状（兼容 snake_case 与 camelCase）。 */
export function normalizeAccount(raw = {}) {
  const apiKey = String(raw.apiKey ?? raw.api_key ?? "").trim();
  const email = String(raw.email ?? "").trim().toLowerCase();
  return {
    email,
    password: String(raw.password ?? ""),
    apiKey,
    apiKeyId: String(raw.apiKeyId ?? raw.api_key_id ?? ""),
    apiKeyName: String(raw.apiKeyName ?? raw.api_key_name ?? ""),
    apiBlocked: String(raw.apiBlocked ?? raw.api_blocked ?? ""),
    accessToken: String(raw.accessToken ?? raw.access_token ?? ""),
    refreshToken: String(raw.refreshToken ?? raw.refresh_token ?? ""),
    expiresAt: String(raw.expiresAt ?? raw.expires_at ?? ""),
    emailVerified: raw.emailVerified ?? raw.email_verified ?? null,
    plus: raw.plus ?? null,
    status: raw.status ?? "active",
    engine: raw.engine ?? "protocol",
    createdAt: raw.createdAt ?? raw.created_at ?? new Date().toISOString(),
    cookies: Array.isArray(raw.cookies) ? raw.cookies : [],
    gatewayKey: raw.gatewayKey ?? null,
    healthy: raw.healthy !== false,
    cooldownUntil: clamp(raw.cooldownUntil ?? 0, 0, Number.MAX_SAFE_INTEGER),
  };
}

/**
 * 给一个号池账号发网关 Key，并把明文记在号池条目里（store 只存哈希）。
 */
export function bindGatewayKey(store, account, { label, limits = { rpm: 0, dailyRequests: 0, concurrent: 0 } } = {}) {
  const { plaintext, hash, prefix } = mintGatewaySecret();
  const publicKey = store.addKey({ label: label ?? `gabai:${account.email}`, hash, prefix, limits });
  return {
    id: publicKey.id,
    label: publicKey.label,
    prefix: publicKey.prefix,
    plaintext,
    createdAt: publicKey.createdAt,
  };
}

/**
 * HTTP 导入端点与 worker NDJSON 流共用的唯一写入口：合并账号、只在缺失时发 Key。
 * 幂等，所以崩溃后重推不会发两把 Key。
 */
export function importAccount({ pool, store, record, mintKey = true, label }) {
  const { account, created } = pool.add(record);
  let gatewayKey = account.gatewayKey ?? null;
  if (mintKey && !gatewayKey && store) {
    gatewayKey = bindGatewayKey(store, account, { label: label ?? `gabai:${account.email}` });
    pool.update(account.id, { gatewayKey });
  }
  return { account: pool.getAccount(account.id) ?? account, created, gatewayKey };
}

/** 脱敏视图：给管理接口 / Agent / 工具调用看的形状。 */
export function maskAccount(account) {
  if (!account) return null;
  const key = String(account.apiKey ?? "");
  return {
    id: account.id,
    email: account.email,
    apiKeyPreview: key ? `${key.slice(0, 8)}…${key.slice(-4)}` : "",
    apiKeyId: account.apiKeyId ?? "",
    apiKeyName: account.apiKeyName ?? "",
    apiBlocked: account.apiBlocked ?? "",
    plus: account.plus ?? null,
    emailVerified: account.emailVerified ?? null,
    status: account.status ?? "active",
    engine: account.engine ?? "protocol",
    createdAt: account.createdAt,
    updatedAt: account.updatedAt ?? null,
    requests: account.requests ?? 0,
    failures: account.failures ?? 0,
    healthy: account.healthy !== false,
    cooldownUntil: account.cooldownUntil ?? 0,
    lastError: account.lastError ?? null,
    gatewayKey: account.gatewayKey
      ? {
          id: account.gatewayKey.id,
          label: account.gatewayKey.label,
          prefix: account.gatewayKey.prefix,
          createdAt: account.gatewayKey.createdAt,
        }
      : null,
  };
}