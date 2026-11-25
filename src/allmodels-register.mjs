// Allmodels registration controller: drives tools/allmodels-reg as a child
// process and folds every account it produces into the gateway's Allmodels pool.
//
// The Python worker is the only thing that knows how the dashboard's signup and
// key endpoints behave; this module decides what to ask for, streams its NDJSON
// events to the admin API, mints a gateway key per account, and owns the
// lifecycle (one job at a time, killable, restart-visible).
import { spawn, spawnSync } from "node:child_process";
import { closeSync, existsSync, mkdirSync, openSync, readFileSync, unlinkSync, writeFileSync, writeSync } from "node:fs";
import { dirname, join, resolve } from "node:path";
import { fileURLToPath } from "node:url";

import { importAccount } from "./allmodels-pool.mjs";
import { isProcessAlive } from "./lock.mjs";

const here = dirname(fileURLToPath(import.meta.url));
const repoRoot = resolve(here, "..");

const MAX_LINES = 240;
const PROVIDERS = new Set(["tempmail_lol", "mailtm", "imap", "fixed"]);
const CAPTCHAS = new Set(["none", "capsolver", "2captcha", "browser", "manual"]);
const ENGINES = new Set(["auto", "protocol", "browser"]);
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

export class AllmodelsRegistrations {
  /**
   * @param {{dataDir: string, port: number, token: string|null, pool: object,
   *          store: object, onAccount?: Function, log?: Function}} options
   */
  constructor({ dataDir, port, token, pool, store, onAccount, log = () => {} }) {
    this.dataDir = dataDir;
    this.port = port;
    this.token = token;
    this.pool = pool;
    this.store = store;
    this.onAccount = onAccount;
    this.log = log;
    this.statePath = join(dataDir, "allmodels-register-job.json");
    this.lockPath = join(dataDir, "allmodels-register.lock");
    this.lockHeld = false;
    this.lockTtlMs = 6 * 3600 * 1000;
    this.toolDir = process.env.ALLMODELS_REGISTER_DIR || join(repoRoot, "tools", "allmodels-reg");
    this.python = process.env.ALLMODELS_PYTHON || (process.platform === "win32" ? "python" : "python3");
    this.baseUrl = process.env.ALLMODELS_BASE_URL || "https://dash.allmodels.io";
    this.job = null;
    this.child = null;
    this._restore();
  }

  /* --------------------------------------------------------------- plumbing */

