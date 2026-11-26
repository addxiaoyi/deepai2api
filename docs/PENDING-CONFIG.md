# 待配置项（自动生成）

> 生成于 2026-11-26T15:32:50.035Z　命令：`node scripts/pending.mjs md`
> 共 19 项：open 11 / blocked 5 / solved 3

判定：`open` = 只缺配置（填环境变量就能跑）；`blocked` = 需要登录态抓一次端点；`solved` = 代码已实现，运行时按需取票据。

| # | 项 | 线 | 类型 | 状态 | 缺的变量 |
| --- | --- | --- | --- | --- | --- |
| 1 | 域名邮箱（catch-all + IMAP） | all | credential | 🟡 open | `LINE_MAIL_DOMAIN` `LINE_IMAP_HOST` `LINE_IMAP_USER` `LINE_IMAP_PASSWORD` |
| 2 | Agnes 发码的腾讯云验证码（ticket + randstr） | agnes | service | ✅ solved | `LINE_CAPTCHA_TICKET` `LINE_CAPTCHA_RANDSTR` |
| 3 | Cerebras 建 Key 端点的真实语义 | cerebras | endpoint | ⛔ blocked | `CEREBRAS_SESSION_COOKIE` `CEREBRAS_API_KEY` |
| 4 | UnoRouter 发 Key 端点 | unorouter | endpoint | ✅ solved | `UNOROUTER_SESSION_COOKIE` |
| 5 | OpenCode Zen 发 Key 路径 | opencode-zen | endpoint | ⛔ blocked | `OPENCODE_API_KEY` |
| 6 | Requesty 发 Key 路径 | requesty | endpoint | ⛔ blocked | `REQUESTY_API_KEY` |
| 7 | JEV AI 发 Key 路径 | jevai | endpoint | ⛔ blocked | `JEVAIS_API_KEY` |
| 8 | AMD 账号登录态（cookie / Bearer） | amd | credential | 🟡 open | `AMD_SESSION_COOKIE` `AMD_ACCESS_TOKEN` |
| 9 | AMD 课程考试的交卷字段 | amd | endpoint | ✅ solved | `AMD_SESSION_COOKIE` |
| 10 | 算力券兑换的 body 字段 | amd | endpoint | 🟡 open | `AMD_SESSION_COOKIE` |
| 11 | Token Factory（免费模型）的数据接口 | amd | endpoint | 🟡 open | `AMD_SESSION_COOKIE` |
| 12 | AMD 开发者云额度审批 | amd | credential | ⛔ blocked | `AMD_CREDIT_APPROVED` |
| 13 | Clever AI 的 App Check token | cleverai | credential | 🟡 open | `CLEVERAI_APPCHECK_TOKEN` |
| 14 | Gitee AI 访问令牌 | giteeai | credential | 🟡 open | `GITEEAI_TOKEN` |
| 15 | ProxyScrape 的 Turnstile | proxyscrape | service | 🟡 open | `PROXYSCRAPE_CAPTCHA_PROVIDER` `PROXYSCRAPE_CAPTCHA_KEY` `CAPTCHA_SOLVER_URL` |
| 16 | Turnstile 求解器运行环境 | all | service | 🟡 open | `CAPTCHA_SOLVER_URL` |
| 17 | 注册用出口代理（一出口一号） | all | config | 🟡 open | `LINE_PROXY_LIST` `PROXYSCRAPE_PUBLIC_BASE` |
| 18 | 网关对外地址（订阅链接 / 接入信息） | all | config | 🟡 open | `DEEPAI_PUBLIC_BASE` |
| 19 | 网关管理口令 | all | credential | 🟡 open | `DEEPAI_GATEWAY_TOKEN` |

## 逐项说明

### 域名邮箱（catch-all + IMAP）（mail-domain）

- 线：`all`　类型：credential　状态：**open**
- 不配会怎样：所有走邮箱验证的线（agnes / cerebras / jevai / cleverai 等）都注册不了；一次性域名大多被站点拒绝
- 相关变量：`LINE_MAIL_DOMAIN`、`LINE_IMAP_HOST`、`LINE_IMAP_USER`、`LINE_IMAP_PASSWORD`
- 怎么补：买/用自己的域名 → 开 catch-all 转发到 IMAP 邮箱 → 填环境变量；
  或先用一次性服务试水：`--mail-provider mailtm`（很多站会拒，只能验证链路）
- 实测证据：jev-ai 实测对 mailinator 回 EMAIL_DOMAIN_NOT_ALLOWED；agnes 对同域名高频注册限流

