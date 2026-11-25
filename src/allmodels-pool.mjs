// Allmodels 号池（account pool）— the file format every consumer reads.
//
// One file holds every registered dashboard account together with the API key it
// was minted, the workspace ids it belongs to, its health counters and the
// gateway key (`sk-dcg-…`) that was issued for it. It is written atomically and
// mode 0600 because it contains plaintext credentials.
//
// Schema: docs/ALLMODELS-POOL.md (`dcg.pool/allmodels@1`).
import { existsSync, mkdirSync, readFileSync, renameSync, statSync, writeFileSync } from "node:fs";
import { dirname, resolve } from "node:path";
import { createHash, randomUUID } from "node:crypto";

import { mintKey as mintGatewaySecret } from "./keys.mjs";
import { withFileLock } from "./lock.mjs";

export const POOL_SCHEMA = "dcg.pool/allmodels@1";
export const POOL_VERSION = 1;
export const DEFAULT_BASE_URL = "https://dash.allmodels.io";
export const DEFAULT_API_BASE = "https://api.allmodels.io/v1";

const clamp = (value, min, max) => Math.min(max, Math.max(min, Number(value) || 0));

export class AllmodelsPool {
  /**
   * @param {string} path            pool file, normally `<dataDir>/allmodels-pool.json`
   * @param {{baseUrl?: string, apiBase?: string, log?: Function}} options
   */
  constructor(path, { baseUrl = DEFAULT_BASE_URL, apiBase = DEFAULT_API_BASE, log = () => {} } = {}) {
    this.path = resolve(path);
    this.baseUrl = baseUrl;
    this.apiBase = apiBase;
    this.log = log;
    this.state = this.#load();
    this.cursor = 0;
    // 网关、MCP server、CLI 可能同时持有一份号池：记住自己的写入时间，
    // 落盘前若发现别人写过，就先合并再写，避免互相覆盖。
    this.writtenAt = mtimeOf(this.path);
    this.writtenHash = hashFile(this.path);
    this.deletedIds = new Set();
    // ids this process created/changed since the last save; local-only entries
    // outside this set were removed by the other process.
    this.ownedIds = new Set();
  }

  static at(dataDir, options) {
    return new AllmodelsPool(resolve(dataDir, "allmodels-pool.json"), options);
  }

