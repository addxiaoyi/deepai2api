// proxyscrape.com 号池 + 代理池 —— 这一条线只产出"代理"，不产出模型通道。
//
// 一个账号能领一份 premium 试用，试用会给出 `host:port:user:pass` 形态的代理。
// 号池文件同时存两样东西：
//   accounts[]  账号（email / accessToken / trialClaimed / 它带出来的代理）
//   proxies[]   去重后的代理串（下游直接吃 `http://user:pass@host:port`）
//
// 与其它线的差别：它没有反代面。它的产物被 `src/proxy-share.mjs` 分发给别的注册机
// （`data/proxies/<line>.txt`），由那些 worker 当出口用。
import { existsSync, mkdirSync, readFileSync, renameSync, writeFileSync } from "node:fs";
import { dirname, resolve } from "node:path";
import { randomUUID } from "node:crypto";

export const POOL_SCHEMA = "dcg.pool/proxyscrape@1";
export const POOL_VERSION = 1;
export const DEFAULT_BASE_URL = "https://dashboard.proxyscrape.com";
export const TURNSTILE_SITE_KEY = "0x4AAAAAAAFWUVCKyusT9T8r";

const clamp = (value, min, max) => Math.min(max, Math.max(min, Number(value) || 0));

/** `http://u:p@h:1` -> 结构化；解析失败返回 null。 */
export function parseEndpoint(endpoint) {
  const raw = String(endpoint ?? "").trim();
  if (!raw) return null;
  let scheme = "http";
  let rest = raw;
  const schemeMatch = /^([a-z0-9+.-]+):\/\//i.exec(rest);
  if (schemeMatch) {
    scheme = schemeMatch[1].toLowerCase();
    rest = rest.slice(schemeMatch[0].length);
  }
  let auth = "";
  const at = rest.lastIndexOf("@");
  if (at !== -1) {
    auth = rest.slice(0, at);
    rest = rest.slice(at + 1);
  }
  const [username, ...passwordParts] = auth ? auth.split(":") : [];
  const password = passwordParts.join(":");
  const colon = rest.lastIndexOf(":");
  if (colon === -1) return null;
  const host = rest.slice(0, colon);
  const port = Number(rest.slice(colon + 1));
  if (!host || !Number.isInteger(port) || port <= 0 || port > 65535) return null;
  return { endpoint: raw, scheme, host, port, username: username ?? "", password: password ?? "" };
}

export class ProxyscrapePool {
  /**
   * @param {string} path pool file, normally `<dataDir>/proxyscrape-pool.json`
   * @param {{baseUrl?: string, log?: Function}} options
   */
  constructor(path, { baseUrl = DEFAULT_BASE_URL, log = () => {} } = {}) {
    this.path = resolve(path);
    this.baseUrl = baseUrl;
    this.log = log;
    this.state = this.#load();
    this.cursor = 0;
  }

  static at(dataDir, options) {
    return new ProxyscrapePool(resolve(dataDir, "proxyscrape-pool.json"), options);
  }

