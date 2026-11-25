// Allmodels 号池的工具层：一份 JSON Schema + 一份处理函数，三种入口共用。
//
//   node src/allmodels-mcp.mjs                  MCP over stdio（Claude Code / Cursor / Codex / VS Code）
//   POST /api/admin/allmodels/tools/:name       网关 HTTP 桥（任何能发 HTTP 的插件/工作流）
//   node src/allmodels-cli.mjs …                CLI（人类 + 脚本）
//
// 同一个 handler、同一份 schema，避免三个入口各写一遍参数校验。
import { maskAccount, importAccount } from "./allmodels-pool.mjs";

const DEFAULT_WAIT_MS = 120_000;

const int = (description, extra = {}) => ({ type: "integer", description, ...extra });
const str = (description, extra = {}) => ({ type: "string", description, ...extra });
const bool = (description) => ({ type: "boolean", description });

function clampInt(value, fallback, min, max) {
  const parsed = Number(value);
  if (!Number.isFinite(parsed)) return fallback;
  return Math.min(max, Math.max(min, Math.trunc(parsed)));
}

function jobView(job) {
  if (!job) return null;
  return {
    id: job.id,
    status: job.status,
    requested: job.requested,
    created: job.created ?? 0,
    failed: job.failed ?? 0,
    attempts: job.attempts ?? 0,
    engine: job.engine,
    browserStack: job.browserStack,
    captcha: job.captcha,
    mailProvider: job.mailProvider,
    concurrency: job.concurrency,
    startedAt: job.startedAt,
    finishedAt: job.finishedAt ?? null,
    export: job.export ?? null,
    current: job.current ?? null,
    summary: job.summary
      ? {
          created: job.summary.created,
          failed: job.summary.failed,
          seconds: job.summary.seconds,
          stored: job.summary.stored,
          failure_reasons: job.summary.failure_reasons ?? [],
        }
      : null,
    lines: (job.lines ?? []).slice(-40).map((line) => `${line.level.toUpperCase()} ${line.text}`),
  };
}

/** Resolve an account by id, falling back to email. */
function locate(pool, { id, email }) {
  if (id) {
    const found = pool.getAccount(String(id));
    if (found) return found;
  }
  if (email) return pool.byEmail(String(email));
  return null;
}

export class ToolError extends Error {
  constructor(message, code = "invalid_request") {
    super(message);
    this.code = code;
  }
}

