// Clever AI Hub 注册控制器：把 tools/cleverai-reg 当子进程开起来，把它的 NDJSON
// 事件折算进网关自己的号池。
//
// 与 allmodels-register 结构一致（一轮一个 job、可强杀、重启后可见），但参数面换成
// cleverai 的：App Check 来源、域名邮箱、指纹浏览器内核、防封节奏。
import { spawn, spawnSync } from "node:child_process";
import { existsSync, mkdirSync, readFileSync, writeFileSync } from "node:fs";
import { dirname, join, resolve } from "node:path";
import { fileURLToPath } from "node:url";

import { importAccount } from "./cleverai-pool.mjs";

const here = dirname(fileURLToPath(import.meta.url));
const repoRoot = resolve(here, "..");

const MAX_LINES = 240;
const PROVIDERS = new Set(["imap", "fixed", "mailtm", "tempmail_lol"]);
const APPCHECK = new Set(["browser", "capsolver", "manual"]);
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

export class CleveraiRegistrations {
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
    this.statePath = join(dataDir, "cleverai-register-job.json");
    this.toolDir = process.env.CLEVERAI_REGISTER_DIR || join(repoRoot, "tools", "cleverai-reg");
    this.python = process.env.CLEVERAI_PYTHON || (process.platform === "win32" ? "python" : "python3");
    this.baseUrl = process.env.CLEVERAI_BASE_URL || "https://cleverai.app";
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
        // 网关死在半路：子进程跟着没了。
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
      this.log("warn", `cleverai: cannot persist job state: ${error.message}`);
    }
  }

  _line(level, text) {
    if (!this.job) return;
    this.job.lines.push({ at: new Date().toISOString(), level, text: String(text).slice(0, 400) });
    if (this.job.lines.length > MAX_LINES) this.job.lines.splice(0, this.job.lines.length - MAX_LINES);
  }

  /** 把 worker 的一条 account 事件并进号池，并给这条账号发网关 Key。 */
  _accept(account) {
    const result = importAccount({
      pool: this.pool,
      store: this.store,
      record: account,
      mintKey: this.job?.mintKeys !== false,
    });
    this.job.created = (this.job.created ?? 0) + 1;
    this.job.pushed = (this.job.pushed ?? 0) + 1;
    this.job.domains = mergeDomain(this.job.domains, account.domain ?? account.email?.split("@")[1]);
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
        this.job.appcheck = event.appcheck ?? this.job.appcheck;
        this.job.mailProvider = event.mail_provider ?? this.job.mailProvider;
        this.job.mailDomains = event.mail_domains ?? this.job.mailDomains ?? [];
        this._line(
          "info",
          `worker 启动：${event.count} 个 × 并发 ${event.concurrency}，邮箱 ${event.mail_provider}，App Check ${event.appcheck}`,
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
        this._line("debug", `worker 推送成功 ${event.email}`);
        break;
      case "push_error":
        this.job.pushFailed = (this.job.pushFailed ?? 0) + 1;
        this._line("error", `worker 推送失败 ${event.email}: ${event.message}`);
        break;
      case "failed":
        this.job.failed = (this.job.failed ?? 0) + 1;
        this._line("warn", `失败 #${event.index} ${event.email}: ${event.kind} ${event.reason}`);
        this.job.failureKinds = mergeKind(this.job.failureKinds, event.kind);
        break;
      case "done":
        this.job.summary = event.summary ?? null;
        this.job.created = event.summary?.created ?? this.job.created;
        this.job.failed = event.summary?.failed ?? this.job.failed;
        this.job.domains = event.summary?.domains ?? this.job.domains;
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

  /* ------------------------------------------------------------------- api */

  status() {
    const toolPresent = existsSync(join(this.toolDir, "cleverai_reg", "cli.py"));
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
      const error = new Error("已经有一轮 Clever AI 注册在跑，先停止或等它结束。");
      error.status = 409;
      throw error;
    }
    // 网关内运行时 worker 也会把每条账号 POST 回来；CLI 单独跑时走事件流合并。
    const push = options.push !== false;
    if (push && !this.token) {
      const error = new Error("网关没有管理口令，注册进程无法回推账号。用 --token 或 DEEPAI_GATEWAY_TOKEN 启动网关。");
      error.status = 400;
      throw error;
    }
    if (!existsSync(join(this.toolDir, "cleverai_reg", "cli.py"))) {
      const error = new Error(`找不到注册工具：${this.toolDir}（可用 CLEVERAI_REGISTER_DIR 指定）`);
      error.status = 400;
      throw error;
    }

    const count = clampInt(options.count, 1, 1, 500);
    const concurrency = clampInt(options.concurrency, 1, 1, 16);
    const mailProvider = PROVIDERS.has(options.mailProvider) ? options.mailProvider : "imap";
    const appcheck = APPCHECK.has(options.appcheck) ? options.appcheck : "browser";
    const engine = ENGINES.has(options.engine) ? options.engine : "auto";
    const browserStack = STACKS.has(options.browserStack) ? options.browserStack : "auto";
    const cdp = String(options.cdp ?? process.env.CLEVERAI_CDP ?? "").trim();
    const humanize = Number(options.humanize);
    const headful = options.headful === true;
    const baseUrl = String(options.baseUrl ?? this.baseUrl).trim() || this.baseUrl;
    const mailDomains = list(options.mailDomains, 40).map((d) => d.toLowerCase());
    const proxies = list(options.proxies, 20);
    const captchaKey = String(options.captchaKey ?? process.env.CLEVERAI_CAPTCHA_KEY ?? "").trim();
    const appcheckToken = String(options.appcheckToken ?? process.env.CLEVERAI_APPCHECK_TOKEN ?? "").trim();
    const chatEngine = String(options.chatEngine ?? "").trim();
    const exportFormat = ["jsonl", "csv", "pool"].includes(options.export) ? String(options.export) : "";
    const perProxy = clampInt(options.perProxyConcurrency, 1, 1, 4);
    const domainBurst = clampInt(options.perDomainBurst, 5, 1, 50);

    const args = [
      "-m",
      "cleverai_reg",
      "--base-url",
      baseUrl,
      "--captcha",
      appcheck,
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
      "--browser-stack",
      browserStack,
    ];
    if (cdp) args.push("--cdp", cdp);
    if (headful) args.push("--headful");
    if (Number.isFinite(humanize) && humanize > 0) args.push("--humanize", String(humanize));
    if (push) args.push("--push-url", `http://127.0.0.1:${this.port}`, "--push-token", this.token);
    if (captchaKey) args.push("--captcha-key", captchaKey);
    if (appcheckToken) args.push("--appcheck-token", appcheckToken);
    if (chatEngine) args.push("--chat-engine", chatEngine);
    for (const domain of mailDomains) args.push("--mail-domains", domain);
    for (const proxy of proxies) args.push("--proxy", proxy);
    if (options.verifyEmail === false) args.push("--no-verify");
    if (options.warmup === false) args.push("--no-warmup");

    this.job = {
      id: `cvreg-${Date.now().toString(36)}`,
      provider: "cleverai",
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
      appcheck,
      captchaKeySet: Boolean(captchaKey),
      appcheckTokenSet: Boolean(appcheckToken),
      engine,
      browserStack,
      cdp: cdp || "",
      headful,
      chatEngine: chatEngine || "",
      mintKeys: options.mintKeys !== false,
      push,
      exportFormat,
      baseUrl,
      perProxyConcurrency: perProxy,
      perDomainBurst: domainBurst,
      proxies: proxies.length,
      lines: [],
      current: null,
      summary: null,
      exitCode: null,
      error: null,
      failureKinds: [],
    };
    this._persist();
    this.log("info", `cleverai: starting ${count} account(s) via ${mailProvider}, appcheck=${appcheck}`);

    const child = spawn(this.python, args, {
      cwd: this.toolDir,
      env: {
        ...process.env,
        PYTHONUTF8: "1",
        PYTHONIOENCODING: "utf-8",
        PYTHONUNBUFFERED: "1",
        ...(captchaKey ? { CLEVERAI_CAPTCHA_KEY: captchaKey } : {}),
        ...(appcheckToken ? { CLEVERAI_APPCHECK_TOKEN: appcheckToken } : {}),
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
      // 批量注册后统一导出：账号 + 网关 Key 一个文件。
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
      this.log("info", `cleverai: ${status} (${this.job.created ?? 0} created)`);
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

  /** 统一导出号池账号 + Key。`format`: jsonl | csv | pool。 */
  export({ format = "jsonl", file = "" } = {}) {
    const stamp = new Date().toISOString().replace(/[:.]/g, "-");
    const ext = format === "csv" ? "csv" : format === "pool" ? "json" : "jsonl";
    const target = file
      ? resolve(file)
      : resolve(this.dataDir, "exports", `cleverai-accounts-${stamp}.${ext}`);
    const result = this.pool.exportTo(target, { format });
    this.log("info", `cleverai: exported ${result.rows} account(s) -> ${result.file}`);
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
  return out.slice(-40);
}

function mergeKind(rows, kind) {
  if (!kind) return rows ?? [];
  const out = Array.isArray(rows) ? rows : [];
  const found = out.find((row) => row.kind === kind);
  if (found) {
    found.count = (found.count ?? 0) + 1;
    return out;
  }
  out.push({ kind, count: 1 });
  return out.slice(-20);
}
