// 待配置项清单 —— "代码已经到位，但还缺外部条件"的东西集中在这里。
//
// 目的：让"哪条线还不能真跑、缺什么、怎么补"变成可查询的数据，而不是散在文档里。
//   node scripts/pending.mjs list        # 看清单（带状态）
//   node scripts/pending.mjs check       # 按当前环境变量判断哪些还缺
//   node scripts/pending.mjs md          # 生成 docs/PENDING-CONFIG.md
//
// 每条都带 `solved`/`open` 与证据，避免"以为配好了其实没有"。
export const KINDS = ["credential", "endpoint", "service", "config"];

/**
 * @typedef {object} PendingItem
 * @property {string} id
 * @property {string} line        影响的线（或 "all"）
 * @property {"credential"|"endpoint"|"service"|"config"} kind
 * @property {"open"|"solved"|"blocked"} status
 * @property {string} title
 * @property {string} impact      不配会怎样
 * @property {string[]} env       需要哪些环境变量
 * @property {string} how         怎么补（可直接照抄的命令/步骤）
 * @property {string} [evidence]  我实测到的证据
 */

/** @type {PendingItem[]} */
export const PENDING = [
  {
    id: "mail-domain",
    line: "all",
    kind: "credential",
    status: "open",
    title: "域名邮箱（catch-all + IMAP）",
    impact: "所有走邮箱验证的线（agnes / cerebras / jevai / cleverai 等）都注册不了；一次性域名大多被站点拒绝",
    env: ["LINE_MAIL_DOMAIN", "LINE_IMAP_HOST", "LINE_IMAP_USER", "LINE_IMAP_PASSWORD"],
    how:
      "买/用自己的域名 → 开 catch-all 转发到 IMAP 邮箱 → 填环境变量；\n" +
      "或先用一次性服务试水：`--mail-provider mailtm`（很多站会拒，只能验证链路）",
    evidence: "jev-ai 实测对 mailinator 回 EMAIL_DOMAIN_NOT_ALLOWED；agnes 对同域名高频注册限流",
  },
  {
    id: "agnes-captcha",
    line: "agnes",
    kind: "service",
    status: "solved",
    title: "Agnes 发码的腾讯云验证码（ticket + randstr）",
    impact: "不解决则 `/api/verification` 永远回 `Captcha verification is required`，注册停在第一步",
    env: ["LINE_CAPTCHA_TICKET", "LINE_CAPTCHA_RANDSTR"],
    how:
      "代码已支持三种取法：\n" +
      "  1) `--captcha browser`（Camoufox 打开 /login、点发送、拦截发码请求抓 ticket/randstr）——需要装 camoufox\n" +
      "  2) `--captcha manual --ticket <t> --randstr <r>`（人工/第三方打码平台给的票）\n" +
      "  3) `--captcha none`（站点临时关校验时）",
    evidence:
      "实测 GET /api/verification → {\"code\":400,\"message\":\"Captcha verification is required\"}；" +
      "前端 chunk 里调用为 `/api/verification?email=&purpose=register&ticket=&randstr=`，TENCENT_CAPTCHA_REGION=cn",
  },
  {
    id: "cerebras-key-path",
    line: "cerebras",
    kind: "endpoint",
    status: "blocked",
    title: "Cerebras 建 Key 端点的真实语义",
    impact: "`/api/auth/api-keys` 只回 `400 \"Bad request.\"`，不知道要什么参数；发 Key 这一步没法脚本化",
    env: ["CEREBRAS_SESSION_COOKIE", "CEREBRAS_API_KEY"],
    how:
      "登录 cloud.cerebras.ai → 浏览器 DevTools → 在 API Keys 页面点一次 \"Create\"，\n" +
      "把请求（URL/method/body/headers）记下来，填进 `tools/cerebras-reg/cerebras_reg/site.py` 的 `key_candidates`；\n" +
      "或者直接手工建 Key，用 `POST /api/admin/cerebras/pool/import` 入池（这条路现在就能用）",
    evidence: "实测 GET/POST /api/auth/api-keys（带 Origin/Referer）恒为 400 \"Bad request.\"；console 是静态导出 + 独立 API",
  },
  {
    id: "unorouter-key-path",
    line: "unorouter",
    kind: "endpoint",
    status: "solved",
    title: "UnoRouter 发 Key 端点",
    impact: "已定位到真端点，剩下只是登录态",
    env: ["UNOROUTER_SESSION_COOKIE"],
    how:
      "端点：`POST https://api.unorouter.com/api/token/`（注意尾斜杠）→ 带登录态 cookie/Bearer 即可建 Key；\n" +
      "worker 的 key_candidates 已按这个顺序排好（先 /api/token/ 再 /api/token）",
    evidence: "实测 POST /api/token/ → 401 {\"code\":\"AUTH_UNAUTHORIZED\"}；不带斜杠 307；GET 被 Cloudflare 403",
  },
  {
    id: "opencode-key-path",
    line: "opencode-zen",
    kind: "endpoint",
    status: "blocked",
    title: "OpenCode Zen 发 Key 路径",
    impact: "只能匿名用 `/zen/v1/models`；发 Key 端点未公开（站点是静态导出，POST /api/* 一律 405）",
    env: ["OPENCODE_API_KEY"],
    how:
      "登录 opencode.ai → 打开 Zen 页面 → DevTools 抓 `Create key` 请求；\n" +
      "另外它家 CLI 走 `opencode auth login`（GitHub 设备码），也可以先手工建 Key 再 `import`",
    evidence: "实测 POST /api/keys /api/api-keys /api/user/api-keys /api/tokens /api/token /v1/keys 全部 405（静态站点）；models 匿名 200",
  },
  {
    id: "requesty-key-path",
    line: "requesty",
    kind: "endpoint",
    status: "blocked",
    title: "Requesty 发 Key 路径",
    impact: "`router.requesty.ai/v1` 可用（200/401），但 app.requesty.ai 的建 Key 接口没暴露",
    env: ["REQUESTY_API_KEY"],
    how:
      "登录 app.requesty.ai → API Keys → DevTools 抓创建请求 → 填进 `tools/requesty-reg/requesty_reg/site.py`；\n" +
      "或手工建 Key 后 `POST /api/admin/requesty/pool/import`",
    evidence: "实测 /api/keys、/api/api-keys、/api/user/api-keys、/api/tokens、/api/token 均为 404 + HTML（Next 静态壳）",
  },
  {
    id: "jev-key-path",
    line: "jevai",
    kind: "endpoint",
    status: "blocked",
    title: "JEV AI 发 Key 路径",
    impact: "注册链路已验证存在（sign-up 400/OK），但 `/settings/apikeys` 是客户端渲染，接口名未知",
    env: ["JEVAIS_API_KEY"],
    how:
      "登录 jev-ai.org → /settings/apikeys → DevTools 抓创建请求 → 填进 `tools/jevai-reg/jevai_reg/site.py` 的 `KEY_CANDIDATES`；\n" +
      "worker 已有\"候选探测 + 命中缓存\"，填对了会自动记住",
    evidence: "实测 /api/auth/ok 200、sign-up/email 400（缺字段=存在）、/api/user/api-keys 404 HTML；匿名 GET /api/v1/models 401",
  },
  {
    id: "amd-account",
    line: "amd",
    kind: "credential",
    status: "open",
    title: "AMD 账号登录态（cookie / Bearer）",
    impact: "没有登录态就没法查积分、起 GPU 实例；这条线的「注册」在 AMD 统一账号（要手机号）",
    env: ["AMD_SESSION_COOKIE", "AMD_ACCESS_TOKEN"],
    how:
      "浏览器登录 developer.amd.com.cn → DevTools → Network → 任一 /api/ 请求 → 复制 `Cookie:` 头 →\n" +
      "`python -m amd_reg import --email you@example.com --cookie \"<cookie>\"`（或 `node src/amd-cli.mjs import cookies.txt`）",
    evidence: "实测 /api/User/GetCurrentUserDataSummary 无 cookie 回 401；bundle 里没有注册端点（登录走 account.amd.com）",
  },
  {
    id: "amd-course-exam",
    line: "amd",
    kind: "endpoint",
    status: "solved",
    title: "AMD 课程考试的交卷字段",
    impact: "有考试的课程只看完视频不算通过；现在已按前端逐字对齐，可自动答题",
    env: ["AMD_SESSION_COOKIE"],
    how:
      "`python -m amd_reg exam --course <id> --auto`：取题（GET ?courseChildId）→ 交卷（POST SetExamAnswer，" +
      "body={CourseChildId, QuestionAnswerRequests:[{OrderId,QuestionId,QuestionMsg,RightAnswer,QuestionType,hasProblem}]}）→ " +
      "读判卷（GetAnswerResult）捡回正确答案 → 回填重交，最多 --rounds 轮",
    evidence:
      "course-detail chunk 的 quizDialog：Se({CourseChildId,QuestionAnswerRequests})、Te({courseChildId})、$e({courseChildId})；" +
      "RightAnswer 是逗号分隔的 Option 值，RightAnswerType 0=单选 1=多选",
  },
  {
    id: "amd-voucher-fields",
    line: "amd",
    kind: "endpoint",
    status: "open",
    title: "算力券兑换的 body 字段",
    impact: "\`ExchangeLabVoucher\` 的参数（数量/面额/目标）前端在兑换弹窗里拼，工具先按 Amount/Count/Num 提交；若站点要别的字段会 400",
    env: ["AMD_SESSION_COOKIE"],
    how:
      "先 \`python -m amd_reg vouchers\` 读出 \`GetLabVoucherExchangeConfig\`（积分/小时/上限）→ \`exchange --dry-run\` 确认 → \`exchange --amount 1\`；\n" +
      "若 400，把响应贴出来，或直接用 \`--extra '{\\\"FieldName\\\": value}'\` 透传站点要求的字段",
    evidence: "points-redeem chunk 里 ExchangeLabVoucher(e) 直接透传调用方对象，字段名不在该 chunk",
  },
  {
    id: "amd-token-factory",
    line: "amd",
    kind: "endpoint",
    status: "open",
    title: "Token Factory（免费模型）的数据接口",
    impact: "\`models\` 命令靠候选探测 + 页面扫描找 base url；找不到就只能手工填",
    env: ["AMD_SESSION_COOKIE"],
    how:
      "登录后打开 /tokenfactory → DevTools 抓一次 XHR（返回里含 Access.BaseUrl）→ 把路径加进\n" +
      "\`tools/amd-reg/amd_reg/vouchers.py\` 的 \`TOKEN_FACTORY_CANDIDATES\`；之后会缓存到 out/tokenfactory.json",
    evidence: "tokenfactory 页面 chunk 只有渲染逻辑（Access.BaseUrl.Value、Pricing.Items），没有 url",
  },
  {
    id: "amd-approval",
    line: "amd",
    kind: "credential",
    status: "blocked",
    title: "AMD 开发者云额度审批",
    impact: "即使注册成功，$100 / 25h / 100h 的免费额度都要 AMD 人工审批（官网写明 sole discretion），脚本无法绕过",
    env: ["AMD_CREDIT_APPROVED"],
    how:
      "按官网流程提交 \"Request Credit\"（AMD AI Developer Program）→ 审批通过后额度出现在账号里 →\n" +
      "用 `python -m amd_reg points` 能看到才说明生效；多号叠加额度是唯一「规模」来源",
    evidence: "AMD 官网：25 小时赠金 \"to qualified developers in AMD's sole discretion\"；新门户写 $100 credits 需激活",
  },
  {
    id: "cleverai-appcheck",
    line: "cleverai",
    kind: "credential",
    status: "open",
    title: "Clever AI 的 App Check token",
    impact: "appserver 全部接口要 `X-Firebase-AppCheck`；拿不到就是 401 APP_CHECK_REQUIRED",
    env: ["CLEVERAI_APPCHECK_TOKEN"],
    how:
      "worker 已有 `python -m cleverai_reg --json appcheck`（Camoufox 抓 reCAPTCHA Enterprise）；\n" +
      "批量注册时一个 token 可用约 1 小时，面板里有\"续期\"按钮",
    evidence: "实测 401 {\"code\":\"APP_CHECK_REQUIRED\"}；token TTL 3600s，按 app 安装共享",
  },
  {
    id: "giteeai-tokens",
    line: "giteeai",
    kind: "credential",
    status: "open",
    title: "Gitee AI 访问令牌",
    impact: "这条线没有机器注册接口，令牌必须在 gitee.com 个人设置里手工生成",
    env: ["GITEEAI_TOKEN"],
    how:
      "gitee.com → 设置 → 私人令牌 → 生成（勾选 AI 相关权限）→\n" +
      "`node src/giteeai-cli.mjs import tokens.txt`（会先用 `/v1/models` 校验再入池）",
    evidence: "实测 https://ai.gitee.com/v1/models 200（可列模型），说明校验路径可用",
  },
  {
    id: "proxyscrape-turnstile",
    line: "proxyscrape",
    kind: "service",
    status: "open",
    title: "ProxyScrape 的 Turnstile",
    impact: "注册与登录都要 `cf_turnstile_token`，不带就是 400",
    env: ["PROXYSCRAPE_CAPTCHA_PROVIDER", "PROXYSCRAPE_CAPTCHA_KEY", "CAPTCHA_SOLVER_URL"],
    how:
      "起本地求解器：`python tools/captcha-solver/main.py --headless` →\n" +
      "`--captcha solver --captcha-key http://127.0.0.1:5000`；或者用 capsolver：`--captcha capsolver --captcha-key <clientKey>`",
    evidence: "实测 POST /v2/v4/account/auth/register 无 token → 400 \"No Cloudflare Turnstile token provided.\"；sitekey 0x4AAAAAAAFWUVCKyusT9T8r",
  },
  {
    id: "captcha-solver-runtime",
    line: "all",
    kind: "service",
    status: "open",
    title: "Turnstile 求解器运行环境",
    impact: "没有它，凡是 Turnstile 站都只能手工喂 token",
    env: ["CAPTCHA_SOLVER_URL"],
    how: "`pip install -r tools/captcha-solver/requirements.txt`（quart + patchright + camoufox）→ `python tools/captcha-solver/main.py --headless`",
    evidence: "vendor 自 Theyka/Turnstile-Solver；协议 GET /turnstile + GET /result 已封成 src/captcha.mjs",
  },
  {
    id: "register-proxies",
    line: "all",
    kind: "config",
    status: "open",
    title: "注册用出口代理（一出口一号）",
    impact: "同 IP 批量注册必被封；OpenRouter / Groq / Cerebras 这类尤其严格",
    env: ["LINE_PROXY_LIST", "PROXYSCRAPE_PUBLIC_BASE"],
    how:
      "先跑 ProxyScrape 线拿代理：`node src/proxyscrape-cli.mjs register -n 3` → `apply` 会把出口写进 `data/proxies/<line>.txt`；\n" +
      "各线 register 时不带 `proxies` 就会自动读这些文件",
    evidence: "proxyscrape 线本身就是\"注册领代理\"；`src/proxy-share.mjs` 负责分发",
  },
  {
    id: "gateway-public-base",
    line: "all",
    kind: "config",
    status: "open",
    title: "网关对外地址（订阅链接 / 接入信息）",
    impact: "没有它，`/proxy/<token>` 订阅链接和 `connect` 返回的都是 127.0.0.1，别的设备拉不到",
    env: ["DEEPAI_PUBLIC_BASE"],
    how: "部署时设 `DEEPAI_PUBLIC_BASE=https://your.domain`（Nginx 反代 + SSE 关 buffering）",
    evidence: "docs/DEPLOY.md 的 Nginx 段落",
  },
  {
    id: "admin-token",
    line: "all",
    kind: "credential",
    status: "open",
    title: "网关管理口令",
    impact: "面板与所有 `/api/admin/*` 都过不去",
    env: ["DEEPAI_GATEWAY_TOKEN"],
    how: "网关首次启动会打印 token；填进面板或环境变量（worker 回推也要它）",
    evidence: "所有 worker 的 `push` 都带 `Authorization: Bearer <token>`",
  },
];

