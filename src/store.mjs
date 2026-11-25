// Persistent gateway state.
//
// Shaped after what a subscription-to-API gateway needs (sub2api parity):
//   accounts[]  — every linked DeepAI account, with health + usage counters
//   keys[]      — minted `sk-dcg-…` keys, per-key limits and usage
//   sessions{}  — admin WebUI sessions
//
// Written with atomic replace so a crash never leaves a half-written file.
import { mkdirSync, readFileSync, renameSync, writeFileSync, existsSync, statSync } from "node:fs";
import { dirname, join } from "node:path";
import { createHash, randomUUID } from "node:crypto";

import { withFileLock } from "./lock.mjs";

const EMPTY = { version: 2, accounts: [], keys: [], sessions: {}, tombstones: { accounts: [], keys: [] } };

export class Store {
  constructor(path, { writeDelayMs = 750 } = {}) {
    this.path = path;
    this.writeDelayMs = writeDelayMs;
    this.state = load(path);
    this.dirty = false;
    this.timer = null;
    this.flushing = false;
    // Cross-process safety: the gateway, the MCP server and the CLI may all hold
    // a Store for the same file. Remember when we last wrote it, and merge the
    // on-disk copy in whenever another process wrote in between.
    this.writtenAt = mtimeOf(path);
    // Content fingerprint of the last bytes we wrote: exact detection of
    // "somebody else wrote since", independent of filesystem mtime granularity.
    this.writtenHash = hashFile(path);
    this.deletedAccounts = new Set();
    this.deletedKeys = new Set();
    // What *this* process created/changed since the last flush. Entries that only
    // exist locally and are not in here were deleted by the other process.
    this.ownedAccounts = new Set();
    this.ownedKeys = new Set();
    this.onLockBusy = null;
  }

  static at(dataDir, options) {
    return new Store(join(dataDir, "gateway-state.json"), options);
  }

  /* --------------------------------------------------------------- accounts */

  get accounts() {
    return this.state.accounts;
  }

  /** The account new traffic goes to by default (first healthy one). */
  get account() {
    return this.state.accounts[0] ?? null;
  }

  getAccount(id) {
    return this.state.accounts.find((a) => a.id === id) ?? null;
  }

  /**
   * Link (or re-link) an account. Re-login of the same api key refreshes the
   * existing record instead of piling up duplicates.
   */
  upsertAccount(account) {
    const existing = this.state.accounts.find((a) => a.apiKey === account.apiKey);
    const patch = {
      apiKey: account.apiKey,
      username: account.username ?? "",
      isPro: account.isPro === true,
      connect: account.connect ?? null,
      source: account.source ?? "oauth",
      linkedAt: new Date().toISOString(),
      healthy: true,
      lastError: null,
      cooldownUntil: 0,
    };
    if (existing) {
      Object.assign(existing, patch);
      this.ownedAccounts.add(existing.id);
      this.save();
      return existing;
    }
    const created = {
      id: randomUUID(),
      label: account.label ?? account.username ?? "account",
      requests: 0,
      promptTokens: 0,
      completionTokens: 0,
      lastUsedAt: null,
      ...patch,
    };
    this.state.accounts.push(created);
    this.ownedAccounts.add(created.id);
    this.saveNow();
    return created;
  }

  /** Compatibility shim: a single linked account. */
  setAccount(account) {
    for (const existing of this.state.accounts) existing.primary = false;
    const upserted = this.upsertAccount(account);
    upserted.primary = true;
    // keep the primary first so `account` keeps meaning what it used to
    this.state.accounts = [upserted, ...this.state.accounts.filter((a) => a.id !== upserted.id)];
    this.saveNow();
    return upserted;
  }

  removeAccount(id) {
    const before = this.state.accounts.length;
    this.state.accounts = this.state.accounts.filter((a) => a.id !== id);
    if (this.state.accounts.length === before) return false;
    this.deletedAccounts.add(id);
    this.addTombstone("accounts", id);
    this.saveNow();
    return true;
  }

