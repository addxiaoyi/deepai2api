// jev-ai.org 注册控制器：起 `tools/jevai-reg` 子进程，把账号 + Key 并进号池，
// 任务结束自动导出 Key 描述（`data/keys/jevai.json`）供其它工具直接接入。
import { spawn, spawnSync } from "node:child_process";
import { existsSync, mkdirSync, readFileSync, writeFileSync } from "node:fs";
import { dirname, join, resolve } from "node:path";
import { fileURLToPath } from "node:url";

import { importAccount } from "./jevai-pool.mjs";

const here = dirname(fileURLToPath(import.meta.url));
const repoRoot = resolve(here, "..");

const MAX_LINES = 240;
const PROVIDERS = new Set(["imap", "fixed", "mailtm", "tempmail_lol"]);

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

export class JevaiRegistrations {
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
    this.statePath = join(dataDir, "jevai-register-job.json");
    this.toolDir = process.env.JEVAIS_REGISTER_DIR || join(repoRoot, "tools", "jevai-reg");
    this.python = process.env.JEVAIS_PYTHON || (process.platform === "win32" ? "python" : "python3");
    this.baseUrl = process.env.JEVAIS_BASE_URL || "https://jev-ai.org";
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
      this.log("warn", `jevai: cannot persist job state: ${error.message}`);
    }
  }

  _line(level, text) {
    if (!this.job) return;
    this.job.lines.push({ at: new Date().toISOString(), level, text: String(text).slice(0, 400) });
    if (this.job.lines.length > MAX_LINES) this.job.lines.splice(0, this.job.lines.length - MAX_LINES);
  }

  _accept(account) {
    const result = importAccount({
      pool: this.pool,
      store: this.store,
      record: account,
      mintKey: this.job?.mintKeys !== false,
    });
    this.job.created = (this.job.created ?? 0) + (result.created ? 1 : 0);
    this.job.updated = (this.job.updated ?? 0) + (result.created ? 0 : 1);
    this.job.keys = this.pool.keys().length;
    const keyPrefix = result.gatewayKey?.prefix ? ` gw=${result.gatewayKey.prefix}…` : "";
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
        this.job.mailProvider = event.mail_provider ?? this.job.mailProvider;
        this.job.verifyEmail = event.verify_email ?? this.job.verifyEmail;
        this._line(
          "info",
          `worker 启动：${event.count} 个 × 并发 ${event.concurrency}，邮箱 ${event.mail_provider}`,
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
      case "checkin":
        this.job.checked = (this.job.checked ?? 0) + 1;
        this.job.gained = Math.round(((this.job.gained ?? 0) + Number(event.gained ?? 0)) * 1000) / 1000;
        this._line("info", `签到 ${event.email} +${event.gained ?? 0} → ${event.after ?? "-"}`);
        break;
      case "checkin_failed":
        this.job.failed = (this.job.failed ?? 0) + 1;
        this._line("warn", `签到失败 ${event.email}: ${event.kind} ${event.reason}`);
        break;
      case "done":
        this.job.summary = event.summary ?? null;
        this.job.created = event.summary?.created ?? this.job.created;
        this.job.failed = event.summary?.failed ?? this.job.failed;
        if (event.summary?.checked !== undefined) this.job.checked = event.summary.checked;
        if (event.summary?.gained !== undefined) this.job.gained = event.summary.gained;
        this._line(
          "info",
          this.job.mode === "checkin"
            ? `签到完成：${this.job.checked ?? 0} 个账号，+${this.job.gained ?? 0} 额度`
            : `完成：成功 ${this.job.created ?? 0}，失败 ${this.job.failed ?? 0}`,
        );
        break;
      default:
        break;
    }
    this._persist();
  }

  /* ------------------------------------------------------------------- api */

  status() {
    const toolPresent = existsSync(join(this.toolDir, "jevai_reg", "cli.py"));
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

  /** 起 Python 子进程 + 逐行吃 NDJSON（run 与 checkin 共用）。 */
  _spawn(args) {
    const child = spawn(this.python, args, {
      cwd: this.toolDir,
      env: { ...process.env, PYTHONUTF8: "1", PYTHONIOENCODING: "utf-8", PYTHONUNBUFFERED: "1" },
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
      // 产物是 Key：跑完立刻导出"给别的工具接入"的描述
      try {
        this.job.apply = this.apply();
        this._line(
          "info",
          `已导出 ${this.job.apply.keys} 把 Key -> ${this.job.apply.files.json}`,
        );
      } catch (error) {
        this._line("error", `导出 Key 失败：${error.message}`);
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
      this.log("info", `jevai: ${status} (${this.job.created ?? 0} accounts, ${this.job.keys ?? 0} keys)`);
    };

    child.on("error", (error) => {
      this._line("error", `无法启动 Python：${error.message}`);
      finish("failed", { error: error.message });
    });
    child.on("close", (code) => {
      finish(code === 0 ? "done" : "failed", { exitCode: code });
    });
    return this.child;
  }
  start(options = {}) {
    if (this.job?.status === "running") {
      const error = new Error("已经有一轮 jev-ai 注册在跑，先停止或等它结束。");
      error.status = 409;
      throw error;
    }
    if (!existsSync(join(this.toolDir, "jevai_reg", "cli.py"))) {
      const error = new Error(`找不到注册工具：${this.toolDir}（可用 JEVAIS_REGISTER_DIR 指定）`);
      error.status = 400;
      throw error;
    }

    const count = clampInt(options.count, 1, 1, 200);
    const concurrency = clampInt(options.concurrency, 1, 1, 8);
    const mailProvider = PROVIDERS.has(options.mailProvider) ? options.mailProvider : "imap";
    const baseUrl = String(options.baseUrl ?? this.baseUrl).trim() || this.baseUrl;
    const mailDomains = list(options.mailDomains, 40).map((d) => d.toLowerCase());
    const proxies = list(options.proxies, 50);
    const keyName = String(options.keyName ?? "").trim();
    const exportFormat = ["keys", "jsonl", "csv", "pool"].includes(options.export) ? String(options.export) : "";

    const args = [
      "-m",
      "jevai_reg",
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
    if (options.checkin === false) args.push("--no-checkin");

    this.job = {
      id: `jevreg-${Date.now().toString(36)}`,
      provider: "jevai",
      status: "running",
      startedAt: new Date().toISOString(),
      finishedAt: null,
      requested: count,
      created: 0,
      updated: 0,
      failed: 0,
      keys: this.pool.keys().length,
      attempts: 0,
      concurrency,
      mailProvider,
      mailDomains,
      verifyEmail: options.verifyEmail !== false,
      checkin: options.checkin !== false,
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
    this.log("info", `jevai: starting ${count} account(s) via ${mailProvider}`);

    this._spawn(args);

    return this.status();
  }

  /**
   * 每日签到：把池子里的免费额度刷一遍（这是 jev 上除了注册赠额外唯一稳定的免费来源）。
   *
   * 走 `jevai_reg checkin`：逐账号 sign-in → 读额度 → POST /api/user/checkin → 读额度 → 落库。
   * `onlyToday` 让当天的重复触权变成空转，cron 一天跑几次也不会把账号玩坏。
   */
  checkin(options = {}) {
    if (!existsSync(join(this.toolDir, "jevai_reg", "cli.py"))) {
      const error = new Error(`找不到注册工具：${this.toolDir}（可用 JEVAIS_REGISTER_DIR 指定）`);
      error.status = 400;
      throw error;
    }
    if (this.job?.status === "running") {
      const error = new Error("有任务在跑（注册或签到），先等它结束。");
      error.status = 409;
      throw error;
    }
    const proxies = list(options.proxies, 50);
    const limit = clampInt(options.limit, 0, 0, 500);
    const pause = Number.isFinite(Number(options.pause)) ? Math.max(0, Number(options.pause)) : 3;
    const args = [
      "-m",
      "jevai_reg",
      "--base-url",
      this.baseUrl,
      "checkin",
      "--json",
      "--pause",
      String(pause),
    ];
    if (options.onlyToday !== false) args.push("--only-today");
    if (limit > 0) args.push("--limit", String(limit));
    for (const proxy of proxies) args.push("--proxy", proxy);

    this.job = {
      id: `jevchk-${Date.now().toString(36)}`,
      provider: "jevai",
      mode: "checkin",
      status: "running",
      startedAt: new Date().toISOString(),
      finishedAt: null,
      requested: this.pool.size,
      created: 0,
      failed: 0,
      keys: this.pool.keys().length,
      checked: 0,
      gained: 0,
      lines: [],
      summary: null,
      exitCode: null,
      error: null,
      apply: null,
    };
    this._persist();
    this.log("info", `jevai: checkin ${this.pool.size} account(s)`);
    this._spawn(args);
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

  /** 自动接入：把 Key 写成 `data/keys/jevai.txt` + `jevai.json`（含 openai_base）。 */
  apply() {
    const keys = this.pool.exportKeys(resolve(this.dataDir, "keys", "jevai.txt"));
    const connection = this.pool.exportConnection(resolve(this.dataDir, "keys", "jevai.json"));
    return {
      keys: keys.rows,
      openaiBase: connection.openai_base,
      files: { txt: keys.file, json: connection.file },
    };
  }

  export({ format = "keys", file = "" } = {}) {
    const stamp = new Date().toISOString().replace(/[:.]/g, "-");
    const ext = format === "csv" ? "csv" : format === "jsonl" ? "jsonl" : format === "pool" ? "json" : "txt";
    const target = file ? resolve(file) : resolve(this.dataDir, "exports", `jevai-${stamp}.${ext}`);
    const result = this.pool.exportTo(target, { format });
    this.log("info", `jevai: exported ${result.rows} row(s) -> ${result.file}`);
    return result;
  }
}
