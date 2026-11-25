// 通用 provider 注册控制器：驱动 tools/<provider>-reg 的 Python worker，
// 把每个账号折进对应 provider 的号池，并给每个账号发一把网关 Key。
//
// 设计目标（与 allmodels-register.mjs 并列，而不是替代它）：
//   * **独立号池联动**：每个 provider 一份 pool 文件、一把 job 锁、一份 job 状态，
//     wavespeed / novita / allmodels 三条线可以同时跑，互不覆盖、互不阻塞。
//   * **域名邮箱 + 指纹/协议双引擎**：参数原样透传给 worker，worker 才是懂站点的人。
//   * **防封策略**：antiBan 配置整块透传（域名轮换、单域名速率窗、每 IP 串行化、
//     失败退避表、会话复用），并在控制器侧再做一层"同 provider 同出口不并发"的护栏。
//   * **网页反代出 apikey**：worker 在浏览器腿里把 key 读出来后回推；反代层
//     （provider-relay.mjs）用同一把 key 对外提供 [OI] 兼容接口。
import { spawn, spawnSync } from "node:child_process";
import { closeSync, existsSync, mkdirSync, openSync, readFileSync, unlinkSync, writeFileSync, writeSync } from "node:fs";
import { dirname, join, resolve } from "node:path";
import { fileURLToPath } from "node:url";

import { importAccount } from "./provider-pool.mjs";
import { isProcessAlive } from "./lock.mjs";
import { getProvider, resolveEndpoints } from "./provider-catalog.mjs";

const here = dirname(fileURLToPath(import.meta.url));
const repoRoot = resolve(here, "..");

const MAX_LINES = 240;
const MAIL_PROVIDERS = new Set(["tempmail_lol", "mailtm", "imap", "fixed", "domain"]);
const CAPTCHAS = new Set(["none", "capsolver", "2captcha", "browser", "manual", "auto"]);
const ENGINES = new Set(["auto", "protocol", "browser", "hybrid"]);
const STACKS = new Set(["auto", "camoufox", "patchright", "playwright"]);

const clampInt = (value, fallback, min, max) => {
  const parsed = Number(value);
  if (!Number.isFinite(parsed)) return fallback;
  return Math.min(max, Math.max(min, Math.trunc(parsed)));
};

const list = (value, max) =>
  (Array.isArray(value) ? value : [])
    .map((item) => String(item).trim())
    .filter(Boolean)
    .slice(0, max);

/** 防封策略默认值 —— 与 worker 侧 AntiBanConfig 对齐，缺项在这里补齐。 */
export const DEFAULT_ANTI_BAN = Object.freeze({
  // 域名邮箱
  mailDomains: [],
  domainRpm: 3, // 单域名每分钟最多建几个账号
  domainRph: 40, // 单域名每小时上限
  domainHealthFloor: 0.25, // 健康分低于此值暂时弃用该域名
  // 出口
  ipConcurrency: 1, // 同一出口 IP 同时只跑 1 个注册
  proxyRotate: true,
  // 节奏
  jitterMs: [1200, 4200],
  cooldownAfterBatch: 0,
  // 指纹
  geoAlign: true, // 时区/语言跟随代理出口
  freshProfile: true, // 每账号全新 profile
  // 会话
  reuseSession: true, // 复用 cookie jar，避免重复过 OAuth / 验证码
  sessionTtlHours: 72,
  // 失败退避（秒）
  backoff: {
    rate_limited: [20, 45],
    blocked: [20, 40],
    captcha_required: [15, 30],
    verification_pending: [5, 10],
    key_not_obtainable: [0, 0],
    email_taken: [0, 0],
    email_rejected: [0, 0],
  },
});