  clearAccount() {
    for (const account of this.state.accounts) {
      this.deletedAccounts.add(account.id);
      this.addTombstone("accounts", account.id);
    }
    this.state.accounts = [];
    this.saveNow();
  }

  addTombstone(kind, id) {
    this.state.tombstones ??= { accounts: [], keys: [] };
    this.state.tombstones[kind] ??= [];
    const list = this.state.tombstones[kind];
    if (!list.some((row) => row.id === id)) list.push({ id, at: Date.now() });
    if (list.length > 500) list.splice(0, list.length - 500);
  }

  /** Record the outcome of a turn against an account. */
  recordAccount(id, { ok, usage, error, cooldownMs = 0 } = {}) {
    const account = this.getAccount(id);
    if (!account) return;
    account.requests = (account.requests ?? 0) + 1;
    account.lastUsedAt = new Date().toISOString();
    if (usage) {
      account.promptTokens = (account.promptTokens ?? 0) + (usage.prompt_tokens ?? 0);
      account.completionTokens = (account.completionTokens ?? 0) + (usage.completion_tokens ?? 0);
    }
    if (ok) {
      account.healthy = true;
      account.lastError = null;
      account.cooldownUntil = 0;
    } else {
      account.healthy = false;
      account.lastError = String(error ?? "unknown").slice(0, 200);
      account.cooldownUntil = cooldownMs > 0 ? Date.now() + cooldownMs : 0;
    }
    // The scheduler reads health from memory, so a batched write is enough.
    this.save();
  }

  /* ------------------------------------------------------------------- keys */

  listKeys() {
    return this.state.keys.map(publicKey);
  }

  findKeyById(id) {
    return this.state.keys.find((k) => k.id === id) ?? null;
  }

  addKey({ label, hash, prefix, limits }) {
    const key = {
      id: randomUUID(),
      label: String(label ?? "").trim() || "untitled",
      hash,
      prefix,
      createdAt: new Date().toISOString(),
      lastUsedAt: null,
      requests: 0,
      promptTokens: 0,
      completionTokens: 0,
      revoked: false,
      limits: normalizeLimits(limits),
    };
    this.state.keys.push(key);
    this.ownedKeys.add(key.id);
    this.saveNow();
    return publicKey(key);
  }

  revokeKey(id) {
    const key = this.findKeyById(id);
    if (!key) return false;
    key.revoked = true;
    this.saveNow();
    return true;
  }

  deleteKey(id) {
    const before = this.state.keys.length;
    this.state.keys = this.state.keys.filter((k) => k.id !== id);
    if (this.state.keys.length === before) return false;
    this.deletedKeys.add(id);
    this.addTombstone("keys", id);
    this.saveNow();
    return true;
  }

  findByHash(hash) {
    return this.state.keys.find((k) => k.hash === hash && !k.revoked) ?? null;
  }

  /**
   * Count one request against a key and report whether it is still inside its
   * limits. Rate limiting is per-process sliding window: enough for a single
   * gateway instance, and it never blocks the first request of a window.
   */
  touchKey(id, { tokens = 0, now = Date.now() } = {}) {
    const key = this.findKeyById(id);
    if (!key) return { allowed: false, reason: "unknown" };
    const limits = normalizeLimits(key.limits);
    key.window = key.window ?? { startedAt: now, count: 0 };
    if (now - key.window.startedAt >= 60_000) key.window = { startedAt: now, count: 0 };
    if (limits.rpm > 0 && key.window.count >= limits.rpm) {
      return { allowed: false, reason: "rate_limit", retryAfterMs: 60_000 - (now - key.window.startedAt) };
    }
    if (limits.dailyRequests > 0) {
      const day = new Date(now).toISOString().slice(0, 10);
      key.day = key.day ?? { date: day, count: 0 };
      if (key.day.date !== day) key.day = { date: day, count: 0 };
      if (key.day.count >= limits.dailyRequests) {
        return { allowed: false, reason: "daily_quota" };
      }
      key.day.count++;
    }
    key.window.count++;
    key.requests = (key.requests ?? 0) + 1;
    key.lastUsedAt = new Date(now).toISOString();
    if (tokens) key.completionTokens = (key.completionTokens ?? 0) + tokens;
    // Counters only: batched, so a burst of requests costs one write.
    this.save();
    return { allowed: true };
  }

