// jev-ai.org 号池：账号 + `sk-glm5-…` Key。
//
// 这一条线的成品是**模型 API Key**（基址 https://jev-ai.org/api/v1），所以除了
// 池文件还有一份"给别的工具用"的导出：`data/keys/jevai.txt` / `jevai.json`。
// 下游既可以直接拿 Key 打上游，也可以走网关的 `/jevai/v1/*`（见 src/jevai-relay.mjs）。
import { existsSync, mkdirSync, readFileSync, renameSync, writeFileSync } from "node:fs";
import { dirname, resolve } from "node:path";
import { randomUUID } from "node:crypto";

import { mintKey as mintGatewaySecret } from "./keys.mjs";

export const POOL_SCHEMA = "dcg.pool/jevai@1";
export const POOL_VERSION = 1;
export const DEFAULT_BASE_URL = "https://jev-ai.org";
export const DEFAULT_API_BASE = "https://jev-ai.org/api/v1";
export const KEY_PREFIX = "sk-glm5-";

const clamp = (value, min, max) => Math.min(max, Math.max(min, Number(value) || 0));

export class JevaiPool {
  /**
   * @param {string} path pool file, normally `<dataDir>/jevai-pool.json`
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
    return new JevaiPool(resolve(dataDir, "jevai-pool.json"), options);
  }

  #load() {
    const empty = { schema: POOL_SCHEMA, version: POOL_VERSION, provider: "jevai", accounts: [] };
    if (!existsSync(this.path)) return empty;
    try {
      const parsed = JSON.parse(readFileSync(this.path, "utf8"));
      const accounts = Array.isArray(parsed.accounts) ? parsed.accounts.map(normalizeAccount) : [];
      return {
        schema: POOL_SCHEMA,
        version: POOL_VERSION,
        provider: "jevai",
        baseUrl: parsed.baseUrl || this.baseUrl,
        apiBase: parsed.apiBase || this.apiBase,
        updatedAt: parsed.updatedAt ?? null,
        accounts,
      };
    } catch (error) {
      this.log("warn", `jevai: pool file unreadable (${error.message}), starting empty`);
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

  byKey(apiKey) {
    const needle = String(apiKey ?? "").trim();
    if (!needle) return null;
    return this.state.accounts.find((a) => a.apiKey === needle) ?? null;
  }

  /** 只有拿到 Key 且没进冷却的账号才能接流量。 */
  healthy(now = Date.now()) {
    return this.state.accounts.filter(
      (a) => a.status === "active" && a.apiKey && a.healthy !== false && (a.cooldownUntil ?? 0) <= now,
    );
  }

  pick({ exclude = [], now = Date.now() } = {}) {
    const candidates = this.healthy(now).filter((a) => !exclude.includes(a.id));
    if (!candidates.length) return null;
    const picked = candidates[this.cursor % candidates.length];
    this.cursor = (this.cursor + 1) % Math.max(1, candidates.length);
    return picked;
  }

  keys() {
    return this.state.accounts.map((a) => a.apiKey).filter(Boolean);
  }

  stats() {
    const now = Date.now();
    const out = {
      schema: POOL_SCHEMA,
      total: this.state.accounts.length,
      active: 0,
      keys: 0,
      verified: 0,
      cooling: 0,
      unhealthy: 0,
      requests: 0,
      failures: 0,
      updatedAt: this.state.updatedAt ?? null,
      last24h: 0,
    };
    const dayAgo = now - 24 * 3600 * 1000;
    for (const account of this.state.accounts) {
      if (account.status === "active") out.active++;
      if (account.apiKey) out.keys++;
      if (account.emailVerified === true) out.verified++;
      if ((account.cooldownUntil ?? 0) > now) out.cooling++;
      if (account.healthy === false) out.unhealthy++;
      out.requests += account.requests ?? 0;
      out.failures += account.failures ?? 0;
      if (account.createdAt && Date.parse(account.createdAt) >= dayAgo) out.last24h++;
    }
    return out;
  }

  /* ---------------------------------------------------------------- writing */