  #load() {
    const empty = { schema: POOL_SCHEMA, version: POOL_VERSION, provider: "proxyscrape", accounts: [], proxies: [] };
    if (!existsSync(this.path)) return empty;
    try {
      const parsed = JSON.parse(readFileSync(this.path, "utf8"));
      const accounts = Array.isArray(parsed.accounts) ? parsed.accounts.map(normalizeAccount) : [];
      const proxies = Array.isArray(parsed.proxies) ? parsed.proxies.map(normalizeProxy).filter(Boolean) : [];
      return {
        schema: POOL_SCHEMA,
        version: POOL_VERSION,
        provider: "proxyscrape",
        baseUrl: parsed.baseUrl || this.baseUrl,
        updatedAt: parsed.updatedAt ?? null,
        accounts,
        proxies,
      };
    } catch (error) {
      this.log("warn", `proxyscrape: pool file unreadable (${error.message}), starting empty`);
      return empty;
    }
  }

  /* ---------------------------------------------------------------- reading */

  get accounts() {
    return this.state.accounts;
  }

  get proxies() {
    return this.state.proxies;
  }

  get size() {
    return this.state.accounts.length;
  }

  byEmail(email) {
    const needle = String(email ?? "").trim().toLowerCase();
    return this.state.accounts.find((a) => a.email.toLowerCase() === needle) ?? null;
  }

  /** 受健康计数影响的可用代理（`disabled` 的不给出去）。 */
  usable(now = Date.now()) {
    return this.state.proxies.filter(
      (p) => p.status === "active" && (p.cooldownUntil ?? 0) <= now,
    );
  }

  /** 轮询取一条代理（下游要一个个用，不能总给同一条）。 */
  pick({ exclude = [], now = Date.now() } = {}) {
    const candidates = this.usable(now).filter((p) => !exclude.includes(p.endpoint));
    if (!candidates.length) return null;
    const picked = candidates[this.cursor % candidates.length];
    this.cursor = (this.cursor + 1) % Math.max(1, candidates.length);
    return picked;
  }

  /** 分片：把代理按轮转切成 n 份，避免多条线同时压同一条出口。 */
  shard(count) {
    const rows = this.usable().map((p) => p.endpoint);
    const n = Math.max(1, Math.trunc(count) || 1);
    const buckets = Array.from({ length: n }, () => []);
    rows.forEach((row, index) => buckets[index % n].push(row));
    return buckets;
  }

  stats() {
    const now = Date.now();
    const out = {
      schema: POOL_SCHEMA,
      total: this.state.accounts.length,
      trialClaimed: 0,
      proxies: this.state.proxies.length,
      activeProxies: 0,
      cooling: 0,
      requests: 0,
      failures: 0,
      updatedAt: this.state.updatedAt ?? null,
      last24h: 0,
    };
    const dayAgo = now - 24 * 3600 * 1000;
    for (const account of this.state.accounts) {
      if (account.trialClaimed === true) out.trialClaimed++;
      if (account.createdAt && Date.parse(account.createdAt) >= dayAgo) out.last24h++;
      out.requests += account.requests ?? 0;
    }
    for (const proxy of this.state.proxies) {
      if (proxy.status === "active") out.activeProxies++;
      if ((proxy.cooldownUntil ?? 0) > now) out.cooling++;
      out.failures += proxy.failures ?? 0;
    }
    return out;
  }

  /* ---------------------------------------------------------------- writing */

  /** 合并一个账号：按 email 去重，代理并进全局池。 */
  add(record) {
    const incoming = normalizeAccount(record);
    const now = new Date().toISOString();
    let account = incoming.email ? this.byEmail(incoming.email) : null;
    let created = false;

    if (account) {
      Object.assign(account, incoming, {
        id: account.id,
        createdAt: account.createdAt ?? incoming.createdAt ?? now,
        updatedAt: now,
        requests: account.requests ?? 0,
      });
    } else {
      account = {
        ...incoming,
        id: incoming.id || randomUUID(),
        createdAt: incoming.createdAt || now,
        updatedAt: now,
        requests: 0,
      };
      this.state.accounts.push(account);
      created = true;
    }

    const added = this.addProxies(account.proxies, { sourceEmail: account.email });
    this.save();
    return { account, created, proxies: added };
  }

  addProxies(endpoints, { sourceEmail = "" } = {}) {
    let added = 0;
    for (const raw of Array.isArray(endpoints) ? endpoints : []) {
      const parsed = parseEndpoint(raw);
      if (!parsed) continue;
      const existing = this.state.proxies.find((p) => p.endpoint === parsed.endpoint);
      if (existing) {
        if (sourceEmail) existing.sources = unique([...(existing.sources ?? []), sourceEmail]);
        continue;
      }
      this.state.proxies.push({
        ...parsed,
        sources: sourceEmail ? [sourceEmail] : [],
        status: "active",
        ok: 0,
        failures: 0,
        cooldownUntil: 0,
        lastError: null,
        createdAt: new Date().toISOString(),
      });
      added++;
    }
    if (added) this.save();
    return added;
  }

  /** 一次探测/使用的回报：失败两次就冷却 10 分钟。 */
  recordProxy(endpoint, { ok, error = null, cooldownMs = 0, now = Date.now() } = {}) {
    const proxy = this.state.proxies.find((p) => p.endpoint === endpoint);
    if (!proxy) return null;
    if (ok) {
      proxy.ok = (proxy.ok ?? 0) + 1;
      proxy.failures = 0;
      proxy.lastError = null;
      proxy.cooldownUntil = 0;
    } else {
      proxy.failures = (proxy.failures ?? 0) + 1;
      proxy.lastError = String(error ?? "unknown").slice(0, 160);
      const cooldown = cooldownMs || (proxy.failures >= 2 ? 10 * 60_000 : 0);
      if (cooldown > 0) proxy.cooldownUntil = now + cooldown;
    }
    proxy.lastUsedAt = new Date(now).toISOString();
    this.save();
    return proxy;
  }

  removeAccount(id) {
    const before = this.state.accounts.length;
    this.state.accounts = this.state.accounts.filter((a) => a.id !== id);
    if (this.state.accounts.length === before) return false;
    this.save();
    return true;
  }

  removeProxy(endpoint) {
    const before = this.state.proxies.length;
    this.state.proxies = this.state.proxies.filter((p) => p.endpoint !== endpoint);
    if (this.state.proxies.length === before) return false;
    this.save();
    return true;
  }

  /* ---------------------------------------------------------------- exports */

  /** `proxies.txt`：一行一条，下游 `--proxy` 直接吃。 */
  exportProxies(file = "") {
    const rows = this.usable().map((p) => p.endpoint);
    const target = resolve(file || "data/exports/proxyscrape-proxies.txt");
    mkdirSync(dirname(target), { recursive: true });
    writeFileSync(target, rows.length ? rows.join("\n") + "\n" : "", { mode: 0o600 });
    return { file: target, rows: rows.length };
  }

  exportTo(file, { format = "jsonl" } = {}) {
    const target = resolve(file);
    mkdirSync(dirname(target), { recursive: true });
    const rows = this.records();
    if (format === "csv") {
      const header = ["email", "password", "trial_claimed", "proxy_count", "status", "created_at"];
      const escape = (value) => `"${String(value ?? "").replaceAll('"', '""')}"`;
      const body = rows.map((row) =>
        [row.email, row.password, row.trialClaimed, row.proxies.length, row.status, row.createdAt].map(escape).join(","),
      );
      writeFileSync(target, [header.join(","), ...body].join("\n") + "\n", { mode: 0o600 });
    } else if (format === "proxies") {
      const body = this.usable().map((p) => p.endpoint).join("\n");
      writeFileSync(target, body ? body + "\n" : "", { mode: 0o600 });
    } else if (format === "pool") {
      writeFileSync(target, JSON.stringify(this.toJSON(), null, 2), { mode: 0o600 });
    } else {
      const body = rows.map((row) => JSON.stringify(row)).join("\n");
      writeFileSync(target, body ? body + "\n" : "", { mode: 0o600 });
    }
    return { file: target, rows: rows.length, format };
  }

  records() {
    return this.state.accounts.map((account) => ({ ...account }));
  }

  toJSON() {
    return { ...this.state, baseUrl: this.baseUrl, updatedAt: this.state.updatedAt };
  }

  save() {
    this.state.updatedAt = new Date().toISOString();
    mkdirSync(dirname(this.path), { recursive: true });
    const tmp = `${this.path}.${randomUUID()}.tmp`;
    writeFileSync(tmp, JSON.stringify(this.toJSON(), null, 2), { mode: 0o600 });
    renameSync(tmp, this.path);
  }
}