  /* --------------------------------------------------------------- sessions */

  touchSession(token) {
    return this.state.sessions?.[token] ?? null;
  }

  addSession(token) {
    this.state.sessions ??= {};
    this.state.sessions[token] = { createdAt: new Date().toISOString() };
    const entries = Object.entries(this.state.sessions);
    if (entries.length > 32) {
      entries.sort((a, b) => String(a[1].createdAt).localeCompare(String(b[1].createdAt)));
      for (const [stale] of entries.slice(0, entries.length - 32)) delete this.state.sessions[stale];
    }
    this.saveNow();
  }

  dropSession(token) {
    if (this.state.sessions?.[token]) {
      delete this.state.sessions[token];
      this.saveNow();
    }
  }

  save() {
    // Usage counters move on every request; writing the whole file each time
    // would turn a chat into a disk-write amplification problem. Batch instead.
    this.dirty = true;
    if (this.timer) return;
    this.timer = setTimeout(() => {
      this.timer = null;
      this.flush();
    }, this.writeDelayMs);
    this.timer.unref?.();
  }

  /** Read the file again and fold in what other processes wrote (no write). */
  reloadFromDisk() {
    if (!existsSync(this.path)) return false;
    const disk = load(this.path);
    this.state = mergeStoreState(this.state, disk, {
      deletedAccounts: this.deletedAccounts,
      deletedKeys: this.deletedKeys,
      ownedAccounts: this.ownedAccounts,
      ownedKeys: this.ownedKeys,
    });
    return true;
  }

  /** Write immediately. Used for account/key changes and on shutdown. */
  flush() {
    if (this.timer) {
      clearTimeout(this.timer);
      this.timer = null;
    }
    if (!this.dirty || this.flushing) return;
    this.flushing = true;
    try {
      mkdirSync(dirname(this.path), { recursive: true });
      const locked = withFileLock(this.path, () => {
        // Another process (MCP server / CLI) may have written since our last
        // flush: fold its accounts, keys and sessions in before overwriting.
        if (hashFile(this.path) !== this.writtenHash) {
          const disk = load(this.path);
          this.state = mergeStoreState(this.state, disk, {
            deletedAccounts: this.deletedAccounts,
            deletedKeys: this.deletedKeys,
            ownedAccounts: this.ownedAccounts,
            ownedKeys: this.ownedKeys,
          });
        }
        this.deletedAccounts.clear();
        this.deletedKeys.clear();
        // ownedAccounts/ownedKeys are NOT cleared: a concurrent writer that
        // started from a stale base can drop our entries, and the next merge
        // must recognise them as ours instead of treating them as deletions.
        const tmp = `${this.path}.${randomUUID()}.tmp`;
        const bytes = JSON.stringify(this.state, null, 2);
        writeFileSync(tmp, bytes, { mode: 0o600 });
        renameSync(tmp, this.path);
        this.writtenAt = Date.now();
        this.writtenHash = createHash("sha1").update(bytes).digest("hex");
      });
      if (locked.ok) {
        this.dirty = false;
      } else if (locked.reason === "busy") {
        // Keep dirty and let the next save() retry; never drop a structural change.
        this.dirty = true;
        this.onLockBusy?.();
      } else if (locked.error) {
        throw locked.error;
      }
    } finally {
      this.flushing = false;
    }
  }

  /** Persist now: structural changes must survive an immediate crash. */
  saveNow() {
    this.dirty = true;
    this.flush();
  }
}

