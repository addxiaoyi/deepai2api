// proxyscrape 注册控制器：起 `tools/proxyscrape-reg` 子进程，把账号 + 代理并进号池。
//
// 与其它控制器同一套语义（一轮一 job、可强杀、重启后可见），但产物是**代理**：
// 任务结束后自动 `applyProxies()`，把出口分发给别的注册机。
import { spawn, spawnSync } from "node:child_process";
import { existsSync, mkdirSync, readFileSync, writeFileSync } from "node:fs";
import { dirname, join, resolve } from "node:path";
import { fileURLToPath } from "node:url";

import { applyProxies } from "./proxy-share.mjs";

const here = dirname(fileURLToPath(import.meta.url));
const repoRoot = resolve(here, "..");

const MAX_LINES = 240;
const PROVIDERS = new Set(["imap", "fixed", "mailtm", "tempmail_lol"]);
const CAPTCHAS = new Set(["browser", "capsolver", "solver", "manual", "none"]);
const ENGINES = new Set(["auto", "protocol", "browser"]);

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

export class ProxyscrapeRegistrations {
  /**
   * @param {{dataDir: string, port: number, token: string|null, pool: object,
   *          onAccount?: Function, log?: Function}} options
   */
  constructor({ dataDir, port, token, pool, onAccount, log = () => {} }) {
    this.dataDir = dataDir;
    this.port = port;
    this.token = token;
    this.pool = pool;
    this.onAccount = onAccount;
    this.log = log;
    this.statePath = join(dataDir, "proxyscrape-register-job.json");
    this.toolDir = process.env.PROXYSCRAPE_REGISTER_DIR || join(repoRoot, "tools", "proxyscrape-reg");
    this.python = process.env.PROXYSCRAPE_PYTHON || (process.platform === "win32" ? "python" : "python3");
    this.baseUrl = process.env.PROXYSCRAPE_BASE_URL || "https://dashboard.proxyscrape.com";
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
      this.log("warn", `proxyscrape: cannot persist job state: ${error.message}`);
    }
  }

  _line(level, text) {
    if (!this.job) return;
    this.job.lines.push({ at: new Date().toISOString(), level, text: String(text).slice(0, 400) });
    if (this.job.lines.length > MAX_LINES) this.job.lines.splice(0, this.job.lines.length - MAX_LINES);
  }

  /** 一条账号入池；返回新加了多少条代理。 */
  _accept(account) {
    const { account: stored, created, proxies } = this.pool.add(account);
    this.job.created = (this.job.created ?? 0) + (created ? 1 : 0);
    this.job.updated = (this.job.updated ?? 0) + (created ? 0 : 1);
    this.job.proxies = (this.job.proxies ?? 0) + proxies;
    this._line("info", `+ ${stored.email}（新增代理 ${proxies} 条）`);
    try {
      this.onAccount?.({ account: stored, created, proxies });
    } catch (error) {
      this._line("warn", `callback failed for ${stored.email}: ${error.message}`);
    }
    return { stored, created, proxies };
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
          `worker 启动：${event.count} 个 × 并发 ${event.concurrency}，邮箱 ${event.mail_provider}，Turnstile ${event.captcha}`,
        );
        break;
      case "attempt":
        this.job.current = { email: event.email, provider: event.provider };
        this.job.attempts = (this.job.attempts ?? 0) + 1;
        break;
      case "retry":
        this._line("warn", `第 ${event.round} 轮重试：${event.count} 个`);
        break;
      case "account": {
        const account = event.account ?? {};
        try {
          this._accept(account);
        } catch (error) {
          this._line("error", `入池失败 ${account.email}: ${error.message}`);
          this.job.failed = (this.job.failed ?? 0) + 1;
        }
        break;
      }
      case "pushed":
        this._line("debug", `worker 回推成功 ${event.email}`);
        break;
      case "push_error":
        this._line("error", `worker 回推失败 ${event.email}: ${event.message}`);
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
          `完成：成功 ${this.job.created ?? 0}，失败 ${this.job.failed ?? 0}，代理 ${this.job.proxies ?? 0} 条`,
        );
        break;
      default:
        break;
    }
    this._persist();
  }

  /* ------------------------------------------------------------------- api */

  status() {
    const toolPresent = existsSync(join(this.toolDir, "proxyscrape_reg", "cli.py"));
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
      const error = new Error("已经有一轮 proxyscrape 注册在跑，先停止或等它结束。");
      error.status = 409;
      throw error;
    }
    if (!existsSync(join(this.toolDir, "proxyscrape_reg", "cli.py"))) {
      const error = new Error(`找不到注册工具：${this.toolDir}（可用 PROXYSCRAPE_REGISTER_DIR 指定）`);
      error.status = 400;
      throw error;
    }

    const count = clampInt(options.count, 1, 1, 200);
    const concurrency = clampInt(options.concurrency, 1, 1, 8);
    const mailProvider = PROVIDERS.has(options.mailProvider) ? options.mailProvider : "imap";
    const captcha = CAPTCHAS.has(options.captcha) ? options.captcha : "browser";
    const engine = ENGINES.has(options.engine) ? options.engine : "auto";
    const browserStack = String(options.browserStack ?? "auto");
    const cdp = String(options.cdp ?? "").trim();
    const headful = options.headful === true;
    const baseUrl = String(options.baseUrl ?? this.baseUrl).trim() || this.baseUrl;
    const mailDomains = list(options.mailDomains, 40).map((d) => d.toLowerCase());
    const proxies = list(options.proxies, 50);
    // solver 模式下 captchaKey 就是 solver 地址（默认本地 5000）
    const captchaKey = String(
      options.captchaKey ??
        (captcha === "solver"
          ? process.env.PROXYSCRAPE_CAPTCHA_SOLVER_URL || process.env.CAPTCHA_SOLVER_URL || "http://127.0.0.1:5000"
          : process.env.PROXYSCRAPE_CAPTCHA_KEY) ??
        "",
    ).trim();
    const exportFormat = ["jsonl", "csv", "proxies", "pool"].includes(options.export) ? String(options.export) : "";
    const delay = Array.isArray(options.delay) && options.delay.length === 2 ? options.delay.map(Number) : null;
    const perProxy = clampInt(options.perProxyConcurrency, 1, 1, 4);

    const args = [
      "-m",
      "proxyscrape_reg",
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
      "--engine",
      engine,
      "--captcha",
      captcha,
      "--browser-stack",
      browserStack,
    ];
    if (cdp) args.push("--cdp", cdp);
    if (headful) args.push("--headful");
    if (delay) args.push("--delay", String(delay[0]), String(delay[1]));
    if (captchaKey) args.push("--captcha-key", captchaKey);
    for (const domain of mailDomains) args.push("--mail-domains", domain);
    for (const proxy of proxies) args.push("--proxy", proxy);
    if (options.verifyEmail === false) args.push("--no-verify");
    if (options.claimTrial === false) args.push("--no-trial");

    this.job = {
      id: `psreg-${Date.now().toString(36)}`,
      provider: "proxyscrape",
      status: "running",
      startedAt: new Date().toISOString(),
      finishedAt: null,
      requested: count,
      created: 0,
      updated: 0,
      failed: 0,
      proxies: 0,
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
      claimTrial: options.claimTrial !== false,
      exportFormat,
      baseUrl,
      perProxyConcurrency: perProxy,
      proxies_in: proxies.length,
      lines: [],
      current: null,
      summary: null,
      exitCode: null,
      error: null,
      share: null,
    };
    this._persist();
    this.log("info", `proxyscrape: starting ${count} account(s) via ${mailProvider}, turnstile=${captcha}`);

    const child = spawn(this.python, args, {
      cwd: this.toolDir,
      env: {
        ...process.env,
        PYTHONUTF8: "1",
        PYTHONIOENCODING: "utf-8",
        PYTHONUNBUFFERED: "1",
        ...(captchaKey ? { PROXYSCRAPE_CAPTCHA_KEY: captchaKey } : {}),
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
      // 产物是代理：跑完立刻分发给其它注册机
      try {
        this.job.share = applyProxies({ pool: this.pool, dataDir: this.dataDir });
        this._line("info", `已分发 ${this.job.share.total} 条代理到 ${this.job.share.lines.length} 条线`);
      } catch (error) {
        this._line("error", `分发代理失败：${error.message}`);
      }
      if (status === "done" && this.job.exportFormat) {
        try {
          this.job.export = this.export({ format: this.job.exportFormat });
          this._line("info", `已导出 ${this.job.export.rows} 行 -> ${this.job.export.file}`);
        } catch (error) {
          this._line("error", `导出失败：${error.message}`);
        }
      }
      this._persist();
      this.child = null;
      this.log("info", `proxyscrape: ${status} (${this.job.created ?? 0} accounts, ${this.job.proxies ?? 0} proxies)`);
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
    return { ok: true, stopped: true, ...this.status() };
  }

  export({ format = "proxies", file = "" } = {}) {
    const stamp = new Date().toISOString().replace(/[:.]/g, "-");
    const ext = format === "csv" ? "csv" : format === "jsonl" ? "jsonl" : format === "pool" ? "json" : "txt";
    const target = file ? resolve(file) : resolve(this.dataDir, "exports", `proxyscrape-${stamp}.${ext}`);
    const result = this.pool.exportTo(target, { format });
    this.log("info", `proxyscrape: exported ${result.rows} row(s) -> ${result.file}`);
    return result;
  }
}