  _restore() {
    try {
      if (!existsSync(this.statePath)) return;
      const saved = JSON.parse(readFileSync(this.statePath, "utf8"));
      if (!saved) return;
      if (saved.status === "running") {
        // The gateway died mid-run; the child is gone with it.
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
      this.log("warn", `allmodels: cannot persist job state: ${error.message}`);
    }
  }

  _line(level, text) {
    if (!this.job) return;
    this.job.lines.push({ at: new Date().toISOString(), level, text: String(text).slice(0, 400) });
    if (this.job.lines.length > MAX_LINES) this.job.lines.splice(0, this.job.lines.length - MAX_LINES);
  }

  /** Merge one worker account event into the pool; returns the stored entry. */
  _accept(account) {
    const result = importAccount({
      pool: this.pool,
      store: this.store,
      record: account,
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
        this.job.baseUrl = event.base_url ?? this.job.baseUrl;
        this.job.captcha = event.captcha ?? this.job.captcha;
        this.job.mailProvider = event.mail_provider ?? this.job.mailProvider;
        this._line(
          "info",
          `worker 启动：${event.count} 个 × 并发 ${event.concurrency}，邮箱 ${event.mail_provider}，人机校验 ${event.captcha}`,
        );
        break;
      case "attempt":
        this.job.current = { email: event.email, domain: event.domain, provider: event.provider };
        this.job.attempts = (this.job.attempts ?? 0) + 1;
        break;
      case "retry":
        this._line("warn", `第 ${event.round} 轮重试：${event.count} 个`);
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
        if (this.job.pushed === undefined) this.job.pushed = 0;
        this._line("debug", `worker 推送成功 ${event.email}`);
        break;
      case "push_error":
        this.job.pushFailed = (this.job.pushFailed ?? 0) + 1;
        this._line("error", `worker 推送失败 ${event.email}: ${event.message}`);
        break;
      case "failed":
        this.job.failed = (this.job.failed ?? 0) + 1;
        this._line("warn", `失败 #${event.index} ${event.email}: ${event.kind} ${event.reason}`);
        break;
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
   * 跨进程互斥：同一个 dataDir 只允许一轮注册。
   *
   * 网关、MCP server、CLI 是三个进程，共享同一份 job 文件与 python worker 的
   * sqlite；没有这把锁，两边会互相覆盖进度、重复注册同一批地址。锁文件带 pid，
   * 进程已死或超过 6 小时视为残留并回收。
   */
  _acquireJobLock() {
    mkdirSync(this.dataDir, { recursive: true });
    for (let attempt = 0; attempt < 2; attempt++) {
      try {
        const fd = openSync(this.lockPath, "wx");
        try {
          writeSync(fd, JSON.stringify({ pid: process.pid, at: Date.now() }));
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
    const toolPresent = existsSync(join(this.toolDir, "allmodels_reg", "cli.py"));
    return {
      ok: true,
      running: this.job?.status === "running",
      job: this.job,
      python: this.python,
      toolDir: this.toolDir,
      toolPresent,
      baseUrl: this.baseUrl,
      pool: this.pool.stats(),
    };
  }

  start(options = {}) {
    if (this.job?.status === "running") {
      const error = new Error("已经有一轮 Allmodels 注册在跑，先停止或等它结束。");
      error.status = 409;
      throw error;
    }
    // Inside the gateway the worker also POSTs each account back; standalone
    // (CLI) runs fold the same records from the NDJSON stream instead.
    const push = options.push !== false;
    if (!this._acquireJobLock()) {
      const error = new Error("另一个进程（网关 / MCP / CLI）正在跑 Allmodels 注册，先等它结束。");
      error.status = 409;
      throw error;
    }
    try {
      if (push && !this.token) {
        const error = new Error("网关没有管理口令，注册进程无法回推账号。用 --token 或 DEEPAI_GATEWAY_TOKEN 启动网关。");
        error.status = 400;
        throw error;
      }
      if (!existsSync(join(this.toolDir, "allmodels_reg", "cli.py"))) {
        const error = new Error(`找不到注册工具：${this.toolDir}（可用 ALLMODELS_REGISTER_DIR 指定）`);
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
      : PROVIDERS.has(options.mailProvider)
        ? options.mailProvider
        : "tempmail_lol";
    const captcha = CAPTCHAS.has(options.captcha) ? options.captcha : "none";
    const engine = ENGINES.has(options.engine) ? options.engine : "auto";
    const browserStack = STACKS.has(options.browserStack) ? options.browserStack : "auto";
    const cdp = String(options.cdp ?? process.env.ALLMODELS_CDP ?? "").trim();
    const humanize = Number(options.humanize);
    const headful = options.headful === true;
    const keysPerAccount = clampInt(options.keysPerAccount, 1, 1, 10);
    const baseUrl = String(options.baseUrl ?? this.baseUrl).trim() || this.baseUrl;
    const mailDomains = list(options.mailDomains, 40).map((d) => d.toLowerCase());
    const proxies = list(options.proxies, 20);
    const captchaKey = String(options.captchaKey ?? process.env.ALLMODELS_CAPTCHA_KEY ?? "").trim();
    const exportFormat = ["jsonl", "csv", "pool"].includes(options.export) ? String(options.export) : "";

    const args = [
      "-m",
      "allmodels_reg",
      "run",
      "--json",
      "-n",
      String(count),
      "-c",
      String(concurrency),
      "--mail-provider",
      mailProvider,
      "--base-url",
      baseUrl,
      "--keys-per-account",
      String(keysPerAccount),
      "--captcha",
      captcha,
      "--engine",
      engine,
      "--browser-stack",
      browserStack,
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

    this.job = {
      id: `amreg-${Date.now().toString(36)}`,
      provider: "allmodels",
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
      mintKeys: options.mintKeys !== false,
      push,
      exportFormat,
      baseUrl,
      proxies: proxies.length,
      lines: [],
      current: null,
      summary: null,
      exitCode: null,
      error: null,
    };
    this._persist();
    this.log("info", `allmodels: starting ${count} account(s) via ${mailProvider}, captcha=${captcha}`);

    const child = spawn(this.python, args, {
      cwd: this.toolDir,
      env: {
        ...process.env,
        PYTHONUTF8: "1",
        PYTHONIOENCODING: "utf-8",
        PYTHONUNBUFFERED: "1",
        ...(captchaKey ? { ALLMODELS_CAPTCHA_KEY: captchaKey } : {}),
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
      // "批量注册后统一导出": one file with accounts + panel keys + gateway keys.
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
      this.log("info", `allmodels: ${status} (${this.job.created ?? 0} created)`);
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

  /** Unified export of pool accounts + keys. `format`: jsonl | csv | pool. */
  export({ format = "jsonl", file = "" } = {}) {
    // Another process may have imported accounts since we loaded the file.
    this.pool.reload();
    const stamp = new Date().toISOString().replace(/[:.]/g, "-");
    const ext = format === "csv" ? "csv" : format === "pool" ? "json" : "jsonl";
    const target = file
      ? resolve(file)
      : resolve(this.dataDir, "exports", `allmodels-accounts-${stamp}.${ext}`);
    const result = this.pool.exportTo(target, { format });
    this.log("info", `allmodels: exported ${result.rows} account(s) -> ${result.file}`);
    return result;
  }
}

function mergeDomain(list, domain) {
  if (!domain) return list ?? [];
  const out = Array.isArray(list) ? list : [];
  const found = out.find((row) => row.domain === domain);
  if (found) {
    found.count = (found.count ?? 0) + 1;
    return out;
  }
  out.push({ domain, count: 1 });
  return out.slice(-40);
}