export class ProviderRegistrations {
  /**
   * @param {{dataDir: string, port: number, token: string|null, provider: string|object,
   *          pool: object, store: object, onAccount?: Function, log?: Function}} options
   */
  constructor({ dataDir, port, token, provider, pool, store, onAccount, log = () => {} }) {
    this.dataDir = dataDir;
    this.port = port;
    this.token = token;
    this.provider = typeof provider === "string" ? getProvider(provider) : provider;
    this.pool = pool;
    this.store = store;
    this.onAccount = onAccount;
    this.log = log;

    this.statePath = join(dataDir, `${this.provider.id}-register-job.json`);
    this.lockPath = join(dataDir, this.provider.lockFile);
    this.lockHeld = false;
    this.lockTtlMs = 6 * 3600 * 1000;

    this.toolDir = process.env[this.provider.toolDirEnv] || join(repoRoot, this.provider.workerDir);
    this.python = process.env[this.provider.pythonEnv] || (process.platform === "win32" ? "python" : "python3");
    this.endpoints = resolveEndpoints(this.provider);

    this.job = null;
    this.child = null;
    this._restore();
  }

  /* --------------------------------------------------------------- plumbing */

  get id() {
    return this.provider.id;
  }

  get toolPresent() {
    return existsSync(join(this.toolDir, this.provider.workerModule, "cli.py"));
  }

