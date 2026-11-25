// Provider 工具层：一份 JSON Schema + 一份 handler，三个入口共用。
//
//   node src/provider-mcp.mjs                 MCP over stdio（Claude Code / Cursor / Codex / VS Code）
//   POST /api/admin/providers/:id/*           网关 HTTP 桥（见 provider-admin.mjs）
//   node src/provider-cli.mjs …              CLI（人 + 脚本）
//
// 覆盖 catalog 里的每一个 provider（含 wavespeed / novita），不存在"某个 provider
// 有工具、另一个没有"的情况：所有名字都带 provider 参数，默认取 catalog 第一项。
import { maskAccount, importAccount } from "./provider-pool.mjs";
import { PROVIDERS, getProvider, providerSummary } from "./provider-catalog.mjs";

const DEFAULT_WAIT_MS = 120_000;

const int = (description, extra = {}) => ({ type: "integer", description, ...extra });
const str = (description, extra = {}) => ({ type: "string", description, ...extra });
const bool = (description) => ({ type: "boolean", description });
const providerArg = () =>
  str("provider id（默认全部；注册类工具必填）", { enum: PROVIDERS.map((p) => p.id) });

function clampInt(value, fallback, min, max) {
  const parsed = Number(value);
  if (!Number.isFinite(parsed)) return fallback;
  return Math.min(max, Math.max(min, Math.trunc(parsed)));
}

function jobView(job) {
  if (!job) return null;
  return {
    id: job.id,
    provider: job.provider,
    status: job.status,
    requested: job.requested,
    created: job.created ?? 0,
    failed: job.failed ?? 0,
    attempts: job.attempts ?? 0,
    engine: job.engine,
    mailProvider: job.mailProvider,
    mailDomains: job.mailDomains ?? [],
    captcha: job.captcha,
    concurrency: job.concurrency,
    antiBan: job.antiBan ?? null,
    keyPolicy: job.keyPolicy ?? null,
    startedAt: job.startedAt,
    finishedAt: job.finishedAt ?? null,
    export: job.export ?? null,
    current: job.current ?? null,
    failureReasons: job.failureReasons ?? {},
    domainHealth: job.domainHealth ?? job.domains ?? [],
    lines: (job.lines ?? []).slice(-40).map((line) => `${String(line.level).toUpperCase()} ${line.text}`),
  };
}

export class ToolError extends Error {
  constructor(message, code = "invalid_request") {
    super(message);
    this.code = code;
  }
}

/** 解析一个 provider：省略时若只有一个可用则用它，否则要求显式指定。 */
function resolve(ctx, providerId, { allowAll = false } = {}) {
  const id = String(providerId ?? "").trim().toLowerCase();
  if (!id) {
    if (allowAll) return null;
    const available = [...ctx.runtime.pools.keys()];
    if (available.length === 1) return ctx.runtime.requireProvider(available[0]);
    throw new ToolError(`需要指定 provider（可选：${available.join(", ")}）`, "missing_provider");
  }
  try {
    return ctx.runtime.requireProvider(id);
  } catch (error) {
    throw new ToolError(error.message, error.code ?? "unknown_provider");
  }
}

function locate(pool, { id, email }) {
  if (id) {
    const found = pool.getAccount(String(id));
    if (found) return found;
  }
  if (email) return pool.byEmail(String(email));
  return null;
}

