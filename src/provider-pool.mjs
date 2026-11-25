// 通用 provider 号池 —— 每个 provider 一份独立文件、独立 schema、独立锁。
//
// 与 `allmodels-pool.mjs` 同构，但参数化了 provider：
//   data/<provider>-pool.json     ← 号码池（明文凭据，0600，原子写）
//   data/<provider>-register.lock ← 跨进程注册互斥锁（网关 / MCP / CLI 共用）
//
// 关键点：**号池文件按 provider 物理隔离**，所以新增 wavespeed / novita
// 不会读写 allmodels-pool.json，两边可以同时跑、互不覆盖。
//
// Schema: dcg.pool/<provider>@1
import { existsSync, mkdirSync, readFileSync, renameSync, statSync, writeFileSync } from "node:fs";
import { dirname, resolve } from "node:path";
import { randomUUID } from "node:crypto";

import { mintKey as mintGatewaySecret } from "./keys.mjs";
import { isNewerThan, withFileLock } from "./lock.mjs";
import { resolveEndpoints } from "./provider-catalog.mjs";

const clamp = (value, min, max) => Math.min(max, Math.max(min, Number(value) || 0));

export function poolSchemaFor(providerId) {
  return `dcg.pool/${providerId}@1`;
}

export class ProviderPool {
  /**
   * @param {string} path              号池文件绝对路径
   * @param {{provider: object, apiBase?: string, log?: Function}} options
   */
  constructor(path, { provider, apiBase = "", log = () => {} } = {}) {
    this.path = resolve(path);
    this.provider = provider;
    this.log = log;
    const endpoints = resolveEndpoints(provider);
    this.apiBase = apiBase || endpoints.apiBase;
    this.panelBase = endpoints.panelBase;
    this.state = this.#load();
    this.cursor = 0;
    this.writtenAt = mtimeOf(this.path);
    this.deletedIds = new Set();
    this.touchedIds = new Set();
  }

  /** 在 dataDir 下按 provider 定位号池文件。 */
  static at(dataDir, provider, options = {}) {
    return new ProviderPool(resolve(dataDir, provider.poolFile), { provider, ...options });
  }

  get schema() {
    return poolSchemaFor(this.provider.id);
  }

