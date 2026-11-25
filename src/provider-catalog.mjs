// Provider 描述符注册表 —— 一个 provider 的全部"身份信息"集中在这里。
//
// 网关、号池、注册控制器、反代层、工具层（MCP / HTTP / CLI）都只认这个描述符，
// 所以新增一个 provider 只需要：
//   1) 在本文件加一条 PROVIDERS 条目；
//   2) 在 tools/<name>-reg/ 放一个 Python worker（协议 + NDJSON 事件）；
//   3) 可选：在 tools/<name>-desktop-re/ 放桌面端逆向产物。
//
// 命名约定（决定了文件与路由，必须稳定）：
//   id          —— 小写短名，如 "wavespeed"、"novita"、"allmodels"
//   poolFile    —— 号池文件名，如 "wavespeed-pool.json"
//   lockFile    —— 跨进程注册锁文件名
//   workerDir   —— Python worker 目录（相对仓库根）
//   workerModule—— Python 包名（`-m <workerModule>`）
//   panelBase   —— 面板（注册 / 取 key）地址
//   apiBase     —— OpenAI 兼容推理地址（反代层用）
//   consoleBase —— 控制台地址（浏览器腿导航用）

export const PROVIDER_IDS = ["wavespeed", "novita", "moonai"];

const DEFAULTS = {
  // 面板 / 控制台 / 推理
  panelBase: "",
  consoleBase: "",
  apiBase: "",
  // 文件
  poolFile: "",
  lockFile: "",
  workerDir: "",
  workerModule: "",
  workerEntry: "",
  // 环境变量前缀
  envPrefix: "",
  // 默认工具目录
  toolDirEnv: "",
  pythonEnv: "",
  // 能力开关
  supportsEmailSignup: false,
  supportsOAuthOnly: true,
  supportsKeyListing: false,
  supportsKeyCreationApi: false,
  openAiCompatible: true,
  // 模型目录
  modelsPath: "",
  // 反代默认模型
  defaultModel: "",
  // 展示
  label: "",
};

const describe = (raw) => {
  const id = String(raw.id ?? "").trim().toLowerCase();
  if (!id) throw new Error("provider descriptor needs an id");
  const merged = { ...DEFAULTS, ...raw, id };
  const prefix = merged.envPrefix || id.toUpperCase().replace(/-/g, "_");
  merged.envPrefix = prefix;
  merged.poolFile = merged.poolFile || `${id}-pool.json`;
  merged.lockFile = merged.lockFile || `${id}-register.lock`;
  merged.workerDir = merged.workerDir || `tools/${id}-reg`;
  merged.workerModule = merged.workerModule || `${id.replace(/-/g, "_")}_reg`;
  merged.workerEntry = merged.workerEntry || `${merged.workerDir}/${merged.workerModule}/cli.py`;
  merged.toolDirEnv = merged.toolDirEnv || `${prefix}_REGISTER_DIR`;
  merged.pythonEnv = merged.pythonEnv || `${prefix}_PYTHON`;
  merged.label = merged.label || id;
  return Object.freeze(merged);
};