  _restore() {
    try {
      if (!existsSync(this.statePath)) return;
      const saved = JSON.parse(readFileSync(this.statePath, "utf8"));
      if (!saved) return;
      if (saved.status === "running") {
        // 网关在跑批时挂了：子进程随之消失。
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
      this.log("warn", `${this.id}: cannot persist job state: ${error.message}`);
    }
  }

  _line(level, text) {
    if (!this.job) return;
    const lvl = ["debug", "info", "warn", "error"].includes(level) ? level : "info";
    this.job.lines.push({ at: new Date().toISOString(), level: lvl, text: String(text).slice(0, 400) });
    if (this.job.lines.length > MAX_LINES) this.job.lines.splice(0, this.job.lines.length - MAX_LINES);
  }

  /** 合并一个 worker 账号事件进号池，返回入库结果。 */
  _accept(account) {
    const result = importAccount({
      pool: this.pool,
      store: this.store,
      record: { ...account, provider: this.id },
      mintKey: this.job?.mintKeys !== false,
    });
    this.job.created = (this.job.created ?? 0) + 1;
    this.job.pushed = (this.job.pushed ?? 0) + 1;
    this.job.domains = mergeDomain(this.job.domains, account.domain);
    const keyPrefix = result.gatewayKey?.prefix ? ` key=${result.gatewayKey.prefix}…` : "";
    this._line("info", `+ ${account.email}${keyPrefix}`);
    try {
      this.onAccount?.(result);
    } catch (error) {
      this._line("warn", `callback failed for ${account.email}: ${error.message}`);
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
        this.job.engine = event.engine ?? this.job.engine;
        this.job.mailProvider = event.mail_provider ?? this.job.mailProvider;
        this.job.captcha = event.captcha ?? this.job.captcha;
        this.job.antiBan = event.anti_ban ?? this.job.antiBan;
        this._line(
          "info",
          `worker 启动：${event.count} 个 × 并发 ${event.concurrency}，邮箱 ${event.mail_provider}，` +
            `引擎 ${event.engine}，人机校验 ${event.captcha}`,
        );
        break;
      case "attempt":
        this.job.current = {
          email: event.email,
          domain: event.domain,
          provider: event.provider,
          engine: event.engine ?? null,
          ip: event.ip ?? null,
        };
        this.job.attempts = (this.job.attempts ?? 0) + 1;
        break;
      case "retry":
        this.job.retryRound = event.round ?? 0;
        this._line("warn", `第 ${event.round} 轮重试：${event.count} 个`);
        break;
      case "backoff":
        this._line("warn", `${event.kind} 退避 ${event.seconds}s（${event.email ?? "-"}）`);
        break;
      case "domain":
        this.job.domainHealth = event.domains ?? this.job.domainHealth;
        break;
      case "account": {
        const account = event.account ?? {};
        try {
          const { account: stored } = this._accept({
            ...account,
            domain: event.domain,
            engine: event.engine ?? account.engine,
          });
          this.job.gatewayKeys = (this.job.gatewayKeys ?? 0) + (stored.gatewayKey ? 1 : 0);
        } catch (error) {
          this._line("error", `入池失败 ${account.email}: ${error.message}`);
          this.job.pushFailed = (this.job.pushFailed ?? 0) + 1;
        }
        break;
      }
      case "pushed":
        this._line("debug", `worker 推送成功 ${event.email}`);
        break;
      case "push_error":
        this.job.pushFailed = (this.job.pushFailed ?? 0) + 1;
        this._line("error", `worker 推送失败 ${event.email}: ${event.message}`);
        break;
      case "failed": {
        this.job.failed = (this.job.failed ?? 0) + 1;
        const kind = event.kind || "error";
        this.job.failureReasons = this.job.failureReasons ?? {};
        const row = (this.job.failureReasons[kind] = this.job.failureReasons[kind] ?? { count: 0, example: "" });
        row.count += 1;
        if (!row.example) row.example = String(event.reason ?? "").slice(0, 160);
        this._line("warn", `失败 #${event.index ?? "?"} ${event.email || "?"}: ${kind} ${event.reason ?? ""}`);
        break;
      }
      case "done":
        this.job.summary = event.summary ?? null;
        this.job.created = event.summary?.created ?? this.job.created;
        this.job.failed = event.summary?.failed ?? this.job.failed;
        this._line(
          "info",
          `完成：成功 ${this.job.created ?? 0}，失败 ${this.job.failed ?? 0}，用时 ${event.summary?.seconds ?? "?"}s`,
        );
        break;
      default:
        break;
    }
    this._persist();
  }

  /* ------------------------------------------------------------- job lock */

  /**
   * 跨进程互斥：同一个 dataDir 的同一个 provider 只允许一轮注册。
   * 网关、MCP server、CLI 是三个进程，共享同一份 job 文件与 worker 的 sqlite。
   * 注意锁名带 provider：wavespeed 的锁不会挡住 novita 的注册。
   */
  _acquireJobLock() {
    mkdirSync(this.dataDir, { recursive: true });
    for (let attempt = 0; attempt < 2; attempt++) {
      try {
        const fd = openSync(this.lockPath, "wx");
        try {
          writeSync(fd, JSON.stringify({ pid: process.pid, at: Date.now(), provider: this.id }));
        } finally {
          closeSync(fd);
        }
        this.lockHeld = true;
        this._installExitHook();
        return true;
      } catch (error) {
        if (error.code !== "EEXIST") throw error;
        let owner = {};
        try {
          owner = JSON.parse(readFileSync(this.lockPath, "utf8"));
        } catch {
          owner = {};
        }
        const age = Date.now() - Number(owner.at ?? 0);
        if (!isProcessAlive(Number(owner.pid)) || age > this.lockTtlMs) {
          try {
            unlinkSync(this.lockPath);
          } catch {
            // someone else reclaimed it first
          }
          continue;
        }
        return false;
      }
    }
    return false;
  }

  _releaseJobLock() {
    if (!this.lockHeld) return;
    try {
      unlinkSync(this.lockPath);
    } catch {
      // already gone
    }
    this.lockHeld = false;
  }

  _installExitHook() {
    if (this._exitHook) return;
    this._exitHook = () => this._releaseJobLock();
    process.once("exit", this._exitHook);
  }

  /* ------------------------------------------------------------------- api */

  status() {
    return {
      ok: true,
      provider: this.id,
      running: this.job?.status === "running",
      job: this.job,
      python: this.python,
      toolDir: this.toolDir,
      toolPresent: this.toolPresent,
      panelBase: this.endpoints.panelBase,
      apiBase: this.endpoints.apiBase,
      pool: this.pool.stats(),
    };
  }

  start(options = {}) {
    if (this.job?.status === "running") {
      const error = new Error(`${this.id} 已经有一轮注册在跑，先停止或等它结束。`);
      error.status = 409;
      throw error;
    }
    const push = options.push !== false;
    if (!this._acquireJobLock()) {
      const error = new Error(`另一个进程（网关 / MCP / CLI）正在跑 ${this.id} 注册，先等它结束。`);
      error.status = 409;
      throw error;
    }
    try {
      if (push && !this.token) {
        const error = new Error("网关没有管理口令，注册进程无法回推账号。用 --token 或 DEEPAI_GATEWAY_TOKEN 启动网关。");
        error.status = 400;
        throw error;
      }
      if (!this.toolPresent) {
        const error = new Error(
          `找不到注册工具：${this.toolDir}（可用 ${this.provider.toolDirEnv} 指定）`,
        );
        error.status = 400;
        throw error;
      }
    } catch (error) {
      this._releaseJobLock();
      throw error;
    }

    const count = clampInt(options.count, 1, 1, 500);
    const concurrency = clampInt(options.concurrency, 2, 1, 16);
    const fixedEmail = String(options.email ?? "").trim();
    const mailProvider = fixedEmail
      ? "fixed"
      : MAIL_PROVIDERS.has(options.mailProvider)
        ? options.mailProvider
        : "imap";
    const captcha = CAPTCHAS.has(options.captcha) ? options.captcha : "none";
    const engine = ENGINES.has(options.engine) ? options.engine : "auto";
    const browserStack = STACKS.has(options.browserStack) ? options.browserStack : "auto";
    const cdp = String(options.cdp ?? process.env[`${this.provider.envPrefix}_CDP`] ?? "").trim();
    const humanize = Number(options.humanize);
    const headful = options.headful === true;
    const keysPerAccount = clampInt(options.keysPerAccount, 1, 1, 10);
    const panelBase = String(options.baseUrl ?? options.panelBase ?? this.endpoints.panelBase).trim();
    const apiBase = String(options.apiBase ?? this.endpoints.apiBase).trim();
    const mailDomains = list(options.mailDomains, 60).map((d) => d.toLowerCase());
    const proxies = list(options.proxies, 40);
    const captchaKey = String(options.captchaKey ?? process.env[`${this.provider.envPrefix}_CAPTCHA_KEY`] ?? "").trim();
    const exportFormat = ["jsonl", "csv", "pool"].includes(options.export) ? String(options.export) : "";
    const antiBan = { ...DEFAULT_ANTI_BAN, ...(options.antiBan && typeof options.antiBan === "object" ? options.antiBan : {}) };
    if (mailDomains.length) antiBan.mailDomains = mailDomains;
    const keyPolicy = options.keyPolicy && typeof options.keyPolicy === "object" ? options.keyPolicy : {};
    const sessionImport = options.session && typeof options.session === "object" ? options.session : null;

    const args = [
      "-m",
      this.provider.workerModule,
      "run",
      "--json",
      "-n",
      String(count),
      "-c",
      String(concurrency),
      "--mail-provider",
      mailProvider,
      "--base-url",
      panelBase,
      "--api-base",
      apiBase,
      "--keys-per-account",
      String(keysPerAccount),
      "--captcha",
      captcha,
      "--engine",
      engine,
      "--browser-stack",
      browserStack,
      "--anti-ban",
      JSON.stringify(antiBan),
      "--key-policy",
      JSON.stringify(keyPolicy),
      // worker 的 sqlite/jsonl 落在网关数据目录下（data/<provider>-reg/），
      // 不放 tools/<provider>-reg/out：一是跟号池状态同源好备份，
      // 二是避免不同 dataDir 的两套网关共用同一份去重库。
      "--out-dir",
      join(this.dataDir, `${this.id}-reg`),
    ];
    if (cdp) args.push("--cdp", cdp);
    if (headful) args.push("--headful");
    if (Number.isFinite(humanize) && humanize > 0) args.push("--humanize", String(humanize));
    if (push) {
      args.push("--push-url", `http://127.0.0.1:${this.port}`, "--push-token", this.token);
    }
    if (captchaKey) args.push("--captcha-key", captchaKey);
    if (fixedEmail) args.push("--email", fixedEmail);
    for (const domain of mailDomains) args.push("--mail-domains", domain);
    for (const proxy of proxies) args.push("--proxy", proxy);
    if (options.verifyEmail === false) args.push("--no-verify");
    if (sessionImport) args.push("--session-file", String(sessionImport.file ?? ""));

    this.job = {
      id: `${this.id}-reg-${Date.now().toString(36)}`,
      provider: this.id,
      status: "running",
      startedAt: new Date().toISOString(),
      finishedAt: null,
      requested: count,
      created: 0,
      failed: 0,
      pushed: 0,
      attempts: 0,
      concurrency,
      mailProvider,
      mailDomains,
      captcha,
      captchaKeySet: Boolean(captchaKey),
      engine,
      browserStack,
      cdp: cdp || "",
      headful,
      keysPerAccount,
      keyPolicy,
      mintKeys: options.mintKeys !== false,
      push,
      exportFormat,
      panelBase,
      apiBase,
      proxies: proxies.length,
      antiBan,
      sessionImport: sessionImport ? { file: sessionImport.file ?? "", cookies: (sessionImport.cookies ?? []).length } : null,
      lines: [],
      current: null,
      summary: null,
      exitCode: null,
      error: null,
    };
    this._persist();
    this.log("info", `${this.id}: starting ${count} account(s) via ${mailProvider}/${engine}, captcha=${captcha}`);

    const child = spawn(this.python, args, {
      cwd: this.toolDir,
      env: {
        ...process.env,
        PYTHONUTF8: "1",
        PYTHONIOENCODING: "utf-8",
        PYTHONUNBUFFERED: "1",
        [`${this.provider.envPrefix}_GATEWAY_URL`]: `http://127.0.0.1:${this.port}`,
        ...(captchaKey ? { [`${this.provider.envPrefix}_CAPTCHA_KEY`]: captchaKey } : {}),
      },
      windowsHide: true,
    });
    this.child = child;
    this.job.pid = child.pid;

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
      if (status === "done" && this.job.exportFormat) {
        try {
          this.job.export = this.export({ format: this.job.exportFormat });
          this._line("info", `已导出 ${this.job.export.rows} 个账号 -> ${this.job.export.file}`);
        } catch (error) {
          this._line("error", `导出失败：${error.message}`);
        }
      }
      this._persist();
      this.child = null;
      this._releaseJobLock();
      this.log("info", `${this.id}: ${status} (${this.job.created ?? 0} created)`);
    };

    child.on("error", (error) => {
      this._line("error", `无法启动 Python：${error.message}`);
      finish("failed", { error: error.message });
    });
    child.on("close", (code) => {
      finish(code === 0 ? "done" : "failed", { exitCode: code });
    });

    return this.status();
  }

