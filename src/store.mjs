// Persistent gateway state.
//
// Shaped after what a subscription-to-API gateway needs (sub2api parity):
//   accounts[]  — every linked DeepAI account, with health + usage counters
//   keys[]      — minted `sk-dcg-…` keys, per-key limits and usage
//   sessions{}  — admin WebUI sessions
//
// Written with atomic replace so a crash never leaves a half-written file.
import { mkdirSync, readFileSync, renameSync, writeFileSync, existsSync } from "node:fs";
import { dirname, join } from "node:path";
import { randomUUID } from "node:crypto";

const EMPTY = { version: 2, accounts: [], keys: [], sessions: {} };

export class Store {
  constructor(path, { writeDelayMs = 750 } = {}) {
    this.path = path;
    this.writeDelayMs = writeDelayMs;
    this.state = load(path);
    this.dirty = false;
    this.timer = null;
    this.flushing = false;
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
    this.saveNow();
    return true;
  }

  clearAccount() {
    this.state.accounts = [];
    this.saveNow();
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
      const tmp = `${this.path}.${randomUUID()}.tmp`;
      writeFileSync(tmp, JSON.stringify(this.state, null, 2), { mode: 0o600 });
      renameSync(tmp, this.path);
      this.dirty = false;
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

function load(path) {
  if (!existsSync(path)) return structuredClone(EMPTY);
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
    };
  } catch {
    return structuredClone(EMPTY);
  }
}