  #load() {
    const empty = { schema: POOL_SCHEMA, version: POOL_VERSION, provider: "allmodels", accounts: [], tombstones: [] };
    if (!existsSync(this.path)) return empty;
    try {
      const parsed = JSON.parse(readFileSync(this.path, "utf8"));
      const accounts = Array.isArray(parsed.accounts) ? parsed.accounts.map(normalizeAccount) : [];
      return {
        schema: POOL_SCHEMA,
        version: POOL_VERSION,
        provider: "allmodels",
        baseUrl: parsed.baseUrl || this.baseUrl,
        apiBase: parsed.apiBase || this.apiBase,
        updatedAt: parsed.updatedAt ?? null,
        accounts,
        tombstones: Array.isArray(parsed.tombstones) ? parsed.tombstones : [],
      };
    } catch (error) {
      this.log("warn", `allmodels: pool file unreadable (${error.message}), starting empty`);
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

  /** Accounts that can take traffic right now, in insertion order. */
  healthy(now = Date.now()) {
    return this.state.accounts.filter(
      (a) => a.status === "active" && a.healthy !== false && (a.cooldownUntil ?? 0) <= now,
    );
  }

  /**
   * Round-robin pick over healthy accounts. `exclude` skips ids already tried in
   * the current request so a retry lands on a different account.
   */
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

  /* ---------------------------------------------------------------- writing */

  /**
   * Merge one account. Dedupe order: `apiKey`, then `email`. Re-imports refresh
   * the credentials but keep the usage counters and any gateway key already issued.
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
      Object.assign(existing, incoming, keep, {
        id: existing.id,
        createdAt: keep.createdAt ?? incoming.createdAt ?? now,
        updatedAt: now,
        status: incoming.status === "revoked" ? "revoked" : existing.status === "revoked" ? "revoked" : incoming.status,
      });
      this.ownedIds.add(existing.id);
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
    this.ownedIds.add(created.id);
    this.state.accounts.push(created);
    this.save();
    return { account: created, created: true };
  }

  /** Fold in accounts another process wrote (no write of our own). */
  reload() {
    const disk = readPoolFile(this.path);
    if (!disk) return false;
    this.state.accounts = mergePoolAccounts(this.state.accounts, disk.accounts, this.deletedIds, this.ownedIds);
    this.state.tombstones = mergeTombstones(this.state.tombstones, disk.tombstones);
    this.applyTombstones();
    return true;
  }

  /** Drop entries whose delete was recorded (they must never come back). */
  applyTombstones() {
    const dead = new Set((this.state.tombstones ?? []).map((row) => row.id));
    if (!dead.size) return;
    this.state.accounts = this.state.accounts.filter((account) => !dead.has(account.id));
  }

  update(id, patch) {
    const account = this.getAccount(id);
    if (!account) return null;
    Object.assign(account, patch, { updatedAt: new Date().toISOString() });
    this.ownedIds.add(id);
    this.save();
    return account;
  }

  remove(id) {
    const before = this.state.accounts.length;
    this.state.accounts = this.state.accounts.filter((a) => a.id !== id);
    if (this.state.accounts.length === before) return false;
    this.deletedIds.add(id);
    this.state.tombstones ??= [];
    if (!this.state.tombstones.some((row) => row.id === id)) this.state.tombstones.push({ id, at: Date.now() });
    if (this.state.tombstones.length > 500) this.state.tombstones.splice(0, this.state.tombstones.length - 500);
    this.save();
    return true;
  }

  /** Record the outcome of an upstream call against one pooled key. */
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

  /** Plain records, secrets included, in schema order. */
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
        "key_prefix",
        "project_id",
        "guardrail_id",
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
          row.keyPrefix,
          row.projectId,
          row.guardrailId,
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

  /** Persist immediately: structural changes must survive a crash. */
  save() {
    this.state.updatedAt = new Date().toISOString();
    mkdirSync(dirname(this.path), { recursive: true });
    const locked = withFileLock(this.path, () => {
      if (hashFile(this.path) !== this.writtenHash) {
        const disk = readPoolFile(this.path);
        if (disk) {
          this.state.accounts = mergePoolAccounts(
            this.state.accounts,
            disk.accounts,
            this.deletedIds,
            this.ownedIds,
          );
          this.state.tombstones = mergeTombstones(this.state.tombstones, disk.tombstones);
          if (disk.baseUrl) this.baseUrl = this.baseUrl || disk.baseUrl;
        }
      }
      this.deletedIds.clear();
      this.applyTombstones();
      // ownedIds stays cumulative: a stale concurrent writer may drop our entries,
      // and the next merge must recognise them as ours instead of as deletions.
      const tmp = `${this.path}.${randomUUID()}.tmp`;
      const bytes = JSON.stringify(this.toJSON(), null, 2);
      writeFileSync(tmp, bytes, { mode: 0o600 });
      renameSync(tmp, this.path);
      this.writtenAt = Date.now();
      this.writtenHash = createHash("sha1").update(bytes).digest("hex");
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

function hashFile(path) {
  try {
    return createHash("sha1").update(readFileSync(path)).digest("hex");
  } catch {
    return "";
  }
}

function readPoolFile(path) {
  try {
    if (!existsSync(path)) return null;
    const parsed = JSON.parse(readFileSync(path, "utf8"));
    return {
      baseUrl: parsed.baseUrl,
      apiBase: parsed.apiBase,
      accounts: Array.isArray(parsed.accounts) ? parsed.accounts.map(normalizeAccount) : [],
      tombstones: Array.isArray(parsed.tombstones) ? parsed.tombstones : [],
    };
  } catch {
    return null;
  }
}

/** Union two tombstone lists; deletes are permanent for 30 days / 500 entries. */
export function mergeTombstones(local = [], disk = []) {
  const seen = new Map();
  for (const row of [...(disk ?? []), ...(local ?? [])]) {
    if (!row?.id) continue;
    const at = Number(row.at ?? 0);
    seen.set(row.id, { id: row.id, at: Math.max(at, seen.get(row.id)?.at ?? 0) });
  }
  const cutoff = Date.now() - 30 * 24 * 3600 * 1000;
  return [...seen.values()].filter((row) => row.at >= cutoff).slice(-500);
}

/**
 * Union the on-disk account list with ours.
 *
 * Rules: counters take the max (usage is monotonic), the newer `updatedAt` wins
 * for mutable fields, an issued `gatewayKey` is never dropped, explicit deletes
 * win, and an account that both processes created under different ids collapses
 * into one (same email or same panel key).
 */
export function mergePoolAccounts(localAccounts, diskAccounts, deletedIds = new Set(), ownedIds = null) {
  const byId = new Map();
  for (const account of diskAccounts) byId.set(account.id, account);

  for (const account of localAccounts) {
    const other = byId.get(account.id);
    if (!other) {
      // Local-only: keep it only if this process created/updated it, otherwise
      // the other process removed it and it must stay removed.
      if (!ownedIds || ownedIds.has(account.id)) byId.set(account.id, account);
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

  // Collapse accidental duplicates created concurrently (same email / same key).
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

/** Normalise whatever the worker pushed into the canonical entry shape. */
export function normalizeAccount(raw = {}) {
  const apiKey = String(raw.apiKey ?? raw.api_key ?? "").trim();
  const email = String(raw.email ?? "").trim().toLowerCase();
  return {
    id: typeof raw.id === "string" ? raw.id : "",
    email,
    password: String(raw.password ?? ""),
    name: String(raw.name ?? ""),
    apiKey,
    keyId: String(raw.keyId ?? raw.key_id ?? ""),
    keyPrefix: String(raw.keyPrefix ?? raw.key_prefix ?? ""),
    keyLast4: String(raw.keyLast4 ?? raw.key_last4 ?? ""),
    keySource: String(raw.keySource ?? raw.api_key_source ?? ""),
    extraKeys: Array.isArray(raw.extraKeys ?? raw.extra_keys) ? raw.extraKeys ?? raw.extra_keys : [],
    projectId: String(raw.projectId ?? raw.project_id ?? ""),
    guardrailId: String(raw.guardrailId ?? raw.guardrail_id ?? ""),
    organizationId: String(raw.organizationId ?? raw.organization_id ?? ""),
    emailVerified: raw.emailVerified ?? raw.email_verified ?? null,
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
 * Issue a gateway key for one pooled account and remember its plaintext in the
 * pool entry (the store only keeps the hash, so this is the one place it lives).
 */
export function bindGatewayKey(store, account, { label, limits = { rpm: 0, dailyRequests: 0, concurrent: 0 } } = {}) {
  const { plaintext, hash, prefix } = mintGatewaySecret();
  const publicKey = store.addKey({ label: label ?? `allmodels:${account.email}`, hash, prefix, limits });
  const gatewayKey = {
    id: publicKey.id,
    label: publicKey.label,
    prefix: publicKey.prefix,
    plaintext,
    createdAt: publicKey.createdAt,
  };
  return gatewayKey;
}

/**
 * The single write path used by both the HTTP import endpoint and the worker's
 * NDJSON stream: merge the account, mint a gateway key once, keep it stable.
 * Idempotent, so a re-push after a crash never doubles keys.
 */
export function importAccount({ pool, store, record, mintKey = true, label }) {
  const { account, created } = pool.add(record);
  let gatewayKey = account.gatewayKey ?? null;
  if (mintKey && !gatewayKey && store) {
    gatewayKey = bindGatewayKey(store, account, { label: label ?? `allmodels:${account.email}` });
    pool.update(account.id, { gatewayKey });
  }
  return { account: pool.getAccount(account.id) ?? account, created, gatewayKey };
}

/**
 * Pool entry with secrets masked — what an agent, the admin API or a tool call
 * is allowed to see. Plaintext only exists in the pool file and in exports.
 */
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
    projectId: account.projectId ?? "",
    guardrailId: account.guardrailId ?? "",
    organizationId: account.organizationId ?? "",
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