const RAW = [
  {
    id: "wavespeed",
    label: "WaveSpeedAI",
    panelBase: "https://wavespeed.ai",
    consoleBase: "https://wavespeed.ai",
    apiBase: "https://llm.wavespeed.ai/v1",
    modelsPath: "/v1/models",
    defaultModel: "deepseek/deepseek-v4-flash",
    supportsEmailSignup: false,
    supportsOAuthOnly: true,
    supportsKeyListing: false,
    supportsKeyCreationApi: false,
    openAiCompatible: true,
    signInUrl: "https://wavespeed.ai/sign-in",
    accessKeyUrl: "https://wavespeed.ai/accesskey",
    taskApiBase: "https://api.wavespeed.ai/api/v3",
    docs: "https://wavespeed.ai/docs/get-started-api",
  },
  {
    id: "novita",
    label: "Novita AI",
    panelBase: "https://novita.ai",
    consoleBase: "https://novita.ai",
    // [OI] 兼容推理前缀（SDK 自己再拼 /v1/chat/completions）
    apiBase: "https://api.novita.ai/openai",
    modelsPath: "/v1/models",
    defaultModel: "deepseek/deepseek-v4.1-flash",
    supportsEmailSignup: true,
    supportsOAuthOnly: false,
    // 面板自己有一套可用的 Key 接口（见 panelApi），OpenAPI 那边只能读。
    supportsKeyListing: true,
    supportsKeyCreationApi: true,
    openAiCompatible: true,
    signInUrl: "https://novita.ai/user/login",
    registerUrl: "https://novita.ai/user/register",
    accessKeyUrl: "https://novita.ai/settings/key-management",
    taskApiBase: "https://api.novita.ai",
    docs: "https://docs.novita.ai/guides/quickstart",

    // ---- 从线上 SPA chunk 反查出来的真实面板接口（不是猜测）----
    // 前端常量：I = "https://api-server.novita.ai"（T({base_url: I})）
    // 注册：a.kz({email,password,confirmPassword,cloudflareToken,redirectUrl,
    //            allowNotification,fromInviteCode,agreementVersion,utm*})
    // 登录：s.iD({email,password,cloudflareToken,redirectUrl,fromInviteCode,agreementVersion})
    // Key ：POST /v2/user/key（body 里 expireTime 先经 QP() 归一）
    panelApiBase: "https://api-server.novita.ai",
    panelApi: {
      register: { method: "POST", path: "/v1/user/register" },
      login: { method: "POST", path: "/v1/user/login" },
      info: { method: "GET", path: "/v1/user/info" },
      verifyEmail: { method: "POST", path: "/v1/user/email/verify" },
      keyList: { method: "GET", path: "/v2/user/key" },
      keyCreate: { method: "POST", path: "/v2/user/key" },
      keyUpdate: { method: "PUT", path: "/v2/user/key" },
      keyDelete: { method: "DELETE", path: "/v2/user/key/" },
    },
    // Turnstile sitekey，来自 chunk 99745: fE = "0x4AAAAAAAaG28VfN_OxkED8"
    turnstileSiteKey: "0x4AAAAAAAaG28VfN_OxkED8",
    turnstileTokenField: "cloudflareToken",
    anthropicBase: "https://api.novita.ai/anthropic",
  },
  {
    // ------------------------------------------------------------------ moonai
    // mooonai.com（MoonAI，KZ 的 AI 销售代理 SaaS）。它的"对外 API Key"（api_…）
    // 是 Agent 渠道 Webhook 用的，**不能**直接调模型；能调模型的是
    // `/api/v1/services/llm-proxy/chat/completions`，鉴权是 **用户 OAuth access_token**。
    //
    // 所以这个 provider 的形态和 wavespeed / novita 不同：
    //   面板注册 → 邮箱验证 → OAuth code → access_token（86400s + refresh_token）
    //   → 建组织 → 建 api_ Key（记账/留档）→ 反代层用 access_token 调 llm-proxy。
    //
    // 上游不是 OpenAI 形状（返回 {content, finish_reason, usage}，不接受 stream /
    // tools / stop / n / user / penalties），翻译在 src/moonai-adapter.mjs。
    id: "moonai",
    label: "MoonAI",
    panelBase: "https://auth.mooonai.com",
    consoleBase: "https://dashboard.mooonai.com",
    // llm-proxy 的 host（不是 /v1 前缀，路径整段由 relay.path 给）
    apiBase: "https://core.mooonai.com",
    modelsPath: "",
    defaultModel: "gpt-4o-mini",
    supportsEmailSignup: true,
    supportsOAuthOnly: false,
    supportsKeyListing: true,
    supportsKeyCreationApi: true,
    openAiCompatible: false,
    signInUrl: "https://auth.mooonai.com/auth/signin/",
    registerUrl: "https://auth.mooonai.com/auth/signup/",
    accessKeyUrl: "https://dashboard.mooonai.com/settings/api-keys",
    docs: "https://docs.mooonai.com/",

    // ---- 线上实测锁定（2026-11，见 docs/MOONAI-POOL.md 的取证表）----
    models: ["gpt-4.1", "gpt-4o", "gpt-4o-mini", "gpt-5-nano", "o3-mini", "o4-mini"],
    // authentik OAuth client（从 core /api/v1/auth/url 返回的 auth_url 里读到的）
    oauth: {
      clientId: "xsFdCzICUafHTwslFEf6H1kAroErD7SRUvPzI6Gj",
      authorizationPath: "/oauth2/authorize/",
      // 关键：默认回调是 /oauth，不是首页 —— 传别的 redirect_url 一律
      // "Mismatching redirect URI"（400）。
      redirectUri: "https://dashboard.mooonai.com/oauth",
      scope: "openid introspection email profile groups",
    },
    // 后端（Django + DRF）路径，注册机与反代层共用同一张表
    panelApi: {
      signup: { method: "GET|POST", path: "/auth/signup/" },
      verifyEmail: { method: "GET", path: "/auth/verify-email/?token=" },
      signin: { method: "GET|POST", path: "/auth/signin/" },
      token: { method: "POST", path: "/api/v1/auth/token" },
      refresh: { method: "POST", path: "/api/v1/auth/refresh" },
      userinfo: { method: "POST", path: "/api/v1/auth/userinfo" },
      organizations: { method: "GET|POST", path: "/api/v1/organizations" },
      organizationList: { method: "GET", path: "/api/v1/organizations/" },
      apiKeys: { method: "GET|POST", path: "/api/v1/organizations/{org}/api-keys" },
      llmProxy: { method: "POST", path: "/api/v1/services/llm-proxy/chat/completions" },
      llmValidation: { method: "POST", path: "/api/v1/llm-validation" },
    },
    // 反代层怎么把 OpenAI 请求变成 llm-proxy 请求
    relay: {
      kind: "moonai-llm-proxy",
      path: "/api/v1/services/llm-proxy/chat/completions",
      authMode: "user-token", // Bearer <access_token>，不是 api_ Key
      refreshPath: "/api/v1/auth/refresh",
      refreshWithinMs: 10 * 60 * 1000, // 提前 10 分钟续期
      synthesizeStream: true, // 上游不支持 stream，SSE 由网关合成
      emulateTools: true, // 上游不接受 tools，用提示词模拟再翻译回来
      // 上游 pydantic 是 extra_forbidden，这些字段必须先剥掉
      dropFields: [
        "stream",
        "stream_options",
        "tools",
        "tool_choice",
        "parallel_tool_calls",
        "stop",
        "n",
        "user",
        "frequency_penalty",
        "presence_penalty",
        "logit_bias",
        "logprobs",
        "top_logprobs",
        "response_format",
        "seed",
        "metadata",
        "store",
        "modalities",
        "audio",
        "reasoning",
        "reasoning_effort",
        "service_tier",
        "verbosity",
      ],
      allowedFields: ["model", "messages", "temperature", "top_p", "max_tokens", "max_completion_tokens"],
    },
  },
];