export const TOOL_SPECS = [
  {
    name: "provider_list",
    title: "列出所有 provider",
    description:
      "列出 catalog 里的每个 provider（wavespeed / novita / …）及其号池统计、是否在跑注册、" +
      "worker 是否就位、OpenAI 兼容前缀。开工前先看一眼。",
    inputSchema: { type: "object", properties: {}, additionalProperties: false },
    async handler({ runtime }) {
      return {
        ok: true,
        providers: runtime.list().map((row) => ({
          ...row,
          openaiBase: `/p/${row.id}/v1`,
          poolFile: `${row.id}-pool.json`,
        })),
      };
    },
  },
  {
    name: "provider_status",
    title: "查看某个 provider 的状态",
    description: "号池统计 + 当前/最近一轮注册任务的进度、失败分类与最近 40 行日志。",
    inputSchema: {
      type: "object",
      properties: { provider: providerArg() },
      additionalProperties: false,
    },
    async handler(ctx, args) {
      const { provider, pool, registrations } = resolve(ctx, args.provider);
      const status = registrations.status();
      return {
        ok: true,
        provider: provider.id,
        summary: providerSummary(provider),
        pool: pool.stats(),
        job: jobView(status.job),
        worker: { present: status.toolPresent, dir: status.toolDir, python: status.python },
      };
    },
  },
  {
    name: "provider_register",
    title: "批量注册账号",
    description:
      "按 provider 批量注册并自动创建网关 sk-dcg- Key、写入该 provider 的独立号池。" +
      "engine=auto 先用协议、被拦自动切指纹浏览器；browser 直接开浏览器" +
      "（camoufox/patchright/playwright，或 cdp 接管外部指纹浏览器）。" +
      "域名邮箱用 mailProvider=imap + mailDomains 指定自有域名。" +
      "防封策略通过 antiBan 传（域名速率窗、每 IP 串行化、指纹地理对齐、失败退避）。" +
      "默认立即返回 jobId，用 provider_status 轮询；wait=true 则等到结束或超时。",
    inputSchema: {
      type: "object",
      properties: {
        provider: providerArg(),
        count: int("要注册的账号数", { minimum: 1, maximum: 500, default: 1 }),
        concurrency: int("并发数（同 IP 建议 1）", { minimum: 1, maximum: 16, default: 2 }),
        engine: str("注册引擎", { enum: ["auto", "protocol", "browser"], default: "auto" }),
        browserStack: str("指纹内核", { enum: ["auto", "camoufox", "patchright", "playwright"], default: "auto" }),
        cdp: str("外部指纹浏览器的 CDP 地址，例如 http://127.0.0.1:50325"),
        headful: bool("显示浏览器窗口（browser 引擎）"),
        mailProvider: str("邮箱来源", { enum: ["imap", "domain", "mailtm", "tempmail_lol", "fixed"], default: "imap" }),
        email: str("固定邮箱地址（provider=fixed 时自动 plus 分号）"),
        mailDomains: { type: "array", items: { type: "string" }, description: "域名邮箱轮换池" },
        captcha: str("人机校验解题方式", {
          enum: ["auto", "none", "capsolver", "2captcha", "browser", "manual"],
          default: "auto",
        }),
        captchaKey: str("解题平台 API key（manual 模式下就是 token 本身）"),
        baseUrl: str("面板地址，省略用 catalog 默认值"),
        apiBase: str("推理基址，省略用 catalog 默认值"),
        proxies: { type: "array", items: { type: "string" }, description: "代理列表，按账号轮换" },
        keysPerAccount: int("每个账号创建几个面板 Key", { minimum: 1, maximum: 10, default: 1 }),
        antiBan: { type: "object", description: "防封策略覆盖，见 docs/PROVIDERS.md" },
        keyPolicy: { type: "object", description: "key 策略（per_account / expire_time / open_model_access）" },
        verifyEmail: bool("是否轮询邮箱点验证链接（默认 true）"),
        // 只有声明了 workerFlags 的 provider 才吃这几项（blackbox 这类 OAuth 线）
        sessionFile: str("复用登录会话的 cookies JSON 路径（blackbox 协议腿）"),
        googlePool: str("Google 身份池 JSONL 路径（blackbox 浏览器腿）"),
        keysPath: str("覆盖面板 Key 列表接口路径（探测失败时用）"),
        keysCreatePath: str("覆盖面板建 Key 接口路径"),
        export: str("任务结束后自动导出", { enum: ["jsonl", "csv", "pool"] }),
        wait: bool("等任务结束再返回（默认 false）"),
        timeoutMs: int("wait=true 时的最长等待", { minimum: 5_000, maximum: 900_000, default: DEFAULT_WAIT_MS }),
      },
      required: ["provider"],
      additionalProperties: false,
    },
    async handler(ctx, args) {
      const { provider, registrations } = resolve(ctx, args.provider);
      let started;
      try {
        started = registrations.start({
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
          apiBase: args.apiBase,
          proxies: args.proxies,
          keysPerAccount: args.keysPerAccount,
          antiBan: args.antiBan,
          keyPolicy: args.keyPolicy,
          verifyEmail: args.verifyEmail,
          export: args.export,
          // provider 描述符里 workerFlags 白名单过滤后才会真正拼进命令行，
          // 所以给 novita / moonai 传这几个字段是安全的（会被丢掉）。
          workerArgs: {
            "--session-file": args.sessionFile,
            "--google-pool": args.googlePool,
            "--keys-path": args.keysPath,
            "--keys-create-path": args.keysCreatePath,
          },
          push: false,
        });
      } catch (error) {
        throw new ToolError(error.message, error.code ?? "register_start_failed");
      }

      if (args.wait !== true) {
        return {
          ok: true,
          provider: provider.id,
          started: true,
          job: jobView(started.job),
          hint: `用 provider_status（provider=${provider.id}）轮询进度`,
        };
      }

      const deadline = Date.now() + clampInt(args.timeoutMs, DEFAULT_WAIT_MS, 5_000, 900_000);
      let status = registrations.status();
      while (status.job?.status === "running" && Date.now() < deadline) {
        await new Promise((done) => setTimeout(done, 700));
        status = registrations.status();
      }
      const finished = registrations.status();
      return {
        ok: finished.job?.status === "done",
        provider: provider.id,
        timedOut: finished.job?.status === "running",
        job: jobView(finished.job),
        pool: finished.pool ?? ctx.runtime.pools.get(provider.id)?.stats(),
      };
    },
  },
  {
    name: "provider_register_stop",
    title: "停止注册",
    description: "强杀当前 provider 的注册 worker（taskkill /T /F）。",
    inputSchema: {
      type: "object",
      properties: { provider: providerArg() },
      required: ["provider"],
      additionalProperties: false,
    },
    async handler(ctx, args) {
      const { provider, registrations } = resolve(ctx, args.provider);
      return { ok: true, provider: provider.id, ...registrations.stop() };
    },
  },
  {
    name: "provider_pool",
    title: "查看号池账号",
    description: "列出某个 provider 号池里的账号（面板 Key 脱敏），可按状态/邮箱过滤。",
    inputSchema: {
      type: "object",
      properties: {
        provider: providerArg(),
        limit: int("最多返回多少条", { minimum: 1, maximum: 500, default: 100 }),
        status: str("按状态过滤", { enum: ["active", "revoked", "disabled"] }),
        email: str("按邮箱精确查找"),
        healthyOnly: bool("只看健康可用的账号"),
      },
      required: ["provider"],
      additionalProperties: false,
    },
    async handler(ctx, args) {
      const { provider, pool } = resolve(ctx, args.provider);
      let rows = pool.accounts;
      if (args.email) rows = rows.filter((a) => a.email === String(args.email).toLowerCase());
      if (args.status) rows = rows.filter((a) => a.status === args.status);
      if (args.healthyOnly === true) rows = rows.filter((a) => a.healthy !== false && a.status === "active");
      const limit = clampInt(args.limit, 100, 1, 500);
      return {
        ok: true,
        provider: provider.id,
        stats: pool.stats(),
        returned: Math.min(rows.length, limit),
        accounts: rows.slice(-limit).map(maskAccount),
      };
    },
  },
  {
    name: "provider_export",
    title: "导出账号与 Key",
    description: "把某个 provider 的账号 + 面板 Key + 网关 Key 导出成一个文件（jsonl / csv / pool）。",
    inputSchema: {
      type: "object",
      properties: {
        provider: providerArg(),
        format: str("导出格式", { enum: ["jsonl", "csv", "pool"], default: "jsonl" }),
        file: str("目标文件路径；省略则写到 data/exports/"),
      },
      required: ["provider"],
      additionalProperties: false,
    },
    async handler(ctx, args) {
      const { provider, registrations } = resolve(ctx, args.provider);
      return {
        ok: true,
        provider: provider.id,
        ...registrations.export({ format: args.format ?? "jsonl", file: args.file ?? "" }),
      };
    },
  },
  {
    name: "provider_import",
    title: "导入一个账号",
    description:
      "把外部（别的 worker / 手工）注册到的账号并入号池；按 apiKey 幂等，" +
      "缺失时自动补发网关 sk-dcg- Key。",
    inputSchema: {
      type: "object",
      properties: {
        provider: providerArg(),
        email: str("账号邮箱"),
        apiKey: str("面板 API Key"),
        password: str("面板密码"),
        keyId: str("面板 Key id"),
        keyPrefix: str("面板 Key 前缀"),
        keyLast4: str("面板 Key 后四位"),
        expireTime: str("Key 过期时间（novita 有效）"),
        emailVerified: bool("邮箱是否已验证"),
        engine: str("来源引擎", { enum: ["protocol", "browser", "manual"], default: "protocol" }),
        mintKey: bool("是否自动创建网关 Key（默认 true）"),
      },
      required: ["provider", "email", "apiKey"],
      additionalProperties: false,
    },
    async handler(ctx, args) {
      const { provider, pool } = resolve(ctx, args.provider);
      const apiKey = String(args.apiKey ?? "").trim();
      if (apiKey.length < 16 || /[\r\n]/.test(apiKey)) throw new ToolError("apiKey 不合法");
      const email = String(args.email ?? "").trim().toLowerCase();
      if (!/^[^\s@]+@[^\s@]+\.[^\s@]+$/.test(email)) throw new ToolError("email 不合法");
      const result = importAccount({
        pool,
        store: ctx.store,
        record: { ...args, email, apiKey },
        mintKey: args.mintKey !== false,
      });
      return {
        ok: true,
        provider: provider.id,
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
    name: "provider_mark_health",
    title: "回报调用结果",
    description: "针对某个账号记录一次上游调用结果；失败会累计次数并进入冷却，调度会跳过冷却中的账号。",
    inputSchema: {
      type: "object",
      properties: {
        provider: providerArg(),
        id: str("账号 id"),
        email: str("或账号邮箱"),
        ok: bool("这次调用是否成功"),
        error: str("失败原因"),
        cooldownMs: int("失败冷却毫秒", { minimum: 0, default: 60_000 }),
      },
      required: ["provider"],
      additionalProperties: false,
    },
    async handler(ctx, args) {
      const { provider, pool } = resolve(ctx, args.provider);
      const account = locate(pool, args);
      if (!account) throw new ToolError("号池里没有这个账号", "not_found");
      const updated = pool.recordResult(account.id, {
        ok: args.ok !== false,
        error: args.error ?? null,
        cooldownMs: clampInt(args.cooldownMs, 60_000, 0, 86_400_000),
      });
      return { ok: true, provider: provider.id, account: maskAccount(updated), pool: pool.stats() };
    },
  },
  {
    name: "provider_remove",
    title: "移除账号",
    description: "把账号从号池移除（不会撤销已经发出去的网关 Key）。",
    inputSchema: {
      type: "object",
      properties: { provider: providerArg(), id: str("账号 id"), email: str("或账号邮箱") },
      required: ["provider"],
      additionalProperties: false,
    },
    async handler(ctx, args) {
      const { provider, pool } = resolve(ctx, args.provider);
      const account = locate(pool, args);
      if (!account) throw new ToolError("号池里没有这个账号", "not_found");
      pool.remove(account.id);
      return { ok: true, provider: provider.id, removed: { id: account.id, email: account.email }, pool: pool.stats() };
    },
  },
  {
    name: "provider_probe",
    title: "探测上游可用性",
    description: "拿号池里一把 key 打上游 /models，确认面板与 Key 都还活着（不注册、不消耗额度）。",
    inputSchema: {
      type: "object",
      properties: { provider: providerArg() },
      required: ["provider"],
      additionalProperties: false,
    },
    async handler(ctx, args) {
      const { provider } = resolve(ctx, args.provider);
      const result = await ctx.runtime.probe(provider.id);
      return { ok: result.ok === true, provider: provider.id, ...result };
    },
  },
  {
    name: "provider_relay_info",
    title: "取得反代接入信息",
    description:
      "返回某个 provider 的 OpenAI 兼容接入地址与可用网关 Key 前缀 —— " +
      "把 «base_url + Bearer sk-dcg-…» 填进任何 OpenAI SDK / 编程工具即可用该号池。" +
      "同时给出 Anthropic 兼容路径与异步任务路径。",
    inputSchema: {
      type: "object",
      properties: {
        provider: providerArg(),
        publicBase: str("网关对外地址；省略用 http://127.0.0.1:<port>"),
      },
      required: ["provider"],
      additionalProperties: false,
    },
    async handler(ctx, args) {
      const { provider, pool } = resolve(ctx, args.provider);
      const base = String(args.publicBase ?? ctx.config?.publicBase ?? `http://127.0.0.1:${ctx.config?.port ?? 8787}`)
        .replace(/\/+$/, "");
      const keys = pool.accounts
        .filter((account) => account.gatewayKey)
        .slice(-10)
        .map((account) => ({
          email: account.email,
          prefix: account.gatewayKey.prefix,
          label: account.gatewayKey.label,
        }));
      return {
        ok: true,
        provider: provider.id,
        baseUrl: `${base}/p/${provider.id}/v1`,
        chatCompletions: `${base}/p/${provider.id}/v1/chat/completions`,
        models: `${base}/p/${provider.id}/v1/models`,
        anthropicMessages: `${base}/p/${provider.id}/v1/messages`,
        asyncSubmit: `${base}/p/${provider.id}/v1/async/<task-path>`,
        asyncResult: `${base}/p/${provider.id}/v1/async/result?id=<task-id>`,
        auth: "Authorization: Bearer sk-dcg-…",
        gatewayKeys: keys,
        note: keys.length
          ? "网关 Key 的明文只在导出文件或号池文件里；这里只给前缀。"
          : "号池里还没有带网关 Key 的账号，先跑一轮 provider_register。",
      };
    },
  },
  {
    name: "provider_antiban",
    title: "查看/调整防封策略",
    description:
      "读取某个 provider 当前生效的防封默认值（域名速率窗、每 IP 并发、指纹地理对齐、" +
      "失败退避表、会话复用、多账号隔离）。改动用 provider_register 的 antiBan 传入。",
    inputSchema: {
      type: "object",
      properties: { provider: providerArg() },
      required: ["provider"],
      additionalProperties: false,
    },
    async handler(ctx, args) {
      const { provider, registrations } = resolve(ctx, args.provider);
      const status = registrations.status();
      return {
        ok: true,
        provider: provider.id,
        defaults: ctx.defaultAntiBan ?? null,
        active: status.job?.antiBan ?? null,
      };
    },
  },
];

/** 建一个工具分发器。 */
export function createProviderToolRegistry(ctx) {
  const byName = new Map(TOOL_SPECS.map((spec) => [spec.name, spec]));

  return {
    list() {
      return TOOL_SPECS.map((spec) => ({
        name: spec.name,
        title: spec.title,
        description: spec.description,
        inputSchema: spec.inputSchema,
      }));
    },

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

export { getProvider, PROVIDERS };