function normalizeLimits(limits) {
  const value = limits ?? {};
  return {
    rpm: Number.isFinite(Number(value.rpm)) ? Math.max(0, Number(value.rpm)) : 0,
    dailyRequests: Number.isFinite(Number(value.dailyRequests)) ? Math.max(0, Number(value.dailyRequests)) : 0,
    concurrent: Number.isFinite(Number(value.concurrent)) ? Math.max(0, Number(value.concurrent)) : 0,
  };
}

function publicKey(key) {
  return {
    id: key.id,
    label: key.label,
    prefix: key.prefix,
    createdAt: key.createdAt,
    lastUsedAt: key.lastUsedAt,
    requests: key.requests ?? 0,
    promptTokens: key.promptTokens ?? 0,
    completionTokens: key.completionTokens ?? 0,
    revoked: key.revoked === true,
    limits: normalizeLimits(key.limits),
  };
}

export function publicAccount(account) {
  if (!account) return null;
  return {
    id: account.id,
    label: account.label,
    username: account.username,
    isPro: account.isPro === true,
    source: account.source,
    linkedAt: account.linkedAt,
    healthy: account.healthy !== false,
    lastError: account.lastError ?? null,
    lastUsedAt: account.lastUsedAt ?? null,
    requests: account.requests ?? 0,
    promptTokens: account.promptTokens ?? 0,
    completionTokens: account.completionTokens ?? 0,
    cooldownUntil: account.cooldownUntil ?? 0,
    apiKeyPreview: account.apiKey ? `${account.apiKey.slice(0, 8)}…${account.apiKey.slice(-4)}` : "",
  };
}

function load(path) {  if (!existsSync(path)) return structuredClone(EMPTY);
  try {
    const parsed = JSON.parse(readFileSync(path, "utf8"));
    const accounts = Array.isArray(parsed.accounts) ? parsed.accounts : [];
    // v1 stored a single `account`; migrate it in place.
    if (!accounts.length && parsed.account) {
      accounts.push({
        id: randomUUID(),
        label: parsed.account.username || "account",
        primary: true,
        requests: 0,
        promptTokens: 0,
        completionTokens: 0,
        healthy: true,
        cooldownUntil: 0,
        lastError: null,
        ...parsed.account,
      });
    }
    for (const account of accounts) {
      account.requests ??= 0;
      account.promptTokens ??= 0;
      account.completionTokens ??= 0;
      account.cooldownUntil ??= 0;
      account.healthy = account.healthy !== false;
      account.id ??= randomUUID();
      account.label ??= account.username ?? "account";
    }
    const keys = (Array.isArray(parsed.keys) ? parsed.keys : []).map((key) => ({
      ...key,
      limits: normalizeLimits(key.limits),
      promptTokens: key.promptTokens ?? 0,
      completionTokens: key.completionTokens ?? 0,
      requests: key.requests ?? 0,
    }));
    return {
      version: 2,
      accounts,
      keys,
      sessions: parsed.sessions && typeof parsed.sessions === "object" ? parsed.sessions : {},
      tombstones: {
        accounts: Array.isArray(parsed.tombstones?.accounts) ? parsed.tombstones.accounts : [],
        keys: Array.isArray(parsed.tombstones?.keys) ? parsed.tombstones.keys : [],
      },
    };
  } catch {
    return structuredClone(EMPTY);
  }
}

function mtimeOf(path) {
  try {
    return statSync(path).mtimeMs;
  } catch {
    return 0;
  }
}

/** Stable fingerprint of a file's bytes; "" when it does not exist yet. */
function hashFile(path) {
  try {
    return createHash("sha1").update(readFileSync(path)).digest("hex");
  } catch {
    return "";
  }
}

/**
 * Union two copies of the gateway state.
 *
 * Structure (which accounts / keys exist) is decided by both sides — a key minted
 * by the MCP server must survive the gateway's next flush, and an account linked
 * in the browser must survive the CLI's. Usage counters take the max, deletes are
 * explicit (an id in `deleted*` never comes back), and a revoked key stays revoked.
 */