export const byId = (id) => PENDING.find((item) => item.id === id) ?? null;

/** 按线过滤；`all` 算每条线都要。 */
export const forLine = (line) => PENDING.filter((item) => item.line === line || item.line === "all");

export const openItems = () => PENDING.filter((item) => item.status !== "solved");

export function summary() {
  const out = { total: PENDING.length, open: 0, solved: 0, blocked: 0, byKind: {}, byLine: {} };
  for (const item of PENDING) {
    if (item.status === "open") out.open++;
    else if (item.status === "solved") out.solved++;
    else out.blocked++;
    out.byKind[item.kind] = (out.byKind[item.kind] ?? 0) + 1;
    out.byLine[item.line] = (out.byLine[item.line] ?? 0) + 1;
  }
  return out;
}

/**
 * 按环境变量判断"还缺什么"。
 *
 * @param {Record<string, string|undefined>} env
 * @returns {Array<{id:string, line:string, title:string, missing:string[], ready:boolean, status:string}>}
 */
export function checkEnv(env = process.env) {
  return PENDING.map((item) => {
    const missing = (item.env ?? []).filter((name) => !String(env[name] ?? "").trim());
    return {
      id: item.id,
      line: item.line,
      title: item.title,
      status: item.status,
      missing,
      // 已经 solved 的项不再要求配置；其余只要还有缺失变量就算没准备好
      ready: item.status === "solved" ? true : missing.length === 0,
    };
  });
}