export const TOOL_SPECS = [
  {
    name: "allmodels_status",
    title: "Allmodels 号池状态",
    description:
      "查看 Allmodels 号池的统计（账号数/健康/冷却/Key 数）与当前或最近一轮注册任务的状态、失败分类和最近日志。",
    inputSchema: { type: "object", properties: {}, additionalProperties: false },
    async handler({ pool, registrations }) {
      pool.reload();
      return {
        ok: true,
        pool: pool.stats(),
        job: jobView(registrations.status().job),
        baseUrl: pool.baseUrl,
        apiBase: pool.apiBase,
      };
    },
  },
  {
    name: "allmodels_register",
    title: "批量注册 Allmodels 账号",
    description:
      "批量注册 dash.allmodels.io 账号并按账号自动创建网关 sk-dcg- Key、写入号池。" +
      "engine=auto 先用协议、被 Turnstile 拦住自动切指纹浏览器；engine=browser 直接开浏览器" +
      "（camoufox/patchright/playwright，或 cdp 接管外部指纹浏览器）。" +
      "默认立即返回 jobId，用 allmodels_status 轮询；wait=true 则等到结束或超时。",
    inputSchema: {
      type: "object",
      properties: {
        count: int("要注册的账号数", { minimum: 1, maximum: 500, default: 1 }),
        concurrency: int("并发数", { minimum: 1, maximum: 16, default: 2 }),
        engine: str("注册引擎", { enum: ["auto", "protocol", "browser"], default: "auto" }),
        browserStack: str("指纹内核", { enum: ["auto", "camoufox", "patchright", "playwright"], default: "auto" }),
        cdp: str("外部指纹浏览器的 CDP 地址，例如 http://127.0.0.1:50325"),
        headful: bool("显示浏览器窗口（browser 引擎）"),
        mailProvider: str("邮箱来源", { enum: ["tempmail_lol", "mailtm", "imap", "fixed"], default: "tempmail_lol" }),
        email: str("固定邮箱地址（provider=fixed 时自动 plus 分号）"),
        mailDomains: { type: "array", items: { type: "string" }, description: "邮箱域名轮换池" },
        captcha: str("人机校验解题方式", {
          enum: ["none", "capsolver", "2captcha", "browser", "manual"],
          default: "none",
        }),
        captchaKey: str("解题平台 API key（manual 模式下就是 token 本身）"),
        baseUrl: str("面板地址，默认 https://dash.allmodels.io"),
        proxies: { type: "array", items: { type: "string" }, description: "代理列表，按账号轮换" },
        keysPerAccount: int("每个账号创建几个面板 Key", { minimum: 1, maximum: 10, default: 1 }),
        verifyEmail: bool("是否轮询邮箱点验证链接（默认 true）"),
        export: str("任务结束后自动导出", { enum: ["jsonl", "csv", "pool"] }),
        wait: bool("等任务结束再返回（默认 false）"),
        timeoutMs: int("wait=true 时的最长等待", { minimum: 5_000, maximum: 900_000, default: DEFAULT_WAIT_MS }),
      },
      additionalProperties: false,
    },
    async handler({ registrations }, args) {
      const started = registrations.start({
        count: clampInt(args.count, 1, 1, 500),
        concurrency: clampInt(args.concurrency, 2, 1, 16),
        engine: args.engine,
        browserStack: args.browserStack,
        cdp: args.cdp,
        headful: args.headful === true,
        mailProvider: args.mailProvider,
        email: args.email,
        mailDomains: args.mailDomains,
        captcha: args.captcha,
        captchaKey: args.captchaKey,
        baseUrl: args.baseUrl,
        proxies: args.proxies,
        keysPerAccount: args.keysPerAccount,
        verifyEmail: args.verifyEmail,
        export: args.export,
        push: false,
      });
      if (args.wait !== true) {
        return { ok: true, started: true, job: jobView(started.job), hint: "用 allmodels_status 轮询进度" };
      }
      const deadline = Date.now() + clampInt(args.timeoutMs, DEFAULT_WAIT_MS, 5_000, 900_000);
      let status = registrations.status();
      while (status.running && Date.now() < deadline) {
        await new Promise((done) => setTimeout(done, 700));
        status = registrations.status();
      }
      return {
        ok: status.job?.status === "done",
        timedOut: status.running,
        job: jobView(status.job),
      };
    },
  },
  {
    name: "allmodels_pool",
    title: "查看号池账号",
    description: "列出号池里的账号（Key 脱敏），可按状态/邮箱过滤。",
    inputSchema: {
      type: "object",
      properties: {
        limit: int("最多返回多少条", { minimum: 1, maximum: 500, default: 100 }),
        status: str("按状态过滤", { enum: ["active", "revoked", "disabled"] }),
        email: str("按邮箱精确查找"),
        healthyOnly: bool("只看健康可用的账号"),
      },
      additionalProperties: false,
    },
    async handler({ pool }, args) {
      pool.reload();
      let rows = pool.accounts;
      if (args.email) rows = rows.filter((a) => a.email === String(args.email).toLowerCase());
      if (args.status) rows = rows.filter((a) => a.status === args.status);
      if (args.healthyOnly === true) rows = rows.filter((a) => a.healthy !== false && a.status === "active");
      const limit = clampInt(args.limit, 100, 1, 500);
      return {
        ok: true,
        stats: pool.stats(),
        returned: Math.min(rows.length, limit),
        accounts: rows.slice(-limit).map(maskAccount),
      };
    },
  },
  {
    name: "allmodels_export",
    title: "导出账号与 Key",
    description: "把号池账号 + 面板 Key + 网关 Key 导出成一个文件（jsonl / csv / pool），返回文件路径与行数。",
    inputSchema: {
      type: "object",
      properties: {
        format: str("导出格式", { enum: ["jsonl", "csv", "pool"], default: "jsonl" }),
        file: str("目标文件路径；省略则写到 data/exports/"),
      },
      additionalProperties: false,
    },
    async handler({ registrations, pool }, args) {
      pool.reload();
      return { ok: true, ...registrations.export({ format: args.format ?? "jsonl", file: args.file ?? "" }) };
    },
  },
  {
    name: "allmodels_import",
    title: "导入一个账号",
    description:
      "把外部（别的 worker / 手工）注册到的账号并入号池；按 apiKey 幂等，缺失时自动补发网关 sk-dcg- Key。",
    inputSchema: {
      type: "object",
      properties: {
        email: str("账号邮箱"),
        apiKey: str("面板 API Key（sk_live_…）"),
        password: str("面板密码"),
        keyId: str("面板 Key id"),
        keyPrefix: str("面板 Key 前缀"),
        keyLast4: str("面板 Key 后四位"),
        projectId: str("项目 id"),
        guardrailId: str("guardrail id"),
        organizationId: str("组织 id"),
        emailVerified: bool("邮箱是否已验证"),
        engine: str("来源引擎", { enum: ["protocol", "browser"], default: "protocol" }),
        mintKey: bool("是否自动创建网关 Key（默认 true）"),
      },
      required: ["email", "apiKey"],
      additionalProperties: false,
    },
    async handler({ pool, store }, args) {
      pool.reload();
      const apiKey = String(args.apiKey ?? "").trim();
      if (apiKey.length < 16 || /[\r\n]/.test(apiKey)) throw new ToolError("apiKey 不合法");
      const email = String(args.email ?? "").trim().toLowerCase();
      if (!/^[^\s@]+@[^\s@]+\.[^\s@]+$/.test(email)) throw new ToolError("email 不合法");
      const result = importAccount({ pool, store, record: { ...args, email, apiKey }, mintKey: args.mintKey !== false });
      return {
        ok: true,
        created: result.created,
        account: maskAccount(result.account),
        gatewayKey: result.gatewayKey
          ? { id: result.gatewayKey.id, prefix: result.gatewayKey.prefix, label: result.gatewayKey.label }
          : null,
        pool: pool.stats(),
      };
    },
  },
  {
    name: "allmodels_keys",
    title: "列出已发网关 Key",
    description: "列出每个号池账号绑定的面板 Key 元信息与网关 sk-dcg- Key 前缀（不含明文）。",
    inputSchema: { type: "object", properties: {}, additionalProperties: false },
    async handler({ pool }) {
      pool.reload();
      return {
        ok: true,
        keys: pool.accounts.map((account) => ({
          email: account.email,
          keyPrefix: account.keyPrefix,
          keyLast4: account.keyLast4,
          keySource: account.keySource,
          status: account.status,
          gatewayKey: account.gatewayKey
            ? { id: account.gatewayKey.id, label: account.gatewayKey.label, prefix: account.gatewayKey.prefix }
            : null,
        })),
      };
    },
  },
  {
    name: "allmodels_mark_health",
    title: "回报调用结果",
    description: "针对某个账号记录一次上游调用结果；失败会累计次数并进入冷却，调度会跳过冷却中的账号。",
    inputSchema: {
      type: "object",
      properties: {
        id: str("账号 id"),
        email: str("或账号邮箱"),
        ok: bool("这次调用是否成功"),
        error: str("失败原因"),
        cooldownMs: int("失败冷却毫秒", { minimum: 0, default: 60_000 }),
      },
      additionalProperties: false,
    },
    async handler({ pool }, args) {
      pool.reload();
      const account = locate(pool, args);
      if (!account) throw new ToolError("号池里没有这个账号", "not_found");
      const updated = pool.recordResult(account.id, {
        ok: args.ok !== false,
        error: args.error ?? null,
        cooldownMs: clampInt(args.cooldownMs, 60_000, 0, 86_400_000),
      });
      return { ok: true, account: maskAccount(updated), pool: pool.stats() };
    },
  },
  {
    name: "allmodels_remove",
    title: "移除账号",
    description: "把账号从号池移除（不会撤销已经发出去的网关 Key）。",
    inputSchema: {
      type: "object",
      properties: { id: str("账号 id"), email: str("或账号邮箱") },
      additionalProperties: false,
    },
    async handler({ pool }, args) {
      pool.reload();
      const account = locate(pool, args);
      if (!account) throw new ToolError("号池里没有这个账号", "not_found");
      pool.remove(account.id);
      return { ok: true, removed: { id: account.id, email: account.email }, pool: pool.stats() };
    },
  },
  {
    name: "allmodels_probe",
    title: "探测面板可用性",
    description: "检查面板 /api/auth/ok 与 /signup 是否可达（不注册、不消耗额度），用于开工前自检。",
    inputSchema: {
      type: "object",
      properties: { baseUrl: str("面板地址，默认 https://dash.allmodels.io") },
      additionalProperties: false,
    },
    async handler({ pool }, args) {
      const base = String(args.baseUrl ?? pool.baseUrl ?? "https://dash.allmodels.io").replace(/\/+$/, "");
      const started = Date.now();
      const health = await fetch(`${base}/api/auth/ok`, { signal: AbortSignal.timeout(15_000) });
      const page = await fetch(`${base}/signup`, { signal: AbortSignal.timeout(15_000) });
      const body = await health.text();
      return {
        ok: health.ok && body.trim() === '{"ok":true}',
        baseUrl: base,
        auth: { status: health.status, body: body.slice(0, 120) },
        signupPage: { status: page.status },
        ms: Date.now() - started,
      };
    },
  },
];

/** Build the dispatcher for one gateway/pool instance. */
export function createToolRegistry(ctx) {
  const byName = new Map(TOOL_SPECS.map((spec) => [spec.name, spec]));

  return {
    /** MCP `tools/list` shape. */
    list() {
      return TOOL_SPECS.map((spec) => ({
        name: spec.name,
        title: spec.title,
        description: spec.description,
        inputSchema: spec.inputSchema,
      }));
    },

    /** OpenAI / Anthropic function-calling shape, for hosts that prefer it. */
    openai() {
      return TOOL_SPECS.map((spec) => ({
        type: "function",
        function: { name: spec.name, description: spec.description, parameters: spec.inputSchema },
      }));
    },

    async call(name, args = {}) {
      const spec = byName.get(name);
      if (!spec) throw new ToolError(`未知工具：${name}`, "unknown_tool");
      if (args === null || typeof args !== "object" || Array.isArray(args)) {
        throw new ToolError("arguments 必须是对象");
      }
      const missing = (spec.inputSchema.required ?? []).filter((key) => args[key] === undefined);
      if (missing.length) throw new ToolError(`缺少参数：${missing.join(", ")}`, "invalid_args");
      return spec.handler(ctx, args);
    },
  };
}
