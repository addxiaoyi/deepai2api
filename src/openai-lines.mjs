// OpenAI 兼容线的通用引擎：一个描述 → 号池 + 注册控制器 + 反代。
//
// 六条线（agnes / cerebras / opencode-zen / requesty / unorouter / pollinations）
// 共用这套实现；每条的 `src/<line>-pool.mjs` 等只是薄壳，站点差异全在描述里。
//
// 与 jev-ai 那条线的差别：这里假设上游是**纯 OpenAI 形状**（部分还带 Anthropic），
// 所以反代不需要 shape 转换，只需要换 Key、轮询、故障切换，以及对聚合站做
// `:free` / `-free` 模型的**同族回退**。
import { existsSync, mkdirSync, readFileSync, renameSync, writeFileSync } from "node:fs";
import { spawn, spawnSync } from "node:child_process";
import { dirname, join, resolve } from "node:path";
import { fileURLToPath } from "node:url";
import { randomUUID } from "node:crypto";

import { mintKey as mintGatewaySecret } from "./keys.mjs";

const here = dirname(fileURLToPath(import.meta.url));
const repoRoot = resolve(here, "..");

const COOLDOWN_AUTH_MS = 10 * 60 * 1000;
const COOLDOWN_RATE_MS = 60 * 1000;
const RETRYABLE = new Set([401, 403, 408, 425, 429, 500, 502, 503, 504]);
const MAX_LINES = 200;

/** 补齐默认值 + 冻结：一条线的全部行为都由这个对象决定。 */
export function defineLine(desc) {
  const line = {
    kind: "openai",
    keyPrefix: "sk-",
    freeSuffix: "",
    anonymous: false,
    anthropic: false,
    models: [],
    docs: [`docs/${String(desc.id).toUpperCase()}-POOL.md`],
    envPrefix: `LINE_${String(desc.id).toUpperCase()}_`,
    ...desc,
  };
  line.schema = `dcg.pool/${line.id}@1`;
  line.pyModule = line.pyModule ?? `${String(line.id).replace(/-/g, "_")}_reg`;
  line.toolDir = line.toolDir ?? `tools/${line.id}-reg`;
  line.relayPrefix = `/${line.id}/v1/`;
  return Object.freeze(line);
}

/* ------------------------------------------------------------------ 号池 */

export class LinePool {
  /**
   * @param {object} line defineLine(...) 的结果
   * @param {string} path 池文件路径（`<dataDir>/<id>-pool.json`）
   */
  constructor(line, path, { log = () => {} } = {}) {
    this.line = line;
    this.path = resolve(path);
    this.log = log;
    this.state = this.#load();
    this.cursor = 0;
  }

  static at(line, dataDir, options) {
    return new LinePool(line, resolve(dataDir, `${line.id}-pool.json`), options);
  }

