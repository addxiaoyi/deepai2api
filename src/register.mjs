// Registration controller: drives tools/deepai-reg as a child process and folds
// every account it produces into the gateway's pool.
//
// The Python worker is the only thing that knows how DeepAI's signup works; this
// module just decides what to ask for, streams its NDJSON events to the WebUI,
// and owns the lifecycle (one job at a time, killable, restart-visible).
import { spawn, spawnSync } from "node:child_process";
import { existsSync, mkdirSync, readFileSync, writeFileSync } from "node:fs";
import { dirname, join, resolve } from "node:path";
import { fileURLToPath } from "node:url";

const here = dirname(fileURLToPath(import.meta.url));
const repoRoot = resolve(here, "..");

const MAX_LINES = 240;
const ENGINES = new Set(["auto", "protocol", "browser"]);
const PROVIDERS = new Set(["tempmail_lol", "mailtm", "imap", "fixed"]);

const clampInt = (value, fallback, min, max) => {
  const parsed = Number(value);
  if (!Number.isFinite(parsed)) return fallback;
  return Math.min(max, Math.max(min, Math.trunc(parsed)));
};

export class Registrations {
  constructor({ dataDir, port, token, onAccount, log = () => {} }) {
    this.dataDir = dataDir;
    this.port = port;
    this.token = token;
    this.onAccount = onAccount;
    this.log = log;
    this.statePath = join(dataDir, "register-job.json");
    this.toolDir = process.env.DEEPAI_REGISTER_DIR || join(repoRoot, "tools", "deepai-reg");
    this.python = process.env.DEEPAI_PYTHON || (process.platform === "win32" ? "python" : "python3");
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
      this.log("warn", `register: cannot persist job state: ${error.message}`);
    }
  }

  _line(level, text) {
    if (!this.job) return;
    this.job.lines.push({ at: new Date().toISOString(), level, text: String(text).slice(0, 400) });
    if (this.job.lines.length > MAX_LINES) this.job.lines.splice(0, this.job.lines.length - MAX_LINES);
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
        this._line("info", `worker 启动：${event.engine} × ${event.count}，邮箱 ${event.mail_provider}`);
        break;
      case "attempt":
        this.job.current = { email: event.email, domain: event.domain, provider: event.provider };
        this.job.attempts = (this.job.attempts ?? 0) + 1;
        break;
      case "account": {
        const account = event.account ?? {};
        this.job.created = (this.job.created ?? 0) + 1;
        this.job.domains = mergeDomain(this.job.domains, event.domain, true);
        this._line("info", `+ ${account.email} (${event.engine})`);
        try {
          this.onAccount?.({
            apiKey: account.api_key,
            username: account.username || account.email,
            isPro: account.is_pro === true,
            email: account.email,
            label: account.email,
          });
          this.job.pushed = (this.job.pushed ?? 0) + 1;
        } catch (error) {
          this._line("error", `入池失败 ${account.email}: ${error.message}`);
          this.job.pushFailed = (this.job.pushFailed ?? 0) + 1;
        }
        break;
      }
      case "done":
        this.job.summary = event.summary ?? null;
        this.job.created = event.summary?.created ?? this.job.created;
        this.job.failed = event.summary?.failed ?? this.job.failed;
        if (Array.isArray(event.summary?.mail_domains)) {
          this.job.domainStats = event.summary.mail_domains;
        }
        this._line(
          "info",
          `完成：成功 ${this.job.created ?? 0}，失败 ${this.job.failed ?? 0}，用时 ${event.summary?.seconds ?? "?"}s`,
        );
        break;
      case "push_error":
        this._line("error", `worker 推送失败 ${event.email}: ${event.message}`);
        break;
      default:
        break;
    }
    this._persist();
  }

  /* ------------------------------------------------------------------- api */

  status() {
    if (!this.job) {
      return {
        ok: true,
        running: false,
        job: null,
        python: this.python,
        toolDir: this.toolDir,
        toolPresent: existsSync(join(this.toolDir, "deepai_reg", "cli.py")),
      };
    }
    return {
      ok: true,
      running: this.job.status === "running",
      job: this.job,
      python: this.python,
      toolDir: this.toolDir,
      toolPresent: existsSync(join(this.toolDir, "deepai_reg", "cli.py")),
    };
  }

  start(options = {}) {
    if (this.job?.status === "running") {
      const error = new Error("已经有一轮注册在跑，先停止或等它结束。");
      error.status = 409;
      throw error;
    }
    if (!this.token) {
      const error = new Error("网关没有管理口令，注册进程无法回推账号。用 --token 或 DEEPAI_GATEWAY_TOKEN 启动网关。");
      error.status = 400;
      throw error;
    }
    if (!existsSync(join(this.toolDir, "deepai_reg", "cli.py"))) {
      const error = new Error(`找不到注册工具：${this.toolDir}（可用 DEEPAI_REGISTER_DIR 指定）`);
      error.status = 400;
      throw error;
    }

    const count = clampInt(options.count, 1, 1, 200);
    const concurrency = clampInt(options.concurrency, 1, 1, 8);
    const engine = ENGINES.has(options.engine) ? options.engine : "auto";
    const mailProvider = PROVIDERS.has(options.mailProvider) ? options.mailProvider : "tempmail_lol";
    const mailDomains = (Array.isArray(options.mailDomains) ? options.mailDomains : [])
      .map((d) => String(d).trim().toLowerCase())
      .filter(Boolean)
      .slice(0, 40);
    const proxies = (Array.isArray(options.proxies) ? options.proxies : [])
      .map((p) => String(p).trim())
      .filter(Boolean)
      .slice(0, 20);

    const args = [
      "-m",
      "deepai_reg",
      "run",
      "--json",
      "-n",
      String(count),
      "--engine",
      engine,
      "--concurrency",
      String(concurrency),
      "--mail-provider",
      mailProvider,
      "--push-url",
      `http://127.0.0.1:${this.port}`,
      "--push-token",
      this.token,
    ];
    for (const domain of mailDomains) args.push("--mail-domains", domain);
    for (const proxy of proxies) args.push("--proxy", proxy);
    if (options.verifyEmail === false) args.push("--no-verify");
    if (options.headless === false) args.push("--headful");

    this.job = {
      id: `reg-${Date.now().toString(36)}`,
      status: "running",
      startedAt: new Date().toISOString(),
      finishedAt: null,
      requested: count,
      created: 0,
      failed: 0,
      pushed: 0,
      attempts: 0,
      engine,
      concurrency,
      mailProvider,
      mailDomains,
      proxies: proxies.length,
      lines: [],
      current: null,
      summary: null,
      exitCode: null,
      error: null,
    };
    this._persist();
    this.log("info", `register: starting ${count} account(s) via ${engine}/${mailProvider}`);

    const child = spawn(this.python, args, {
      cwd: this.toolDir,
      env: {
        ...process.env,
        PYTHONUTF8: "1",
        PYTHONIOENCODING: "utf-8",
        PYTHONUNBUFFERED: "1",
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
      this._persist();
      this.child = null;
      this.log("info", `register: ${status} (${this.job.created ?? 0} created)`);
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
}

function mergeDomain(list, domain, ok) {
  if (!domain) return list ?? [];
  const out = Array.isArray(list) ? list : [];
  const found = out.find((row) => row.domain === domain);
  if (found) return out;
  out.push({ domain, ok });
  return out.slice(-40);
}