export const PROVIDERS = Object.freeze(RAW.map(describe));

const BY_ID = new Map(PROVIDERS.map((p) => [p.id, p]));

export function getProvider(id) {
  const key = String(id ?? "").trim().toLowerCase();
  const found = BY_ID.get(key);
  if (!found) {
    const error = new Error(`未知 provider：${id}（可用：${PROVIDERS.map((p) => p.id).join(", ")}）`);
    error.status = 400;
    error.code = "unknown_provider";
    throw error;
  }
  return found;
}

export function hasProvider(id) {
  return BY_ID.has(String(id ?? "").trim().toLowerCase());
}

/** 每个 provider 的默认端点，环境变量可以逐项覆盖。 */
export function resolveEndpoints(provider) {
  const env = process.env;
  const prefix = provider.envPrefix;
  return {
    panelBase: env[`${prefix}_PANEL_BASE`] || env[`${prefix}_BASE_URL`] || provider.panelBase,
    consoleBase: env[`${prefix}_CONSOLE_BASE`] || provider.consoleBase,
    apiBase: env[`${prefix}_API_BASE`] || provider.apiBase,
    taskApiBase: env[`${prefix}_TASK_API_BASE`] || provider.taskApiBase || "",
  };
}

/** 供 MCP / CLI / 管理台展示用的精简视图。 */
export function providerSummary(provider) {
  const endpoints = resolveEndpoints(provider);
  return {
    id: provider.id,
    label: provider.label,
    panelBase: endpoints.panelBase,
    consoleBase: endpoints.consoleBase,
    apiBase: endpoints.apiBase,
    taskApiBase: endpoints.taskApiBase,
    modelsPath: provider.modelsPath,
    defaultModel: provider.defaultModel,
    openAiCompatible: provider.openAiCompatible,
    supportsEmailSignup: provider.supportsEmailSignup,
    supportsOAuthOnly: provider.supportsOAuthOnly,
    supportsKeyListing: provider.supportsKeyListing,
    supportsKeyCreationApi: provider.supportsKeyCreationApi,
    signInUrl: provider.signInUrl || "",
    registerUrl: provider.registerUrl || "",
    accessKeyUrl: provider.accessKeyUrl || "",
    docs: provider.docs || "",
    // moonai 这类"自有协议"的 provider 需要额外声明，管理台/CLI 直接读这里
    models: Array.isArray(provider.models) ? [...provider.models] : [],
    relay: provider.relay
      ? {
          kind: provider.relay.kind,
          path: provider.relay.path,
          authMode: provider.relay.authMode,
          synthesizeStream: provider.relay.synthesizeStream === true,
          emulateTools: provider.relay.emulateTools === true,
        }
      : null,
    panelApi: provider.panelApi ?? null,
  };
}