  #load() {
    const empty = {
      schema: this.line.schema,
      version: 1,
      provider: this.line.id,
      apiBase: this.line.apiBase,
      accounts: [],
    };
    if (!existsSync(this.path)) return empty;
    try {
      const parsed = JSON.parse(readFileSync(this.path, "utf8"));
      return {
        ...empty,
        apiBase: parsed.apiBase || this.line.apiBase,
        updatedAt: parsed.updatedAt ?? null,
        accounts: Array.isArray(parsed.accounts) ? parsed.accounts.map((row) => normalizeAccount(this.line, row)) : [],
      };
    } catch (error) {
      this.log("warn", `${this.line.id}: pool unreadable (${error.message}), starting empty`);
      return empty;
    }
  }

  get accounts() {
    return this.state.accounts;
  }

  get size() {
    return this.state.accounts.length;
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

  getAccount(id) {
    return this.state.accounts.find((a) => a.id === id) ?? null;
  }

  healthy(now = Date.now()) {
    return this.state.accounts.filter(
      (a) =>
        a.status === "active" &&
        a.healthy !== false &&
        (a.cooldownUntil ?? 0) <= now &&
        (this.line.anonymous || Boolean(a.apiKey)),
    );
  }

  pick({ exclude = [], now = Date.now() } = {}) {
    const rows = this.healthy(now).filter((a) => !exclude.includes(a.id));
    if (!rows.length) return null;
    const picked = rows[this.cursor % rows.length];
    this.cursor = (this.cursor + 1) % Math.max(1, rows.length);
    return picked;
  }

  keys() {
    return this.state.accounts.map((a) => a.apiKey).filter(Boolean);
  }

  stats() {
    const now = Date.now();
    const out = {
      schema: this.line.schema,
      line: this.line.id,
      total: this.state.accounts.length,
      active: 0,
      keys: 0,
      verified: 0,
      cooling: 0,
      unhealthy: 0,
      requests: 0,
      failures: 0,
      anonymous: this.line.anonymous === true,
      apiBase: this.line.apiBase,
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

  add(record) {
    const incoming = normalizeAccount(this.line, record);
    const now = new Date().toISOString();
    const existing =
      (incoming.apiKey && this.byKey(incoming.apiKey)) || (incoming.email ? this.byEmail(incoming.email) : null) || null;
    if (existing) {
      Object.assign(existing, incoming, {
        id: existing.id,
        createdAt: existing.createdAt,
        requests: existing.requests ?? 0,
        failures: existing.failures ?? 0,
        gatewayKey: existing.gatewayKey ?? incoming.gatewayKey ?? null,
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
      account.failures = 0;
      account.lastError = null;
      account.cooldownUntil = 0;
    } else {
      account.failures = (account.failures ?? 0) + 1;
      account.healthy = false;
      account.lastError = String(error ?? "unknown").slice(0, 200);
      account.cooldownUntil = cooldownMs > 0 ? now + cooldownMs : 0;
    }
    this.save();
    return account;
  }

  records() {
    return this.state.accounts.map((row) => ({ ...row }));
  }

  exportKeys(file = "") {
    const rows = this.keys();
    const target = resolve(file || `data/keys/${this.line.id}.txt`);
    mkdirSync(dirname(target), { recursive: true });
    writeFileSync(target, rows.length ? rows.join("\n") + "\n" : "", { mode: 0o600 });
    return { file: target, rows: rows.length };
  }

  exportConnection(file = "") {
    const target = resolve(file || `data/keys/${this.line.id}.json`);
    mkdirSync(dirname(target), { recursive: true });
    const body = {
      provider: this.line.id,
      openai_base: this.line.apiBase,
      anthropic_base: this.line.anthropic ? `${this.line.apiBase.replace(/\/v1$/, "")}/v1` : null,
      key_prefix: this.line.keyPrefix,
      anonymous: this.line.anonymous === true,
      free_suffix: this.line.freeSuffix || null,
      keys: this.keys(),
      updated_at: new Date().toISOString(),
    };
    writeFileSync(target, JSON.stringify(body, null, 2), { mode: 0o600 });
    return { file: target, keys: body.keys.length, openai_base: this.line.apiBase };
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
      const escape = (v) => `"${String(v ?? "").replaceAll('"', '""')}"`;
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
    return { ...this.state, schema: this.line.schema, apiBase: this.line.apiBase };
  }

  save() {
    this.state.updatedAt = new Date().toISOString();
    mkdirSync(dirname(this.path), { recursive: true });
    const tmp = `${this.path}.${randomUUID()}.tmp`;
    writeFileSync(tmp, JSON.stringify(this.toJSON(), null, 2), { mode: 0o600 });
    renameSync(tmp, this.path);
  }
}

function normalizeAccount(line, raw = {}) {
  const apiKey = String(raw.apiKey ?? raw.api_key ?? "").trim();
  return {
    email: String(raw.email ?? "").trim().toLowerCase(),
    password: String(raw.password ?? ""),
    name: String(raw.name ?? ""),
    apiKey,
    keyPrefix: String(raw.keyPrefix ?? raw.key_prefix ?? line.keyPrefix),
    userId: String(raw.userId ?? raw.user_id ?? ""),
    emailVerified: raw.emailVerified ?? raw.email_verified ?? null,
    credits: raw.credits && typeof raw.credits === "object" ? raw.credits : {},
    status: raw.status ?? "active",
    engine: raw.engine ?? (line.anonymous ? "anonymous" : "protocol"),
    createdAt: raw.createdAt ?? raw.created_at ?? new Date().toISOString(),
    gatewayKey: raw.gatewayKey ?? null,
    healthy: raw.healthy !== false,
    cooldownUntil: Number(raw.cooldownUntil ?? 0) || 0,
  };
}

export function maskAccount(account) {
  const preview = (value) => (value ? `${String(value).slice(0, 10)}…${String(value).slice(-4)}` : "");
  return {
    id: account.id,
    email: account.email,
    name: account.name,
    apiKeyPreview: preview(account.apiKey),
    keyPrefix: account.keyPrefix,
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

/** 一条账号发一把网关 Key（Store 只存哈希）。 */
export function bindGatewayKey(store, line, account, { label, limits = { rpm: 0, dailyRequests: 0, concurrent: 0 } } = {}) {
  const { plaintext, hash, prefix } = mintGatewaySecret();
  const publicKey = store.addKey({ label: label ?? `${line.id}:${account.email}`, hash, prefix, limits });
  return { id: publicKey.id, label: publicKey.label, prefix: publicKey.prefix, plaintext, createdAt: publicKey.createdAt };
}

export function importAccount({ line, pool, store, record, mintKey = true, label }) {
  const { account, created } = pool.add(record);
  let gatewayKey = account.gatewayKey ?? null;
  if (mintKey && !gatewayKey && store) {
    gatewayKey = bindGatewayKey(store, line, account, { label: label ?? `${line.id}:${account.email}` });
    pool.update(account.id, { gatewayKey });
  }
  return { account: pool.getAccount(account.id) ?? account, created, gatewayKey };
}

/* ------------------------------------------------------- 注册控制器 */

export class LineRegistrations {
  /**
   * @param {{line: object, dataDir: string, pool: object, store: object,
   *          python?: string, onAccount?: Function, log?: Function}} options
   */
  constructor({ line, dataDir, pool, store, python, onAccount, log = () => {} }) {
    this.line = line;
    this.dataDir = dataDir;
    this.pool = pool;
    this.store = store;
    this.onAccount = onAccount;
    this.log = log;
    this.statePath = join(dataDir, `${line.id}-register-job.json`);
    this.toolDir = process.env[`${line.envPrefix}REGISTER_DIR`] || join(repoRoot, line.toolDir);
    this.python =
      python || process.env[`${line.envPrefix}PYTHON`] || (process.platform === "win32" ? "python" : "python3");
    this.job = null;
    this.child = null;
    this._restore();
  }

  _restore() {
    try {
      if (!existsSync(this.statePath)) return;
      const saved = JSON.parse(readFileSync(this.statePath, "utf8"));
      if (!saved) return;
      if (saved.status === "running") {
        saved.status = "interrupted";
        saved.finishedAt = saved.finishedAt ?? new Date().toISOString();
      }
      this.job = saved;
    } catch {
      this.job = null;
    }
  }

  _persist() {
    try {
      mkdirSync(this.dataDir, { recursive: true });
      writeFileSync(this.statePath, JSON.stringify(this.job, null, 2));
    } catch (error) {
      this.log("warn", `${this.line.id}: cannot persist job: ${error.message}`);
    }
  }

  _line(level, text) {
    if (!this.job) return;
    this.job.lines.push({ at: new Date().toISOString(), level, text: String(text).slice(0, 400) });
    if (this.job.lines.length > MAX_LINES) this.job.lines.splice(0, this.job.lines.length - MAX_LINES);
  }

  _accept(account) {
    const result = importAccount({
      line: this.line,
      pool: this.pool,
      store: this.store,
      record: account,
      mintKey: this.job?.mintKeys !== false,
    });
    this.job.created = (this.job.created ?? 0) + (result.created ? 1 : 0);
    this.job.keys = this.pool.keys().length;
    this._line("info", `+ ${account.email}${result.gatewayKey ? ` gw=${result.gatewayKey.prefix}…` : ""}`);
    try {
      this.onAccount?.(result);
    } catch (error) {
      this._line("warn", `callback failed: ${error.message}`);
    }
    return result;
  }

  _event(event) {
    if (!this.job) return;
    switch (event.type) {
      case "log":
        this._line(event.level ?? "info", event.message ?? "");
        break;
      case "start":
        this.job.mailProvider = event.mail_provider ?? this.job.mailProvider;
        this._line("info", `worker 启动：${event.count} 个 × 并发 ${event.concurrency}`);
        break;
      case "attempt":
        this.job.current = { email: event.email, provider: event.provider };
        this.job.attempts = (this.job.attempts ?? 0) + 1;
        break;
      case "retry":
        this._line("warn", `第 ${event.round} 轮重试：${event.count} 个`);
        break;
      case "account":
        try {
          this._accept(event.account ?? {});
        } catch (error) {
          this._line("error", `入池失败：${error.message}`);
          this.job.failed = (this.job.failed ?? 0) + 1;
        }
        break;
      case "failed":
        this.job.failed = (this.job.failed ?? 0) + 1;
        this._line("warn", `失败 #${event.index} ${event.email}: ${event.kind} ${event.reason}`);
        break;
      case "pushed":
        this._line("debug", `回推成功 ${event.email}`);
        break;
      case "push_error":
        this._line("error", `回推失败 ${event.email}: ${event.message}`);
        break;
      case "done":
        this.job.summary = event.summary ?? null;
        this.job.created = event.summary?.created ?? this.job.created;
        this.job.failed = event.summary?.failed ?? this.job.failed;
        this._line("info", `完成：成功 ${this.job.created ?? 0}，失败 ${this.job.failed ?? 0}`);
        break;
      default:
        break;
    }
    this._persist();
  }

  status() {
    const toolPresent = existsSync(join(this.toolDir, this.line.pyModule, "cli.py"));
    return {
      ok: true,
      line: this.line.id,
      running: this.job?.status === "running",
      job: this.job,
      python: this.python,
      toolDir: this.toolDir,
      toolPresent,
      apiBase: this.line.apiBase,
      pool: this.pool.stats(),
    };
  }

  start(options = {}) {
    if (this.job?.status === "running") {
      const error = new Error(`${this.line.id} 已有一轮在跑，先停止或等它结束。`);
      error.status = 409;
      throw error;
    }
    if (!existsSync(join(this.toolDir, this.line.pyModule, "cli.py"))) {
      const error = new Error(`找不到注册工具：${this.toolDir}（可用 ${this.line.envPrefix}REGISTER_DIR 指定）`);
      error.status = 400;
      throw error;
    }

    const count = clampInt(options.count, 1, 1, 200);
    const concurrency = clampInt(options.concurrency, 1, 1, 8);
    const mailProvider = ["imap", "fixed", "mailtm", "tempmail_lol"].includes(options.mailProvider)
      ? options.mailProvider
      : "imap";
    const mailDomains = list(options.mailDomains, 40).map((d) => d.toLowerCase());
    const proxies = list(options.proxies, 50);
    const keyName = String(options.keyName ?? "").trim();
    const exportFormat = ["keys", "jsonl", "csv", "pool"].includes(options.export) ? String(options.export) : "";
    const baseUrl = String(options.baseUrl ?? this.line.baseUrl).trim() || this.line.baseUrl;

    const args = [
      "-m",
      this.line.pyModule,
      "--base-url",
      baseUrl,
      "run",
      "--json",
      "-n",
      String(count),
      "-c",
      String(concurrency),
      "--mail-provider",
      mailProvider,
    ];
    if (keyName) args.push("--key-name", keyName);
    for (const domain of mailDomains) args.push("--mail-domains", domain);
    for (const proxy of proxies) args.push("--proxy", proxy);
    if (options.verifyEmail === false) args.push("--no-verify");

    this.job = {
      id: `${this.line.id}-${Date.now().toString(36)}`,
      line: this.line.id,
      provider: this.line.id,
      status: "running",
      startedAt: new Date().toISOString(),
      finishedAt: null,
      requested: count,
      created: 0,
      failed: 0,
      keys: this.pool.keys().length,
      attempts: 0,
      concurrency,
      mailProvider,
      mailDomains,
      keyName: keyName || "",
      mintKeys: options.mintKeys !== false,
      exportFormat,
      baseUrl,
      proxies_in: proxies.length,
      lines: [],
      current: null,
      summary: null,
      exitCode: null,
      error: null,
      apply: null,
    };
    this._persist();
    this.log("info", `${this.line.id}: starting ${count} account(s)`);
    this._spawn(args);
    return this.status();
  }

  /**
   * 起一个**非注册**子命令任务（如 amd 的 `study`）：沿用 job/事件流/停止逻辑。
   *
   * @param {string} subcommand   例如 "study"
   * @param {{args?: string[], requested?: number, mode?: string}} options
   */
  runSubcommand(subcommand, { args = [], requested = 0, mode = "" } = {}) {
    if (this.job?.status === "running") {
      const error = new Error(`${this.line.id} 已有任务在跑，先等它结束。`);
      error.status = 409;
      throw error;
    }
    this.job = {
      id: `${this.line.id}-${subcommand}-${Date.now().toString(36)}`,
      line: this.line.id,
      provider: this.line.id,
      mode: mode || subcommand,
      status: "running",
      startedAt: new Date().toISOString(),
      finishedAt: null,
      requested,
      created: 0,
      failed: 0,
      keys: this.pool.keys().length,
      lines: [],
      summary: null,
      exitCode: null,
      error: null,
      apply: null,
    };
    this._persist();
    this.log("info", `${this.line.id}: ${subcommand}`);
    this._spawn(["-m", this.line.pyModule, subcommand, "--json", ...args]);
    return this.status();
  }

  _spawn(args) {
    const child = spawn(this.python, args, {
      cwd: this.toolDir,
      env: { ...process.env, PYTHONUTF8: "1", PYTHONIOENCODING: "utf-8", PYTHONUNBUFFERED: "1" },
      windowsHide: true,
    });
    this.child = child;
    if (this.job) this.job.pid = child.pid;

    let buffer = "";
    child.stdout.setEncoding("utf8");
    child.stdout.on("data", (chunk) => {
      buffer += chunk;
      let index;
      while ((index = buffer.indexOf("\n")) !== -1) {
        const raw = buffer.slice(0, index).trim();
        buffer = buffer.slice(index + 1);
        if (!raw) continue;
        try {
          this._event(JSON.parse(raw));
        } catch {
          this._line("debug", raw);
        }
      }
    });
    child.stderr.setEncoding("utf8");
    child.stderr.on("data", (chunk) => {
      for (const line of String(chunk).split(/\r?\n/)) {
        if (line.trim()) this._line("debug", line.trim());
      }
    });

    const finish = (status, extra = {}) => {
      if (!this.job || this.job.status !== "running") return;
      this.job.status = status;
      this.job.finishedAt = new Date().toISOString();
      Object.assign(this.job, extra);
      if (status !== "done") this._line("warn", `worker 退出：${status}`);
      try {
        this.job.apply = this.apply();
        this._line("info", `已导出 ${this.job.apply.keys} 把 Key -> ${this.job.apply.files.json}`);
      } catch (error) {
        this._line("error", `导出 Key 失败：${error.message}`);
      }
      if (status === "done" && this.job.exportFormat) {
        try {
          this.job.export = this.export({ format: this.job.exportFormat });
        } catch (error) {
          this._line("error", `导出失败：${error.message}`);
        }
      }
      this._persist();
      this.child = null;
      this.log("info", `${this.line.id}: ${status} (${this.job.created ?? 0} accounts)`);
    };

    child.on("error", (error) => {
      this._line("error", `无法启动 Python：${error.message}`);
      finish("failed", { error: error.message });
    });
    child.on("close", (code) => finish(code === 0 ? "done" : "failed", { exitCode: code }));
    return child;
  }

  stop() {
    if (!this.child || this.job?.status !== "running") return { ok: true, stopped: false, ...this.status() };
    const pid = this.child.pid;
    this._line("warn", "收到停止请求");
    try {
      if (process.platform === "win32" && pid) {
        spawnSync("taskkill", ["/pid", String(pid), "/T", "/F"], { windowsHide: true });
      } else {
        this.child.kill("SIGTERM");
      }
    } catch (error) {
      this._line("error", `停止失败：${error.message}`);
    }
    this.job.status = "stopped";
    this.job.finishedAt = new Date().toISOString();
    this._persist();
    this.child = null;
    return { ok: true, stopped: true, ...this.status() };
  }

  apply() {
    const keys = this.pool.exportKeys(resolve(this.dataDir, "keys", `${this.line.id}.txt`));
    const connection = this.pool.exportConnection(resolve(this.dataDir, "keys", `${this.line.id}.json`));
    return { keys: keys.rows, openaiBase: connection.openai_base, files: { txt: keys.file, json: connection.file } };
  }

  export({ format = "keys", file = "" } = {}) {
    const stamp = new Date().toISOString().replace(/[:.]/g, "-");
    const ext = format === "csv" ? "csv" : format === "jsonl" ? "jsonl" : format === "pool" ? "json" : "txt";
    const target = file ? resolve(file) : resolve(this.dataDir, "exports", `${this.line.id}-${stamp}.${ext}`);
    return this.pool.exportTo(target, { format });
  }
}

/* ---------------------------------------------------------------- 反代 */

export class LineRelay {
  constructor({ line, pool, log = () => {}, timeoutMs = 300_000 } = {}) {
    this.line = line;
    this.pool = pool;
    this.log = log;
    this.timeoutMs = timeoutMs;
  }

  status() {
    return {
      ok: true,
      line: this.line.id,
      upstream: this.line.apiBase,
      keyPrefix: this.line.keyPrefix,
      freeSuffix: this.line.freeSuffix || null,
      anthropic: this.line.anthropic === true,
      pool: this.pool?.stats?.() ?? null,
    };
  }

  acquire({ exclude = [] } = {}) {
    const account = this.pool.pick({ exclude });
    if (!account) {
      const error = new Error(`号池里没有可用账号（${this.line.id}）：先注册一批，或清掉冷却。`);
      error.status = 503;
      error.code = "no_account";
      throw error;
    }
    return account;
  }

  /**
   * 透传一次请求。`fallbackModels` 用于聚合站：模型带 `:free` 时，404 会换同族模型重试。
   *
   * @returns {{response: Response, account: object, path: string}}
   */
  async forward(req, path, { body = null, headers = {}, exclude = [], signal = null } = {}) {
    let lastError = null;
    const attemptPath = this.line.freeSuffix ? path : path;
    for (let attempt = 0; attempt < 3; attempt++) {
      const account = this.acquire({ exclude });
      exclude.push(account.id);
      const controller = new AbortController();
      const timer = setTimeout(() => controller.abort(), this.timeoutMs);
      const onAbort = () => controller.abort();
      signal?.addEventListener?.("abort", onAbort, { once: true });
      try {
        const auth = account.apiKey ? { Authorization: `Bearer ${account.apiKey}` } : {};
        const response = await fetch(`${this.line.apiBase.replace(/\/+$/, "")}${attemptPath.replace(/^\/v1/, "")}`, {
          method: req.method,
          headers: {
            "Content-Type": headers["content-type"] ?? "application/json",
            Accept: headers.accept ?? "*/*",
            ...(headers["anthropic-version"] ? { "anthropic-version": headers["anthropic-version"] } : {}),
            ...auth,
            "User-Agent": headers["user-agent"] ?? "deepai2api-gateway",
          },
          ...(body && req.method !== "GET" && req.method !== "HEAD" ? { body } : {}),
          signal: controller.signal,
        });

        if (RETRYABLE.has(response.status) && attempt < 2) {
          const text = await response.clone().text().catch(() => "");
          this.pool.recordResult(account.id, {
            ok: false,
            error: `HTTP ${response.status} ${text.slice(0, 120)}`,
            cooldownMs: response.status === 429 ? COOLDOWN_RATE_MS : COOLDOWN_AUTH_MS,
          });
          lastError = new Error(`upstream HTTP ${response.status}`);
          lastError.status = response.status;
          this.log("warn", `${this.line.id}: ${account.email} HTTP ${response.status}，换下一个`);
          continue;
        }
        const blamed = [401, 403, 429].includes(response.status) || response.status >= 500;
        this.pool.recordResult(account.id, { ok: !blamed });
        return { response, account, path: attemptPath };
      } catch (error) {
        this.pool.recordResult(account.id, { ok: false, error: `transport: ${error.message}`, cooldownMs: COOLDOWN_RATE_MS });
        lastError = error;
      } finally {
        clearTimeout(timer);
        signal?.removeEventListener?.("abort", onAbort);
      }
    }
    const error = new Error(`所有 ${this.line.id} Key 都失败了（${lastError?.message ?? "unknown"}）`);
    error.status = lastError?.status && lastError.status < 500 ? lastError.status : 503;
    error.code = "all_keys_failed";
    throw error;
  }
}

/** 上游 404/400 里带 "model" 的报错时，给一句能看懂的提示（聚合站模型漂移最常见）。 */
export function explainModelError(line, status, text) {
  if (status !== 404 && status !== 400) return null;
  if (!/model/i.test(text ?? "")) return null;
  return {
    error: {
      message:
        `${line.id} 不认识这个模型。${line.freeSuffix ? `免费模型通常带 "${line.freeSuffix}" 后缀，` : ""}` +
        `先 GET ${line.relayPrefix}models 拿当前可用列表。`,
      type: "invalid_request_error",
      code: "model_not_found",
    },
  };
}

function clampInt(value, fallback, min, max) {
  const parsed = Number(value);
  if (!Number.isFinite(parsed)) return fallback;
  return Math.min(max, Math.max(min, Math.trunc(parsed)));
}

function list(value, max) {
  return (Array.isArray(value) ? value : []).map((v) => String(v).trim()).filter(Boolean).slice(0, max);
}
