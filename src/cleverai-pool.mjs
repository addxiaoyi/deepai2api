// Clever AI Hub 号池（account pool）— 独立号池，与 Allmodels / DeepAI 互不影响。
//
// 每个账号一条：Firebase uid、refreshToken（长期凭据）、App Check 令牌（会过期，
// 由转发层续期）、钱包额度、健康计数，以及网关签发的 `sk-dcg-…` Key。文件原子写入、
// 0600，因为里面是明文凭据。
//
// Schema: docs/CLEVERAI-POOL.md (`dcg.pool/cleverai@1`).
import { existsSync, mkdirSync, readFileSync, renameSync, writeFileSync } from "node:fs";
import { dirname, resolve } from "node:path";
import { randomUUID } from "node:crypto";

import { mintKey as mintGatewaySecret } from "./keys.mjs";

export const POOL_SCHEMA = "dcg.pool/cleverai@1";
export const POOL_VERSION = 1;
export const DEFAULT_BASE_URL = "https://cleverai.app";
export const DEFAULT_API_BASE = "https://api.cleverai.app";
export const DEFAULT_ENGINE = "openai-gpt-6-luna";

const clamp = (value, min, max) => Math.min(max, Math.max(min, Number(value) || 0));

export class CleveraiPool {
  /**
   * @param {string} path            pool file, normally `<dataDir>/cleverai-pool.json`
   * @param {{baseUrl?: string, apiBase?: string, engine?: string, log?: Function}} options
   */
  constructor(path, { baseUrl = DEFAULT_BASE_URL, apiBase = DEFAULT_API_BASE, engine = DEFAULT_ENGINE, log = () => {} } = {}) {
    this.path = resolve(path);
    this.baseUrl = baseUrl;
    this.apiBase = apiBase;
    this.engine = engine;
    this.log = log;
    this.state = this.#load();
    this.cursor = 0;
  }

  static at(dataDir, options) {
    return new CleveraiPool(resolve(dataDir, "cleverai-pool.json"), options);
  }

  #load() {
    const empty = { schema: POOL_SCHEMA, version: POOL_VERSION, provider: "cleverai", accounts: [] };
    if (!existsSync(this.path)) return empty;
    try {
      const parsed = JSON.parse(readFileSync(this.path, "utf8"));
      const accounts = Array.isArray(parsed.accounts) ? parsed.accounts.map(normalizeAccount) : [];
      return {
        schema: POOL_SCHEMA,
        version: POOL_VERSION,
        provider: "cleverai",
        baseUrl: parsed.baseUrl || this.baseUrl,
        apiBase: parsed.apiBase || this.apiBase,
        engine: parsed.engine || this.engine,
        updatedAt: parsed.updatedAt ?? null,
        accounts,
      };
    } catch (error) {
      this.log("warn", `cleverai: pool file unreadable (${error.message}), starting empty`);
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

  /** 现在能接流量的账号，按插入顺序。 */
  healthy(now = Date.now()) {
    return this.state.accounts.filter(
      (a) => a.status === "active" && a.healthy !== false && (a.cooldownUntil ?? 0) <= now,
    );
  }

  /** Round-robin；`exclude` 让重试落到别的账号。 */
  pick({ exclude = [], now = Date.now() } = {}) {
    const candidates = this.healthy(now).filter((a) => !exclude.includes(a.id));
    if (!candidates.length) return null;
    const picked = candidates[this.cursor % candidates.length];
    this.cursor = (this.cursor + 1) % Math.max(1, candidates.length);
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
      revoked: 0,
      keys: 0,
      verified: 0,
      credits: 0,
      requests: 0,
      updatedAt: this.state.updatedAt ?? null,
      last24h: 0,
      engines: {},
      placeholderReplies: 0,
    };
    const dayAgo = now - 24 * 3600 * 1000;
    for (const account of this.state.accounts) {
      if (account.status === "active") out.active++;
      if (account.status === "revoked") out.revoked++;
      if (account.healthy === false) out.unhealthy++;
      if ((account.cooldownUntil ?? 0) > now) out.cooling++;
      if (account.gatewayKey) out.keys++;
      if (account.emailVerified === true) out.verified++;
      out.credits += Number(account.wallet?.totalCredit ?? 0);
      out.requests += account.requests ?? 0;
      out.placeholderReplies += Number(account.notices?.total ?? 0);
      if (account.createdAt && Date.parse(account.createdAt) >= dayAgo) out.last24h++;
      const engine = account.engine ?? "protocol";
      out.engines[engine] = (out.engines[engine] ?? 0) + 1;
    }
    return out;
  }

  /* ---------------------------------------------------------------- writing */

  /**
   * 合并一条账号。去重顺序：refreshToken，然后 email。重复导入只刷新凭据，
   * 保留计数和已经发出去的网关 Key —— 幂等，重推不会发两把钥匙。
   */
  add(record) {
    const incoming = normalizeAccount(record);
    const existing =
      (incoming.refreshToken &&
        this.state.accounts.find((a) => a.refreshToken && a.refreshToken === incoming.refreshToken)) ||
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
    this.save();
    return true;
  }