### Agnes 发码的腾讯云验证码（ticket + randstr）（agnes-captcha）

- 线：`agnes`　类型：service　状态：**solved**
- 不配会怎样：不解决则 `/api/verification` 永远回 `Captcha verification is required`，注册停在第一步
- 相关变量：`LINE_CAPTCHA_TICKET`、`LINE_CAPTCHA_RANDSTR`
- 怎么补：代码已支持三种取法：
    1) `--captcha browser`（Camoufox 打开 /login、点发送、拦截发码请求抓 ticket/randstr）——需要装 camoufox
    2) `--captcha manual --ticket <t> --randstr <r>`（人工/第三方打码平台给的票）
    3) `--captcha none`（站点临时关校验时）
- 实测证据：实测 GET /api/verification → {"code":400,"message":"Captcha verification is required"}；前端 chunk 里调用为 `/api/verification?email=&purpose=register&ticket=&randstr=`，TENCENT_CAPTCHA_REGION=cn

### Cerebras 建 Key 端点的真实语义（cerebras-key-path）

- 线：`cerebras`　类型：endpoint　状态：**blocked**
- 不配会怎样：`/api/auth/api-keys` 只回 `400 "Bad request."`，不知道要什么参数；发 Key 这一步没法脚本化
- 相关变量：`CEREBRAS_SESSION_COOKIE`、`CEREBRAS_API_KEY`
- 怎么补：登录 cloud.cerebras.ai → 浏览器 DevTools → 在 API Keys 页面点一次 "Create"，
  把请求（URL/method/body/headers）记下来，填进 `tools/cerebras-reg/cerebras_reg/site.py` 的 `key_candidates`；
  或者直接手工建 Key，用 `POST /api/admin/cerebras/pool/import` 入池（这条路现在就能用）
- 实测证据：实测 GET/POST /api/auth/api-keys（带 Origin/Referer）恒为 400 "Bad request."；console 是静态导出 + 独立 API

### UnoRouter 发 Key 端点（unorouter-key-path）

- 线：`unorouter`　类型：endpoint　状态：**solved**
- 不配会怎样：已定位到真端点，剩下只是登录态
- 相关变量：`UNOROUTER_SESSION_COOKIE`
- 怎么补：端点：`POST https://api.unorouter.com/api/token/`（注意尾斜杠）→ 带登录态 cookie/Bearer 即可建 Key；
  worker 的 key_candidates 已按这个顺序排好（先 /api/token/ 再 /api/token）
- 实测证据：实测 POST /api/token/ → 401 {"code":"AUTH_UNAUTHORIZED"}；不带斜杠 307；GET 被 Cloudflare 403

### OpenCode Zen 发 Key 路径（opencode-key-path）