const unique = (rows) => [...new Set(rows.filter(Boolean))];

export function normalizeProxy(raw = {}) {
  if (typeof raw === "string") return parseEndpoint(raw);
  const parsed = parseEndpoint(raw.endpoint ?? "");
  if (!parsed) return null;
  return {
    ...parsed,
    sources: Array.isArray(raw.sources) ? raw.sources : [],
    status: raw.status ?? "active",
    ok: clamp(raw.ok ?? 0, 0, Number.MAX_SAFE_INTEGER),
    failures: clamp(raw.failures ?? 0, 0, Number.MAX_SAFE_INTEGER),
    cooldownUntil: clamp(raw.cooldownUntil ?? 0, 0, Number.MAX_SAFE_INTEGER),
    lastError: raw.lastError ?? null,
    createdAt: raw.createdAt ?? new Date().toISOString(),
  };
}

export function normalizeAccount(raw = {}) {
  const email = String(raw.email ?? "").trim().toLowerCase();
  const proxies = unique(Array.isArray(raw.proxies) ? raw.proxies : []).filter((p) => parseEndpoint(p));
  return {
    email,
    password: String(raw.password ?? ""),
    accessToken: String(raw.accessToken ?? raw.access_token ?? ""),
    refreshToken: String(raw.refreshToken ?? raw.refresh_token ?? ""),
    trialClaimed: raw.trialClaimed ?? raw.trial_claimed ?? null,
    proxies,
    status: raw.status ?? "active",
    engine: raw.engine ?? "protocol",
    createdAt: raw.createdAt ?? raw.created_at ?? new Date().toISOString(),
  };
}

/** 管理接口脱敏视图：账号不带令牌，代理只报 host:port。 */
export function maskAccount(account) {
  return {
    id: account.id,
    email: account.email,
    trialClaimed: account.trialClaimed,
    engine: account.engine,
    status: account.status,
    proxyCount: Array.isArray(account.proxies) ? account.proxies.length : 0,
    proxyHosts: (account.proxies ?? []).slice(0, 5).map((p) => {
      const parsed = parseEndpoint(p);
      return parsed ? `${parsed.host}:${parsed.port}` : p;
    }),
    hasAccessToken: Boolean(account.accessToken),
    requests: account.requests ?? 0,
    createdAt: account.createdAt,
    updatedAt: account.updatedAt,
  };
}

export function maskProxy(proxy) {
  return {
    endpoint: `${proxy.scheme}://${proxy.host}:${proxy.port}`,
    host: proxy.host,
    port: proxy.port,
    hasAuth: Boolean(proxy.username),
    status: proxy.status,
    ok: proxy.ok ?? 0,
    failures: proxy.failures ?? 0,
    cooldownUntil: proxy.cooldownUntil ?? 0,
    lastError: proxy.lastError ?? null,
    sources: (proxy.sources ?? []).length,
  };
}