  stop() {
    if (!this.child || this.job?.status !== "running") {
      return { ok: true, stopped: false, ...this.status() };
    }
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
    this._releaseJobLock();
    return { ok: true, stopped: true, ...this.status() };
  }

  /** 统一导出：号池账号 + 面板 Key + 网关 Key。 */
  export({ format = "jsonl", file = "" } = {}) {
    const stamp = new Date().toISOString().replace(/[:.]/g, "-");
    const ext = format === "csv" ? "csv" : format === "pool" ? "json" : "jsonl";
    const target = file
      ? resolve(file)
      : resolve(this.dataDir, "exports", `${this.id}-accounts-${stamp}.${ext}`);
    const result = this.pool.exportTo(target, { format });
    this.log("info", `${this.id}: exported ${result.rows} account(s) -> ${result.file}`);
    return result;
  }
}

function mergeDomain(rows, domain) {
  if (!domain) return rows ?? [];
  const out = Array.isArray(rows) ? rows : [];
  const found = out.find((row) => row.domain === domain);
  if (found) {
    found.count = (found.count ?? 0) + 1;
    return out;
  }
  out.push({ domain, count: 1 });
  return out.slice(-60);
}

/** 一次性构造某个 provider 的控制器（网关 / MCP / CLI 共用）。 */
export function makeProviderRegistrations({ dataDir, port, token, providerId, pool, store, onAccount, log }) {
  return new ProviderRegistrations({
    dataDir,
    port,
    token,
    provider: getProvider(providerId),
    pool,
    store,
    onAccount,
    log,
  });
}