- 线：`opencode-zen`　类型：endpoint　状态：**blocked**
- 不配会怎样：只能匿名用 `/zen/v1/models`；发 Key 端点未公开（站点是静态导出，POST /api/* 一律 405）
- 相关变量：`OPENCODE_API_KEY`
- 怎么补：登录 opencode.ai → 打开 Zen 页面 → DevTools 抓 `Create key` 请求；
  另外它家 CLI 走 `opencode auth login`（GitHub 设备码），也可以先手工建 Key 再 `import`
- 实测证据：实测 POST /api/keys /api/api-keys /api/user/api-keys /api/tokens /api/token /v1/keys 全部 405（静态站点）；models 匿名 200

### Requesty 发 Key 路径（requesty-key-path）

- 线：`requesty`　类型：endpoint　状态：**blocked**
- 不配会怎样：`router.requesty.ai/v1` 可用（200/401），但 app.requesty.ai 的建 Key 接口没暴露
- 相关变量：`REQUESTY_API_KEY`
- 怎么补：登录 app.requesty.ai → API Keys → DevTools 抓创建请求 → 填进 `tools/requesty-reg/requesty_reg/site.py`；
  或手工建 Key 后 `POST /api/admin/requesty/pool/import`
- 实测证据：实测 /api/keys、/api/api-keys、/api/user/api-keys、/api/tokens、/api/token 均为 404 + HTML（Next 静态壳）

### JEV AI 发 Key 路径（jev-key-path）

- 线：`jevai`　类型：endpoint　状态：**blocked**
- 不配会怎样：注册链路已验证存在（sign-up 400/OK），但 `/settings/apikeys` 是客户端渲染，接口名未知
- 相关变量：`JEVAIS_API_KEY`
- 怎么补：登录 jev-ai.org → /settings/apikeys → DevTools 抓创建请求 → 填进 `tools/jevai-reg/jevai_reg/site.py` 的 `KEY_CANDIDATES`；
  worker 已有"候选探测 + 命中缓存"，填对了会自动记住
- 实测证据：实测 /api/auth/ok 200、sign-up/email 400（缺字段=存在）、/api/user/api-keys 404 HTML；匿名 GET /api/v1/models 401

### AMD 账号登录态（cookie / Bearer）（amd-account）

- 线：`amd`　类型：credential　状态：**open**
- 不配会怎样：没有登录态就没法查积分、起 GPU 实例；这条线的「注册」在 AMD 统一账号（要手机号）
- 相关变量：`AMD_SESSION_COOKIE`、`AMD_ACCESS_TOKEN`
- 怎么补：浏览器登录 developer.amd.com.cn → DevTools → Network → 任一 /api/ 请求 → 复制 `Cookie:` 头 →
  `python -m amd_reg import --email you@example.com --cookie "<cookie>"`（或 `node src/amd-cli.mjs import cookies.txt`）
- 实测证据：实测 /api/User/GetCurrentUserDataSummary 无 cookie 回 401；bundle 里没有注册端点（登录走 account.amd.com）

### AMD 课程考试的交卷字段（amd-course-exam）

- 线：`amd`　类型：endpoint　状态：**solved**
- 不配会怎样：有考试的课程只看完视频不算通过；现在已按前端逐字对齐，可自动答题
- 相关变量：`AMD_SESSION_COOKIE`
- 怎么补：`python -m amd_reg exam --course <id> --auto`：取题（GET ?courseChildId）→ 交卷（POST SetExamAnswer，body={CourseChildId, QuestionAnswerRequests:[{OrderId,QuestionId,QuestionMsg,RightAnswer,QuestionType,hasProblem}]}）→ 读判卷（GetAnswerResult）捡回正确答案 → 回填重交，最多 --rounds 轮
- 实测证据：course-detail chunk 的 quizDialog：Se({CourseChildId,QuestionAnswerRequests})、Te({courseChildId})、$e({courseChildId})；RightAnswer 是逗号分隔的 Option 值，RightAnswerType 0=单选 1=多选

### 算力券兑换的 body 字段（amd-voucher-fields）

- 线：`amd`　类型：endpoint　状态：**open**
- 不配会怎样：`ExchangeLabVoucher` 的参数（数量/面额/目标）前端在兑换弹窗里拼，工具先按 Amount/Count/Num 提交；若站点要别的字段会 400
- 相关变量：`AMD_SESSION_COOKIE`
- 怎么补：先 `python -m amd_reg vouchers` 读出 `GetLabVoucherExchangeConfig`（积分/小时/上限）→ `exchange --dry-run` 确认 → `exchange --amount 1`；
  若 400，把响应贴出来，或直接用 `--extra '{\"FieldName\": value}'` 透传站点要求的字段
- 实测证据：points-redeem chunk 里 ExchangeLabVoucher(e) 直接透传调用方对象，字段名不在该 chunk

### Token Factory（免费模型）的数据接口（amd-token-factory）

- 线：`amd`　类型：endpoint　状态：**open**
- 不配会怎样：`models` 命令靠候选探测 + 页面扫描找 base url；找不到就只能手工填
- 相关变量：`AMD_SESSION_COOKIE`
- 怎么补：登录后打开 /tokenfactory → DevTools 抓一次 XHR（返回里含 Access.BaseUrl）→ 把路径加进
  `tools/amd-reg/amd_reg/vouchers.py` 的 `TOKEN_FACTORY_CANDIDATES`；之后会缓存到 out/tokenfactory.json
- 实测证据：tokenfactory 页面 chunk 只有渲染逻辑（Access.BaseUrl.Value、Pricing.Items），没有 url

### AMD 开发者云额度审批（amd-approval）

- 线：`amd`　类型：credential　状态：**blocked**
- 不配会怎样：即使注册成功，$100 / 25h / 100h 的免费额度都要 AMD 人工审批（官网写明 sole discretion），脚本无法绕过
- 相关变量：`AMD_CREDIT_APPROVED`
- 怎么补：按官网流程提交 "Request Credit"（AMD AI Developer Program）→ 审批通过后额度出现在账号里 →
  用 `python -m amd_reg points` 能看到才说明生效；多号叠加额度是唯一「规模」来源
- 实测证据：AMD 官网：25 小时赠金 "to qualified developers in AMD's sole discretion"；新门户写 $100 credits 需激活

### Clever AI 的 App Check token（cleverai-appcheck）

- 线：`cleverai`　类型：credential　状态：**open**
- 不配会怎样：appserver 全部接口要 `X-Firebase-AppCheck`；拿不到就是 401 APP_CHECK_REQUIRED
- 相关变量：`CLEVERAI_APPCHECK_TOKEN`
- 怎么补：worker 已有 `python -m cleverai_reg --json appcheck`（Camoufox 抓 reCAPTCHA Enterprise）；
  批量注册时一个 token 可用约 1 小时，面板里有"续期"按钮
- 实测证据：实测 401 {"code":"APP_CHECK_REQUIRED"}；token TTL 3600s，按 app 安装共享

### Gitee AI 访问令牌（giteeai-tokens）

- 线：`giteeai`　类型：credential　状态：**open**
- 不配会怎样：这条线没有机器注册接口，令牌必须在 gitee.com 个人设置里手工生成
- 相关变量：`GITEEAI_TOKEN`
- 怎么补：gitee.com → 设置 → 私人令牌 → 生成（勾选 AI 相关权限）→
  `node src/giteeai-cli.mjs import tokens.txt`（会先用 `/v1/models` 校验再入池）
- 实测证据：实测 https://ai.gitee.com/v1/models 200（可列模型），说明校验路径可用

### ProxyScrape 的 Turnstile（proxyscrape-turnstile）

- 线：`proxyscrape`　类型：service　状态：**open**
- 不配会怎样：注册与登录都要 `cf_turnstile_token`，不带就是 400
- 相关变量：`PROXYSCRAPE_CAPTCHA_PROVIDER`、`PROXYSCRAPE_CAPTCHA_KEY`、`CAPTCHA_SOLVER_URL`
- 怎么补：起本地求解器：`python tools/captcha-solver/main.py --headless` →
  `--captcha solver --captcha-key http://127.0.0.1:5000`；或者用 capsolver：`--captcha capsolver --captcha-key <clientKey>`
- 实测证据：实测 POST /v2/v4/account/auth/register 无 token → 400 "No Cloudflare Turnstile token provided."；sitekey 0x4AAAAAAAFWUVCKyusT9T8r

### Turnstile 求解器运行环境（captcha-solver-runtime）

- 线：`all`　类型：service　状态：**open**
- 不配会怎样：没有它，凡是 Turnstile 站都只能手工喂 token
- 相关变量：`CAPTCHA_SOLVER_URL`
- 怎么补：`pip install -r tools/captcha-solver/requirements.txt`（quart + patchright + camoufox）→ `python tools/captcha-solver/main.py --headless`
- 实测证据：vendor 自 Theyka/Turnstile-Solver；协议 GET /turnstile + GET /result 已封成 src/captcha.mjs

### 注册用出口代理（一出口一号）（register-proxies）

- 线：`all`　类型：config　状态：**open**
- 不配会怎样：同 IP 批量注册必被封；OpenRouter / Groq / Cerebras 这类尤其严格
- 相关变量：`LINE_PROXY_LIST`、`PROXYSCRAPE_PUBLIC_BASE`
- 怎么补：先跑 ProxyScrape 线拿代理：`node src/proxyscrape-cli.mjs register -n 3` → `apply` 会把出口写进 `data/proxies/<line>.txt`；
  各线 register 时不带 `proxies` 就会自动读这些文件
- 实测证据：proxyscrape 线本身就是"注册领代理"；`src/proxy-share.mjs` 负责分发

### 网关对外地址（订阅链接 / 接入信息）（gateway-public-base）

- 线：`all`　类型：config　状态：**open**
- 不配会怎样：没有它，`/proxy/<token>` 订阅链接和 `connect` 返回的都是 127.0.0.1，别的设备拉不到
- 相关变量：`DEEPAI_PUBLIC_BASE`
- 怎么补：部署时设 `DEEPAI_PUBLIC_BASE=https://your.domain`（Nginx 反代 + SSE 关 buffering）
- 实测证据：docs/DEPLOY.md 的 Nginx 段落

### 网关管理口令（admin-token）

- 线：`all`　类型：credential　状态：**open**
- 不配会怎样：面板与所有 `/api/admin/*` 都过不去
- 相关变量：`DEEPAI_GATEWAY_TOKEN`
- 怎么补：网关首次启动会打印 token；填进面板或环境变量（worker 回推也要它）
- 实测证据：所有 worker 的 `push` 都带 `Authorization: Bearer <token>`

