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

export const PROVIDER_IDS = ["wavespeed", "novita", "moonai", "zo", "blackbox"];

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
  {
    // --------------------------------------------------------------------- zo
    // zo.computer（Zo Computer，云端常驻 Linux + 常驻 agent）。形态和前面几个都不同：
    //
    //   1) **无密码**。登录只有 Google SSO 与邮件魔法链（magic link）两条腿：
    //        POST {panel}/api/email-login/request  {email, redirect, desktop}
    //        → 邮件里 https://www.zo.computer/email-login/verify?token=…
    //        → POST {panel}/api/email-login/confirm?token=…（可带 turnstileToken）
    //        → Set-Cookie: access_token / refresh_token（Domain=.zo.computer）
    //   2) **注册 = 建工作区**：拿到会话后 POST {api}/signup/ {handle}，
    //      handle 相当于子域/用户名，且要先 GET {api}/signup/{handle}/available 探占用。
    //   3) **对外凭据是 access token**（Settings → Advanced 里生成，形如 zo_sk_…），
    //      不是 OpenAI Key；接口面也只有四个：
    //        POST {api}/zo/ask                对话（SSE 事件 FrontendModelResponse）
    //        GET  {api}/models/available      可用模型（含 BYOK）
    //        GET  {api}/models/catalog        全量目录（公开，无需鉴权）
    //        GET  {api}/personas/available    persona 列表
    //        POST {api}/mcp                   MCP over HTTP（tools/list、tools/call）
    //   4) 所以反代层要做的不是"透传"，而是把 OpenAI / Anthropic 的 chat 形状翻译成
    //      `{input, model_name, stream}`，并把 MCP 工具暴露给编程工具。翻译在
    //      src/zo-adapter.mjs，`relay.kind = "zo-ask"` 时由 provider-relay 调用。
    id: "zo",
    label: "Zo Computer",
    panelBase: "https://www.zo.computer",
    consoleBase: "https://www.zo.computer",
    apiBase: "https://api.zo.computer",
    modelsPath: "/models/available",
    defaultModel: "zo:openai/gpt-6.2-astra",
    supportsEmailSignup: true,
    supportsOAuthOnly: false,
    // access token 由 /api-keys/ 创建，能列也能建（形状见 docs/ZO-POOL.md）
    supportsKeyListing: true,
    supportsKeyCreationApi: true,
    // 上游不是 OpenAI 形状：只有 /zo/ask 一个对话入口，且流式是自有 SSE 事件
    openAiCompatible: false,
    signInUrl: "https://www.zo.computer/signup",
    registerUrl: "https://www.zo.computer/signup",
    accessKeyUrl: "https://www.zo.computer/settings/advanced",
    docs: "https://www.zo.computer/reference/api-reference",
    desktop: {
      // 桌面端（Electron）把 web 版整个装进壳里，UA 带 `Zo/…`，
      // 前端用 /Zo\//.test(navigator.userAgent) 判断"是不是桌面端"。
      userAgentMarker: "Zo/",
      deepLink: "zo://email-login",
      deepLinkQuery: ["token", "turnstile"],
      releases: {
        mac: "https://github.com/zocomputer/Zo/releases/latest/download/Zo.dmg",
        windows: "https://github.com/zocomputer/Zo/releases/latest/download/Zo-Setup.exe",
        linuxDeb: "https://github.com/zocomputer/Zo/releases/latest/download/Zo.deb",
        linuxDebArm64: "https://github.com/zocomputer/Zo/releases/latest/download/Zo-arm64.deb",
      },
    },
    // ---- 线上实测（2026-11，证据见 docs/ZO-POOL.md 取证表 + tools/zo-reg/_re/）----
    turnstileSiteKey: "0x4AAAAAADTRT4Uvmz3G7YH5",
    mcpPath: "/mcp",
    panelApi: {
      emailLogin: { method: "POST", path: "/api/email-login/request" },
      confirm: { method: "POST", path: "/api/email-login/confirm" },
      verifyPage: { method: "GET", path: "/email-login/verify" },
      loginState: { method: "GET", path: "/api/login-state" },
      logout: { method: "POST", path: "/logout" },
      signupAvailable: { method: "GET", path: "/signup/{handle}/available" },
      signup: { method: "POST", path: "/signup/" },
      apiKeys: { method: "GET|POST", path: "/api-keys/" },
      modelsAvailable: { method: "GET", path: "/models/available" },
      modelsCatalog: { method: "GET", path: "/models/catalog" },
      personas: { method: "GET", path: "/personas/available" },
      ask: { method: "POST", path: "/zo/ask" },
      mcp: { method: "POST", path: "/mcp" },
      byok: { method: "GET", path: "/byok/" },
      settings: { method: "GET", path: "/settings/" },
    },
    relay: {
      kind: "zo-ask",
      path: "/zo/ask",
      authMode: "user-token", // Bearer <access token>（zo_sk_…）
      mcpPath: "/mcp",
      // 上游返回的是 Zo agent 的整段输出（工具调用已经在 Zo 侧跑完），
      // 所以只需要把 SSE 事件折成 OpenAI chunk，不需要自己合成"假工具调用"。
      synthesizeStream: true,
      emulateTools: true,
      modelsPath: "/models/available",
      // /zo/ask 只认这几个字段；多一个都别传（pydantic 校验）
      allowedFields: ["input", "conversation_id", "model_name", "persona_id", "output_format", "memory_mode", "stream"],
    },
  },
  {
    // ---------------------------------------------------------------- blackbox
    // blackbox.ai（BLACKBOX AI，#1 AI Coding Agent）。形态与其它 provider 都不一样：
    //
    //   1) **没有邮箱密码注册**。app.blackbox.ai 是 Auth.js(NextAuth v5) + **只有
    //      Google 一个 provider**（`GET /api/auth/providers` 只回 google，实测
    //      `POST /api/auth/sign-up/email` 恒 400 "Bad request."）。所以"注册"= 用
    //      Google 身份走一次 OAuth 授权，落地成 app 侧的 `__Secure-authjs.session-token`。
    //   2) **域名邮箱的作用**因此是"身份来源"而不是"注册表单字段"：自有域 catch-all
    //      信箱收 Google 的安全提醒 / 验证码，以及 console 那条 OTP 腿的一次性码。
    //   3) **Key 在面板里**（app.blackbox.ai/dashboard），格式 `sk-…`，官方文档把
    //      Agent API 的 Key 也定成 `sk-xxxxxxxx`。面板侧建 Key 的接口路径没在公开
    //      文档里，worker 用"候选路径探测 + 命中即缓存"（`--keys-path` 可覆盖）。
    //   4) **推理是标准 OpenAI 形状**，鉴权 `Authorization: Bearer sk-…`，所以反代层
    //      直接走通用透传（`/p/blackbox/v1/*`），不需要 adapter。
    //   5) 另有一条 **Email OTP + Turnstile** 的 console 腿（console.blackbox.ai/login），
    //      但入口写着"公司必须已经在 GPU Console 里开通，或由管理员加你"，
    //      所以它是可选的第二条腿（`--engine console`），不是默认路径。
    //
    // 线上取证（2026-11，见 docs/BLACKBOX-POOL.md 的取证表）：
    //   GET  https://app.blackbox.ai/api/auth/providers
    //        -> {"google":{"id":"google","name":"Google","type":"oidc", ...}}
    //   GET  https://app.blackbox.ai/api/auth/csrf          -> {"csrfToken":"…"}
    //   POST https://app.blackbox.ai/api/auth/signin/google -> 302 到
    //        accounts.google.com/o/oauth2/v2/auth?client_id=985058387028-…&
    //        redirect_uri=https://app.blackbox.ai/api/auth/callback/google&
    //        code_challenge_method=S256&scope=openid profile email
    //   GET  https://app.blackbox.ai/api/auth/session        -> null（未登录）
    //   GET  https://app.blackbox.ai/api/auth/session-challenge
    //        -> 401 {"status":"unauthenticated"}
    //   POST https://enterprise.blackbox.ai/chat/completions  -> 403
    //        {"error":{"message":"blackbox.Error: Invalid API key","type":"auth_error"}}
    //   POST https://enterprise.blackbox.ai/v1/chat/completions -> 同上（/v1 也可用）
    //   api.blackbox.ai 目前对一切路径回 404（Google Frontend），只剩历史意义；
    //   chat.blackboxcli.ai（CLI 的设备码 OAuth）已 NXDOMAIN。
    id: "blackbox",
    label: "Blackbox AI",
    panelBase: "https://app.blackbox.ai",
    consoleBase: "https://console.blackbox.ai",
    // src/provider-relay.mjs 拼 `${apiBase}${rest}`，rest 是 /v1/chat/completions
    apiBase: "https://enterprise.blackbox.ai",
    // 上游没有 /models（实测 /models 与 /v1/models 都 404）：靠描述符里的白名单，
    // listModels 拿不到上游数据时会退回这里。
    modelsPath: "",
    defaultModel: "blackboxai/blackbox-pro",
    models: [
      "blackboxai/blackbox-pro",
      "blackboxai/blackbox-ai",
      "blackboxai/blackbox-search",
      "blackboxai/openai/gpt-4o",
      "blackboxai/openai/gpt-4o-mini",
      "blackboxai/anthropic/claude-sonnet-4",
      "blackboxai/google/gemini-2.5-pro",
    ],
    // 注册入口只有 Google OAuth；邮箱密码注册不存在（上面的 400 实测）
    supportsEmailSignup: false,
    supportsOAuthOnly: true,
    // 面板能列也能建 Key，但走的是"登录态"而不是公开 API Key 接口
    supportsKeyListing: true,
    supportsKeyCreationApi: true,
    openAiCompatible: true,
    signInUrl: "https://app.blackbox.ai/login",
    registerUrl: "https://app.blackbox.ai/login",
    accessKeyUrl: "https://app.blackbox.ai/dashboard",
    docs: "https://docs.blackbox.ai/api-reference/chat",

    // ---- 这个 worker 额外吃的命令行开关（其它 provider 传了也不会被拼进去）
    workerFlags: [
      "--session-file",
      "--google-pool",
      "--google-cookies",
      "--keys-path",
      "--keys-create-path",
      "--authorization-code",
      "--console-url",
    ],
    // ---- Auth.js（NextAuth v5）授权参数：worker 的协议腿就照这张表走 ----
    auth: {
      kind: "authjs",
      providersPath: "/api/auth/providers",
      csrfPath: "/api/auth/csrf",
      signinPath: "/api/auth/signin/google",
      callbackPath: "/api/auth/callback/google",
      sessionPath: "/api/auth/session",
      challengePath: "/api/auth/session-challenge",
      // Auth.js v5 的 cookie 前缀（HTTPS）
      cookiePrefix: "__Secure-authjs",
      sessionCookie: "__Secure-authjs.session-token",
      callbackCookie: "__Secure-authjs.callback-url",
      stateCookie: "__Secure-authjs.state",
      pkceCookie: "__Secure-authjs.pkce.code_verifier",
      // 从 302 Location 里读出来的（docs/BLACKBOX-POOL.md 取证表）
      googleClientId: "985058387028-iaa60lr5e586kvhjp085qcojv3jv2p7h.apps.googleusercontent.com",
      scope: "openid profile email",
    },
    // ---- 面板 Key 接口：没有公开文档，按候选路径探测，命中就写进 out/key-endpoints.json
    panelApi: {
      session: { method: "GET", path: "/api/auth/session" },
      challenge: { method: "GET", path: "/api/auth/session-challenge" },
      userActivity: { method: "POST", path: "/api/user-activity" },
      // 候选（第一个 200/201 且回带 sk-… 的即为真）
      keyCandidates: [
        "/api/keys",
        "/api/v1/keys",
        "/api/user/api-keys",
        "/api/user/keys",
        "/api/agent-api/keys",
        "/api/dashboard/keys",
        "/api/me/keys",
        "/api/api-keys",
        "/api/settings/api-keys",
        "/api/v1/api-keys",
        "/api/account/api-keys",
      ],
    },
    // ---- console 腿：Email OTP + Cloudflare Turnstile（需要公司已开通 GPU Console）
    consoleLogin: {
      path: "/login",
      turnstile: true,
      note: "入口要求 work email 且公司已在 BLACKBOX GPU Console 开通；Turnstile 用解题器或浏览器腿过。",
    },
    // ---- 反代：OpenAI 形状，透传即可（不需要 adapter）
    relay: {
      kind: "openai",
      path: "/v1/chat/completions",
      authMode: "api-key",
      // 上游 /v1/messages 不在路由表里，Anthropic 请求由 relay 翻成 chat 再翻回来
      synthesizeStream: false,
      emulateTools: false,
    },
    // ---- CLI / 桌面端（用于"下载并逆向 + 模拟运行环境"）
    cli: {
      npm: "@blackbox_ai/blackbox-cli",
      npmCore: "@blackbox_ai/blackbox-cli-core",
      installPs1: "https://blackbox.ai/install.ps1",
      installSh: "https://blackbox.ai/install.sh",
      releasesBase: "https://releases.blackbox.ai",
      releasesApi: "https://releases.blackbox.ai/api/v0/latest?product=blackbox-cli-v2&platform=windows-x64",
      // 设备码 OAuth（已下线，留档）：client_id + scope 从包内常量读出来的
      deviceFlow: {
        base: "https://chat.blackboxcli.ai",
        codePath: "/api/v1/oauth2/device/code",
        tokenPath: "/api/v1/oauth2/token",
        clientId: "f0304373b74a44d2b584a3fb70ca9e56",
        scope: "openid profile email model.completion",
      },
      env: {
        apiKey: "BLACKBOX_API_KEY",
        apiBase: "BLACKBOX_API_BASE_URL",
        model: "BLACKBOX_API_MODEL",
        encryptionBase: "BLACKBOX_ENCRYPTION_BASE_URL",
      },
      credentialsFile: "~/.blackboxcli/oauth_creds.json",
      // 把 CLI 指向本网关时的默认模型（CLI 的 --model 会覆盖）
      gatewayModel: "blackboxai/blackbox-pro",
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
    // blackbox 这类"登录态 + OAuth"的 provider：把登录入口与 CLI 信息也带出去，
    // 面板/CLI 不用再硬编码站点细节。
    auth: provider.auth
      ? { kind: provider.auth.kind, sessionCookie: provider.auth.sessionCookie, googleClientId: provider.auth.googleClientId }
      : null,
    cli: provider.cli
      ? { npm: provider.cli.npm, installPs1: provider.cli.installPs1, gatewayModel: provider.cli.gatewayModel }
      : null,
    consoleLogin: provider.consoleLogin ?? null,
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
