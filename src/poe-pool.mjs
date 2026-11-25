// Poe pool — deliberately separate from the DeepAI account pool.
//
// A Poe account is a cookie jar (p-b / cf_clearance), not an API key, and it has
// a life cycle DeepAI's scheduler knows nothing about (email_verified ->
// phone_required -> active -> dead). Keeping it in its own file means the DeepAI
// scheduler can never hand a Poe cookie to an OpenAI client by accident.
import { existsSync, mkdirSync, readFileSync, writeFileSync } from "node:fs";
import { randomUUID } from "node:crypto";
import { dirname, join } from "node:path";

export const POOL_SCHEMA = "dcg.pool/poe@1";
const EMPTY = { schema: POOL_SCHEMA, version: 1, accounts: [] };

export const POE_STATUSES = ["email_verified", "phone_required", "active", "dead"];

export class PoePool {
  constructor(dataDir, { writeDelayMs = 500 } = {}) {
    this.path = join(dataDir, "poe-pool.json");
    this.state = load(this.path);
    this.dirty = false;
    this.timer = null;
    this.writeDelayMs = writeDelayMs;
  }

  get accounts() {
    return this.state.accounts;
  }

  /** Never leak the cookie jar to a browser session; names only. */
  static public(row) {
    const cookies = row.cookies && typeof row.cookies === "object" ? row.cookies : {};
    return {
      id: row.id,
      email: row.email,
      status: row.status,
      engine: row.engine ?? "",
      phone: row.phone ?? "",
      smsProvider: row.smsProvider ?? "",
      cookieNames: Object.keys(cookies),
      createdAt: row.createdAt ?? null,
      updatedAt: row.updatedAt ?? null,
      note: row.note ?? "",
    };
  }

  upsert(account) {
    const email = String(account.email ?? "").trim().toLowerCase();
    if (!email) throw new Error("email is required");
    const now = new Date().toISOString();
    const status = POE_STATUSES.includes(account.status) ? account.status : "email_verified";
    const existing = this.state.accounts.find((a) => a.email === email);
    const cookies = account.cookies && typeof account.cookies === "object" ? account.cookies : null;
    if (existing) {
      Object.assign(existing, {
        status,
        engine: account.engine ?? existing.engine,
        phone: account.phone ?? existing.phone,
        smsProvider: account.smsProvider ?? existing.smsProvider,
        note: account.note ?? existing.note,
        updatedAt: now,
        ...(cookies ? { cookies } : {}),
      });
      this.touch();
      return { created: false, account: existing };
    }
    const row = {
      id: account.id ?? randomUUID(),
      email,
      status,
      engine: account.engine ?? "",
      phone: account.phone ?? "",
      smsProvider: account.smsProvider ?? "",
      cookies: cookies ?? {},
      note: account.note ?? "",
      createdAt: now,
      updatedAt: now,
    };
    this.state.accounts.push(row);
    this.touch();
    return { created: true, account: row };
  }

  remove(id) {
    const before = this.state.accounts.length;
    this.state.accounts = this.state.accounts.filter((a) => a.id !== id);
    if (this.state.accounts.length === before) return false;
    this.touch();
    return true;
  }

  mark(email, status) {
    const row = this.state.accounts.find((a) => a.email === String(email).toLowerCase());
    if (!row) return false;
    if (!POE_STATUSES.includes(status)) return false;
    row.status = status;
    row.updatedAt = new Date().toISOString();
    this.touch();
    return true;
  }

  counts() {
    const out = Object.fromEntries(POE_STATUSES.map((s) => [s, 0]));
    for (const row of this.state.accounts) out[row.status] = (out[row.status] ?? 0) + 1;
    return out;
  }

  list(limit = 100) {
    return [...this.state.accounts]
      .sort((a, b) => String(b.createdAt).localeCompare(String(a.createdAt)))
      .slice(0, limit)
      .map((row) => PoePool.public(row));
  }

  touch() {
    this.dirty = true;
    if (this.timer) return;
    this.timer = setTimeout(() => this.flush(), this.writeDelayMs);
    this.timer.unref?.();
  }

  flush() {
    if (!this.dirty) return;
    mkdirSync(dirname(this.path), { recursive: true });
    writeFileSync(this.path, JSON.stringify(this.state, null, 2));
    this.dirty = false;
    this.timer = null;
  }
}

function load(path) {
  try {
    if (!existsSync(path)) return structuredClone(EMPTY);
    const parsed = JSON.parse(readFileSync(path, "utf8"));
    if (!parsed || !Array.isArray(parsed.accounts)) return structuredClone(EMPTY);
    // 老文件没有 schema：读的时候补上，写回时落盘。
    return { schema: POOL_SCHEMA, ...parsed };
  } catch {
    return structuredClone(EMPTY);
  }
}