export function toMarkdown(items = PENDING, env = process.env) {
  const s = summary();
  const head = [
    "# 待配置项（自动生成）",
    "",
    `> 生成于 ${new Date().toISOString()}　命令：\`node scripts/pending.mjs md\``,
    `> 共 ${s.total} 项：open ${s.open} / blocked ${s.blocked} / solved ${s.solved}`,
    "",
    "判定：`open` = 只缺配置（填环境变量就能跑）；`blocked` = 需要登录态抓一次端点；`solved` = 代码已实现，运行时按需取票据。",
    "",
    "| # | 项 | 线 | 类型 | 状态 | 缺的变量 |",
    "| --- | --- | --- | --- | --- | --- |",
  ];
  const rows = items.map((item, index) => {
    const missing = (item.env ?? []).filter((name) => !String(env[name] ?? "").trim());
    const mark = item.status === "solved" ? "✅" : item.status === "blocked" ? "⛔" : missing.length ? "🟡" : "🟢";
    return `| ${index + 1} | ${item.title} | ${item.line} | ${item.kind} | ${mark} ${item.status} | ${missing.length ? missing.map((m) => `\`${m}\``).join(" ") : "—"} |`;
  });
  const detail = ["", "## 逐项说明", ""];
  for (const item of items) {
    detail.push(`### ${item.title}（${item.id}）`);
    detail.push("");
    detail.push(`- 线：\`${item.line}\`　类型：${item.kind}　状态：**${item.status}**`);
    detail.push(`- 不配会怎样：${item.impact}`);
    if (item.env?.length) detail.push(`- 相关变量：${item.env.map((m) => `\`${m}\``).join("、")}`);
    detail.push(`- 怎么补：${item.how.replace(/\n/g, "\n  ")}`);
    if (item.evidence) detail.push(`- 实测证据：${item.evidence}`);
    detail.push("");
  }
  return [...head, ...rows, ...detail].join("\n");
}