  #load() {
    const empty = {
      schema: this.schema,
      version: 1,
      provider: this.provider.id,
      accounts: [],
    };
    if (!existsSync(this.path)) return empty;
    try {
      const parsed = JSON.parse(readFileSync(this.path, "utf8"));
      const accounts = Array.isArray(parsed.accounts) ? parsed.accounts.map(normalizeAccount) : [];
      return {
        schema: this.schema,
        version: 1,
        provider: this.provider.id,
        panelBase: parsed.panelBase || this.panelBase,
        apiBase: parsed.apiBase || this.apiBase,
        updatedAt: parsed.updatedAt ?? null,
        accounts,
      };
    } catch (error) {
      this.log("warn", `${this.provider.id}: pool file unreadable (${error.message}), starting empty`);
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

  healthy(now = Date.now()) {
    return this.state.accounts.filter(
      (a) => a.status === "active" && a.healthy !== false && (a.cooldownUntil ?? 0) <= now,
    );
  }

  /** Round-robin：轮询顺序即插入顺序，跳过 exclude 里已试过的。 */
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
      schema: this.schema,
      provider: this.provider.id,
      total: this.state.accounts.length,
      active: 0,
      cooling: 0,
      unhealthy: 0,
      revoked: 0,
      keys: 0,
      extraKeys: 0,
      requests: 0,
      updatedAt: this.state.updatedAt ?? null,
      last24h: 0,
    };
    const dayAgo = now - 24 * 3600 * 1000;
    for (const account of this.state.accounts) {
      if (account.status === "active") out.active++;
      if (account.status === "revoked") out.revoked++;
      if (account.healthy === false) out.unhealthy++;
      if ((account.cooldownUntil ?? 0) > now) out.cooling++;
      if (account.apiKey) out.keys++;
      out.extraKeys += Array.isArray(account.extraKeys) ? account.extraKeys.length : 0;
      out.requests += account.requests ?? 0;
      if (account.createdAt && Date.parse(account.createdAt) >= dayAgo) out.last24h++;
    }
    return out;
  }

  /** 从磁盘重读（worker / 另一个进程可能刚写过）。合并时保留本进程的改动。 */
  reload() {
    const disk = readPoolFile(this.path, this.provider.id);
    if (!disk) return this.state;
    this.state.accounts = mergePoolAccounts(this.state.accounts, disk.accounts);
    this.state.updatedAt = new Date().toISOString();
    this.writtenAt = mtimeOf(this.path);
    return this.state;
  }

  /* ---------------------------------------------------------------- writing */

  /**
   * 合并一个账号。按 `apiKey` 再按 `email` 去重；重复导入刷新凭据，
   * 但保留 id / createdAt / 使用计数 / 已发的网关 Key。
   */
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
      const tokens = {
        accessToken: existing.accessToken ?? "",
        refreshToken: existing.refreshToken ?? "",
        accessTokenExpiresAt: existing.accessTokenExpiresAt ?? 0,
      };
      Object.assign(existing, incoming, keep, {
        id: existing.id,
        createdAt: keep.createdAt ?? incoming.createdAt ?? now,
        updatedAt: now,
        status:
          incoming.status === "revoked"
            ? "revoked"
            : existing.status === "revoked"
              ? "revoked"
              : incoming.status,
      });
      // worker 重推时可能只带 apiKey：别用空令牌把号池里有效的会话抹掉。
      if (!incoming.accessToken && tokens.accessToken) {
        existing.accessToken = tokens.accessToken;
        existing.refreshToken = tokens.refreshToken;
        existing.accessTokenExpiresAt = tokens.accessTokenExpiresAt;
      }
      this.touchedIds.add(existing.id);
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
    this.touchedIds.add(created.id);
    this.state.accounts.push(created);
    this.save();
    return { account: created, created: true };
  }

  update(id, patch) {
    const account = this.getAccount(id);
    if (!account) return null;
    Object.assign(account, patch, { updatedAt: new Date().toISOString() });
    this.touchedIds.add(id);
    this.save();
    return account;
  }

  remove(id) {
    const before = this.state.accounts.length;
    this.state.accounts = this.state.accounts.filter((a) => a.id !== id);
    if (this.state.accounts.length === before) return false;
    this.deletedIds.add(id);
    this.save();
    return true;
  }

  /** 记一次上游调用结果：成功清冷却，失败累计并进入冷却。 */
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

  exportTo(file, { format = "jsonl" } = {}) {
    const target = resolve(file);
    mkdirSync(dirname(target), { recursive: true });
    const rows = this.records();
    if (format === "csv") {
      const header = [
        "provider",
        "email",
        "password",
        "api_key",
        "key_prefix",
        "key_last4",
        "expire_time",
        "engine",
        "status",
        "created_at",
      ];
      const escape = (value) => `"${String(value ?? "").replaceAll('"', '""')}"`;
      const body = rows.map((row) =>
        [
          this.provider.id,
          row.email,
          row.password,
          row.apiKey,
          row.keyPrefix,
          row.keyLast4,
          row.expireTime,
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
      schema: this.schema,
      provider: this.provider.id,
      panelBase: this.panelBase,
      apiBase: this.apiBase,
      updatedAt: this.state.updatedAt,
    };
  }

  /** 原子落盘；跨进程冲突时先合并对方的条目再写。 */
  save() {
    this.state.updatedAt = new Date().toISOString();
    mkdirSync(dirname(this.path), { recursive: true });
    const locked = withFileLock(this.path, () => {
      if (isNewerThan(this.path, this.writtenAt)) {
        const disk = readPoolFile(this.path, this.provider.id);
        if (disk) {
          this.state.accounts = mergePoolAccounts(
            this.state.accounts,
            disk.accounts,
            this.deletedIds,
            this.touchedIds,
          );
          if (disk.apiBase) this.apiBase = disk.apiBase || this.apiBase;
        }
      }
      this.deletedIds.clear();
      this.touchedIds.clear();
      const tmp = `${this.path}.${randomUUID()}.tmp`;
      writeFileSync(tmp, JSON.stringify(this.toJSON(), null, 2), { mode: 0o600 });
      renameSync(tmp, this.path);
      this.writtenAt = Date.now();
    });
    if (!locked.ok && locked.reason === "error") throw locked.error;
  }
}

function mtimeOf(path) {
  try {
    return statSync(path).mtimeMs;
  } catch {
    return 0;
  }
}

function readPoolFile(path, providerId) {
  try {
    if (!existsSync(path)) return null;
    const parsed = JSON.parse(readFileSync(path, "utf8"));
    // 别人的号池文件（不同 provider）一律不读，避免串号。
    if (parsed.provider && parsed.provider !== providerId) return null;
    return {
      apiBase: parsed.apiBase,
      accounts: Array.isArray(parsed.accounts) ? parsed.accounts.map(normalizeAccount) : [],
    };
  } catch {
    return null;
  }
}

/**
 * 并集合并：计数取 max（用量单调），可变字段取 updatedAt 更新的那个，
 * 已发的 gatewayKey 永不丢，显式删除优先，同 email / 同 key 折叠成一条。
 *
 * `touchedIds` 保留在签名里只为兼容旧调用方；合并语义不再依赖它
 * （依赖它会导致"磁盘快照较旧"时丢掉本地新账号）。
 */
export function mergePoolAccounts(localAccounts, diskAccounts, deletedIds = new Set(), touchedIds = null) {
  const byId = new Map();
  for (const account of diskAccounts) byId.set(account.id, account);

  for (const account of localAccounts) {
    const other = byId.get(account.id);
    if (!other) {
      // 磁盘快照里没有这条：要么是本进程刚建的，要么磁盘那份比本地旧。
      // 两种情况都必须保留本地条目 —— 曾经用 `touchedIds` 过滤，结果
      // "另一次 save 先落了盘" 会把刚注册成功的账号从池里抹掉（真丢号）。
      // 跨进程删除由 `deletedIds` 负责，不靠"不存在于磁盘"来推断删除。
      byId.set(account.id, account);
      continue;
    }
    const newer = (account.updatedAt ?? "") >= (other.updatedAt ?? "") ? account : other;
    byId.set(account.id, {
      ...other,
      ...newer,
      id: account.id,
      createdAt: other.createdAt ?? account.createdAt,
      requests: Math.max(account.requests ?? 0, other.requests ?? 0),
      failures: Math.max(account.failures ?? 0, other.failures ?? 0),
      cooldownUntil: Math.max(account.cooldownUntil ?? 0, other.cooldownUntil ?? 0),
      healthy: newer.healthy !== false,
      gatewayKey: account.gatewayKey ?? other.gatewayKey ?? null,
    });
  }
  for (const id of deletedIds) byId.delete(id);

  const seen = new Map();
  for (const account of byId.values()) {
    const dedupeKey = account.apiKey || account.email;
    const existing = seen.get(dedupeKey);
    if (!existing) {
      seen.set(dedupeKey, account);
      continue;
    }
    const winner = (account.updatedAt ?? "") >= (existing.updatedAt ?? "") ? account : existing;
    const loser = winner === account ? existing : account;
    seen.set(dedupeKey, {
      ...loser,
      ...winner,
      requests: Math.max(account.requests ?? 0, existing.requests ?? 0),
      failures: Math.max(account.failures ?? 0, existing.failures ?? 0),
      gatewayKey: winner.gatewayKey ?? loser.gatewayKey ?? null,
    });
  }
  return [...seen.values()];
}

/** 把 worker 推来的任意形状归一成号池条目。 */
export function normalizeAccount(raw = {}) {
  const apiKey = String(raw.apiKey ?? raw.api_key ?? "").trim();
  const email = String(raw.email ?? "").trim().toLowerCase();
  return {
    email,
    password: String(raw.password ?? ""),
    name: String(raw.name ?? ""),
    apiKey,
    keyId: String(raw.keyId ?? raw.key_id ?? ""),
    keyPrefix: String(raw.keyPrefix ?? raw.key_prefix ?? ""),
    keyLast4: String(raw.keyLast4 ?? raw.key_last4 ?? ""),
    keySource: String(raw.keySource ?? raw.api_key_source ?? ""),
    extraKeys: Array.isArray(raw.extraKeys ?? raw.extra_keys) ? raw.extraKeys ?? raw.extra_keys : [],
    expireTime: String(raw.expireTime ?? raw.expire_time ?? ""),
    organizationId: String(raw.organizationId ?? raw.organization_id ?? ""),
    // moonai 这类"用会话令牌而不是面板 Key 调模型"的 provider 需要这三项：
    // access_token 是上游鉴权凭据，refresh_token 用来续期，过期时间用于提前续。
    accessToken: String(raw.accessToken ?? raw.access_token ?? ""),
    refreshToken: String(raw.refreshToken ?? raw.refresh_token ?? ""),
    accessTokenExpiresAt: Number(raw.accessTokenExpiresAt ?? raw.access_token_expires_at ?? 0) || 0,
    emailVerified: raw.emailVerified ?? raw.email_verified ?? null,
    status: raw.status ?? "active",
    engine: raw.engine ?? "protocol",
    domain: String(raw.domain ?? ""),
    proxy: String(raw.proxy ?? ""),
    note: String(raw.note ?? ""),
    createdAt: raw.createdAt ?? raw.created_at ?? new Date().toISOString(),
    cookies: Array.isArray(raw.cookies) ? raw.cookies : [],
    gatewayKey: raw.gatewayKey ?? null,
    healthy: raw.healthy !== false,
    cooldownUntil: clamp(raw.cooldownUntil ?? 0, 0, Number.MAX_SAFE_INTEGER),
  };
}

/** 给一个号池账号签发网关 Key（`sk-dcg-…`），明文只存在号池文件里。 */
export function bindGatewayKey(store, account, { label, limits = { rpm: 0, dailyRequests: 0, concurrent: 0 } } = {}) {
  const { plaintext, hash, prefix } = mintGatewaySecret();
  const publicKey = store.addKey({ label: label ?? `${account.provider ?? "provider"}:${account.email}`, hash, prefix, limits });
  return {
    id: publicKey.id,
    label: publicKey.label,
    prefix: publicKey.prefix,
    plaintext,
    createdAt: publicKey.createdAt,
  };
}

/**
 * 唯一的合并入口：HTTP 导入端点与 worker 的 NDJSON 流都走这里。
 * 幂等：重推同一个账号不会重复发 Key。
 */
export function importAccount({ pool, store, record, mintKey = true, label }) {
  const { account, created } = pool.add(record);
  let gatewayKey = account.gatewayKey ?? null;
  if (mintKey && !gatewayKey && store) {
    gatewayKey = bindGatewayKey(store, account, { label: label ?? `${pool.provider.id}:${account.email}` });
    pool.update(account.id, { gatewayKey });
  }
  return { account: pool.getAccount(account.id) ?? account, created, gatewayKey };
}

/** 脱敏视图：agent / 管理 API / 工具调用能看到的形状。 */
export function maskAccount(account) {
  if (!account) return null;
  const key = String(account.apiKey ?? "");
  return {
    id: account.id,
    email: account.email,
    name: account.name ?? "",
    apiKeyPreview: key ? `${key.slice(0, 8)}…${key.slice(-4)}` : "",
    keyId: account.keyId ?? "",
    keyPrefix: account.keyPrefix ?? "",
    keyLast4: account.keyLast4 ?? "",
    keySource: account.keySource ?? "",
    extraKeys: Array.isArray(account.extraKeys) ? account.extraKeys.length : 0,
    expireTime: account.expireTime ?? "",
    organizationId: account.organizationId ?? "",
    // 会话令牌只报"有没有 / 什么时候过期"，明文永远不出去
    accessTokenPreview: account.accessToken ? `…${String(account.accessToken).slice(-6)}` : "",
    accessTokenExpiresAt: account.accessTokenExpiresAt ?? 0,
    hasRefreshToken: Boolean(account.refreshToken),
    emailVerified: account.emailVerified ?? null,
    status: account.status ?? "active",
    engine: account.engine ?? "protocol",
    domain: account.domain ?? "",
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