  /** 一次上游调用的结果：健康、冷却、错误摘要。 */
  recordResult(id, { ok, error = null, cooldownMs = 0, now = Date.now() } = {}) {
    const account = this.getAccount(id);
    if (!account) return null;
    account.requests = (account.requests ?? 0) + 1;
    account.lastUsedAt = new Date(now).toISOString();
    if (ok) {
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

  /** 钱包变化（每轮对话后上游会回传新的 credit）。 */
  recordWallet(id, wallet) {
    const account = this.getAccount(id);
    if (!account || !wallet) return null;
    account.wallet = { ...(account.wallet ?? {}), ...wallet };
    account.updatedAt = new Date().toISOString();
    this.save();
    return account;
  }

  /**
   * 上游占位回答计数。
   *
   * 这些 engine 的模型层对普通账号只回套话（见 docs/CLEVERAI-POOL.md），转发层
   * 把每次命中记下来：一是给面板看，二是别把它当"模型答得好"混过去。它不触发
   * 冷却 —— 这不是账号的问题，换号也一样。
   */
  recordNotice(id, code) {
    const account = this.getAccount(id);
    if (!account) return null;
    const notices = account.notices ?? { total: 0, byCode: {} };
    notices.total += 1;
    notices.byCode[code] = (notices.byCode[code] ?? 0) + 1;
    notices.lastAt = new Date().toISOString();
    account.notices = notices;
    account.updatedAt = notices.lastAt;
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
        "uid",
        "refresh_token",
        "gateway_key",
        "engine",
        "status",
        "created_at",
      ];
      const escape = (value) => `"${String(value ?? "").replaceAll('"', '""')}"`;
      const body = rows.map((row) =>
        [
          row.email,
          row.password,
          row.uid,
          row.refreshToken,
          row.gatewayKey?.plaintext ?? "",
          row.engine,
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
      engine: this.engine,
      updatedAt: this.state.updatedAt,
    };
  }

  save() {
    this.state.updatedAt = new Date().toISOString();
    mkdirSync(dirname(this.path), { recursive: true });
    const tmp = `${this.path}.${randomUUID()}.tmp`;
    writeFileSync(tmp, JSON.stringify(this.toJSON(), null, 2), { mode: 0o600 });
    renameSync(tmp, this.path);
  }
}

/** 把 worker 推来的记录规范成池内形状。 */
export function normalizeAccount(raw = {}) {
  const email = String(raw.email ?? "").trim().toLowerCase();
  return {
    email,
    password: String(raw.password ?? ""),
    name: String(raw.name ?? ""),
    uid: String(raw.uid ?? raw.userId ?? raw.user_id ?? ""),
    idToken: String(raw.idToken ?? raw.id_token ?? ""),
    refreshToken: String(raw.refreshToken ?? raw.refresh_token ?? ""),
    appCheck: String(raw.appCheck ?? raw.app_check ?? ""),
    appCheckExp: clamp(raw.appCheckExp ?? raw.app_check_exp ?? 0, 0, Number.MAX_SAFE_INTEGER),
    wallet: raw.wallet && typeof raw.wallet === "object" ? raw.wallet : {},
    device: raw.device && typeof raw.device === "object" ? raw.device : {},
    notices: raw.notices && typeof raw.notices === "object" ? raw.notices : undefined,
    emailVerified: raw.emailVerified ?? raw.email_verified ?? null,
    status: raw.status ?? "active",
    engine: raw.engine ?? "protocol",
    createdAt: raw.createdAt ?? raw.created_at ?? new Date().toISOString(),
    gatewayKey: raw.gatewayKey ?? null,
    healthy: raw.healthy !== false,
    cooldownUntil: clamp(raw.cooldownUntil ?? 0, 0, Number.MAX_SAFE_INTEGER),
  };
}

/** 给一条池内账号发一把网关 Key，并把明文留在池里（Store 只存哈希）。 */
export function bindGatewayKey(store, account, { label, limits = { rpm: 0, dailyRequests: 0, concurrent: 0 } } = {}) {
  const { plaintext, hash, prefix } = mintGatewaySecret();
  const publicKey = store.addKey({ label: label ?? `cleverai:${account.email}`, hash, prefix, limits });
  return {
    id: publicKey.id,
    label: publicKey.label,
    prefix: publicKey.prefix,
    plaintext,
    createdAt: publicKey.createdAt,
  };
}

/**
 * 唯一写入口径：合并账号 + 只发一次网关 Key。HTTP 导入和 worker 事件流共用。
 */
export function importAccount({ pool, store, record, mintKey = true, label }) {
  const { account, created } = pool.add(record);
  let gatewayKey = account.gatewayKey ?? null;
  if (mintKey && !gatewayKey && store) {
    gatewayKey = bindGatewayKey(store, account, { label: label ?? `cleverai:${account.email}` });
    pool.update(account.id, { gatewayKey });
  }
  return { account: pool.getAccount(account.id) ?? account, created, gatewayKey };
}

/** 管理接口的脱敏视图。 */
export function maskAccount(account) {
  const preview = (value) => (value ? `${String(value).slice(0, 10)}…${String(value).slice(-4)}` : "");
  return {
    id: account.id,
    email: account.email,
    name: account.name,
    uid: account.uid,
    engine: account.engine,
    status: account.status,
    healthy: account.healthy !== false,
    cooldownUntil: account.cooldownUntil ?? 0,
    emailVerified: account.emailVerified,
    wallet: account.wallet ?? {},
    refreshTokenPreview: preview(account.refreshToken),
    appCheckExp: account.appCheckExp ?? 0,
    gatewayKey: account.gatewayKey
      ? { id: account.gatewayKey.id, label: account.gatewayKey.label, prefix: account.gatewayKey.prefix }
      : null,
    requests: account.requests ?? 0,
    failures: account.failures ?? 0,
    lastError: account.lastError ?? null,
    lastUsedAt: account.lastUsedAt ?? null,
    createdAt: account.createdAt,
    updatedAt: account.updatedAt,
  };
}