export function mergeStoreState(
  local,
  disk,
  { deletedAccounts = new Set(), deletedKeys = new Set(), ownedAccounts = null, ownedKeys = null } = {},
) {
  const accounts = new Map();
  for (const account of disk.accounts ?? []) accounts.set(account.id, account);
  for (const account of local.accounts ?? []) {
    const other = accounts.get(account.id);
    if (!other) {
      // Only keep a local-only account when this process is the one that created
      // it; otherwise the other process deleted it.
      if (!ownedAccounts || ownedAccounts.has(account.id)) accounts.set(account.id, account);
      continue;
    }
    const newer = (account.lastUsedAt ?? "") >= (other.lastUsedAt ?? "") ? account : other;
    accounts.set(account.id, {
      ...other,
      ...newer,
      id: account.id,
      requests: Math.max(account.requests ?? 0, other.requests ?? 0),
      promptTokens: Math.max(account.promptTokens ?? 0, other.promptTokens ?? 0),
      completionTokens: Math.max(account.completionTokens ?? 0, other.completionTokens ?? 0),
      cooldownUntil: Math.max(account.cooldownUntil ?? 0, other.cooldownUntil ?? 0),
      healthy: newer.healthy !== false,
      linkedAt: other.linkedAt ?? account.linkedAt,
    });
  }
  for (const id of deletedAccounts) accounts.delete(id);

  const keys = new Map();
  for (const key of disk.keys ?? []) keys.set(key.id, key);
  for (const key of local.keys ?? []) {
    const other = keys.get(key.id);
    if (!other) {
      if (!ownedKeys || ownedKeys.has(key.id)) keys.set(key.id, key);
      continue;
    }
    keys.set(key.id, {
      ...other,
      ...key,
      id: key.id,
      revoked: key.revoked === true || other.revoked === true,
      requests: Math.max(key.requests ?? 0, other.requests ?? 0),
      promptTokens: Math.max(key.promptTokens ?? 0, other.promptTokens ?? 0),
      completionTokens: Math.max(key.completionTokens ?? 0, other.completionTokens ?? 0),
      lastUsedAt: [key.lastUsedAt, other.lastUsedAt].filter(Boolean).sort().pop() ?? null,
    });
  }
  for (const id of deletedKeys) keys.delete(id);

  const sessions = { ...(disk.sessions ?? {}), ...(local.sessions ?? {}) };
  const entries = Object.entries(sessions);
  let kept = sessions;
  if (entries.length > 32) {
    entries.sort((a, b) => String(a[1]?.createdAt).localeCompare(String(b[1]?.createdAt)));
    kept = Object.fromEntries(entries.slice(entries.length - 32));
  }

  // Tombstones are the durable form of a delete: they travel with the file, so a
  // process that still has the entry in memory cannot bring it back.
  const tombstones = mergeTombstones(local, disk);
  for (const row of tombstones.accounts) accounts.delete(row.id);
  for (const row of tombstones.keys) keys.delete(row.id);

  return { version: 2, accounts: [...accounts.values()], keys: [...keys.values()], sessions: kept, tombstones };
}

function mergeTombstones(local, disk) {
  const out = { accounts: [], keys: [] };
  for (const kind of ["accounts", "keys"]) {
    const seen = new Map();
    for (const row of [...(disk.tombstones?.[kind] ?? []), ...(local.tombstones?.[kind] ?? [])]) {
      if (!row?.id) continue;
      const at = Number(row.at ?? 0);
      const previous = seen.get(row.id);
      seen.set(row.id, { id: row.id, at: previous ? Math.max(previous, at) : at });
    }
    const cutoff = Date.now() - 30 * 24 * 3600 * 1000;
    out[kind] = [...seen.values()].filter((row) => row.at >= cutoff).slice(-500);
  }
  return out;
}