  add(record) {
    const incoming = normalizeAccount(record);
    const now = new Date().toISOString();
    const existing =
      (incoming.apiKey && this.byKey(incoming.apiKey)) ||
      (incoming.email ? this.byEmail(incoming.email) : null) ||
      null;

    if (existing) {
      const keep = {
        id: existing.id,
        createdAt: existing.createdAt,
        requests: existing.requests ?? 0,
        failures: existing.failures ?? 0,
        gatewayKey: existing.gatewayKey ?? null,
      };
      Object.assign(existing, incoming, keep, {
        id: existing.id,
        createdAt: keep.createdAt ?? incoming.createdAt ?? now,
        updatedAt: now,
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

  /* ---------------------------------------------------------------- exports */

  records() {
    return this.state.accounts.map((account) => ({ ...account }));
  }

  /** 给别的工具用的 Key 清单（一行一条）。 */
  exportKeys(file = "") {
    const rows = this.keys();
    const target = resolve(file || "data/keys/jevai.txt");
    mkdirSync(dirname(target), { recursive: true });
    writeFileSync(target, rows.length ? rows.join("\n") + "\n" : "", { mode: 0o600 });
    return { file: target, rows: rows.length };
  }

  /**
   * 自动接入：把 Key + 基址写成一份描述（`data/keys/jevai.json`），
   * 其它工具（Open WebUI 连接、自建客户端、provider-cli 导入）直接读它。
   */
  exportConnection(file = "") {
    const target = resolve(file || "data/keys/jevai.json");
    mkdirSync(dirname(target), { recursive: true });
    const body = {
      provider: "jevai",
      openai_base: this.apiBase,
      key_prefix: KEY_PREFIX,
      keys: this.keys(),
      updated_at: new Date().toISOString(),
    };
    writeFileSync(target, JSON.stringify(body, null, 2), { mode: 0o600 });
    return { file: target, keys: body.keys.length, openai_base: this.apiBase };
  }

  exportTo(file, { format = "jsonl" } = {}) {
    const target = resolve(file);
    mkdirSync(dirname(target), { recursive: true });
    const rows = this.records();
    if (format === "keys") {
      const body = this.keys().join("\n");
      writeFileSync(target, body ? body + "\n" : "", { mode: 0o600 });
    } else if (format === "csv") {
      const header = ["email", "name", "api_key", "email_verified", "engine", "created_at"];
      const escape = (value) => `"${String(value ?? "").replaceAll('"', '""')}"`;
      const body = rows.map((row) =>
        [row.email, row.name, row.apiKey, row.emailVerified, row.engine, row.createdAt].map(escape).join(","),
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
    return { ...this.state, baseUrl: this.baseUrl, apiBase: this.apiBase, updatedAt: this.state.updatedAt };
  }

  save() {
    this.state.updatedAt = new Date().toISOString();
    mkdirSync(dirname(this.path), { recursive: true });
    const tmp = `${this.path}.${randomUUID()}.tmp`;
    writeFileSync(tmp, JSON.stringify(this.toJSON(), null, 2), { mode: 0o600 });
    renameSync(tmp, this.path);
  }
}

export function normalizeAccount(raw = {}) {
  const email = String(raw.email ?? "").trim().toLowerCase();
  const apiKey = String(raw.apiKey ?? raw.api_key ?? "").trim();
  return {
    email,
    password: String(raw.password ?? ""),
    name: String(raw.name ?? ""),
    apiKey,
    keyPrefix: String(raw.keyPrefix ?? raw.key_prefix ?? KEY_PREFIX),
    userId: String(raw.userId ?? raw.user_id ?? ""),
    emailVerified: raw.emailVerified ?? raw.email_verified ?? null,
    credits: raw.credits && typeof raw.credits === "object" ? raw.credits : {},
    status: raw.status ?? "active",
    engine: raw.engine ?? "protocol",
    createdAt: raw.createdAt ?? raw.created_at ?? new Date().toISOString(),
    gatewayKey: raw.gatewayKey ?? null,
    healthy: raw.healthy !== false,
    cooldownUntil: clamp(raw.cooldownUntil ?? 0, 0, Number.MAX_SAFE_INTEGER),
  };
}

/** 给一条账号发一把网关 Key（Store 只存哈希，明文留在池里）。 */
export function bindGatewayKey(store, account, { label, limits = { rpm: 0, dailyRequests: 0, concurrent: 0 } } = {}) {
  const { plaintext, hash, prefix } = mintGatewaySecret();
  const publicKey = store.addKey({ label: label ?? `jevai:${account.email}`, hash, prefix, limits });
  return { id: publicKey.id, label: publicKey.label, prefix: publicKey.prefix, plaintext, createdAt: publicKey.createdAt };
}

/** 唯一写入口径：合并账号 + 只发一次网关 Key。 */
export function importAccount({ pool, store, record, mintKey = true, label }) {
  const { account, created } = pool.add(record);
  let gatewayKey = account.gatewayKey ?? null;
  if (mintKey && !gatewayKey && store) {
    gatewayKey = bindGatewayKey(store, account, { label: label ?? `jevai:${account.email}` });
    pool.update(account.id, { gatewayKey });
  }
  return { account: pool.getAccount(account.id) ?? account, created, gatewayKey };
}

export function maskAccount(account) {
  const preview = (value) => (value ? `${String(value).slice(0, 10)}…${String(value).slice(-4)}` : "");
  return {
    id: account.id,
    email: account.email,
    name: account.name,
    apiKeyPreview: preview(account.apiKey),
    keyPrefix: account.keyPrefix,
    userId: account.userId,
    emailVerified: account.emailVerified,
    credits: account.credits ?? {},
    status: account.status,
    engine: account.engine,
    healthy: account.healthy !== false,
    cooldownUntil: account.cooldownUntil ?? 0,
    requests: account.requests ?? 0,
    failures: account.failures ?? 0,
    lastError: account.lastError ?? null,
    gatewayKey: account.gatewayKey
      ? { id: account.gatewayKey.id, label: account.gatewayKey.label, prefix: account.gatewayKey.prefix }
      : null,
    createdAt: account.createdAt,
    updatedAt: account.updatedAt,
  };
}
