# DeepAI Code Gateway + OpenWebUI fork

把 DeepAI Code 的托管模型通道逆向出来，做成 **sub2api 形态**的中转网关：
OAuth 登录订阅账号 → 铸造 OpenAI 兼容 API Key → 给任意工具用。
**前端不另写**，直接 fork [Open WebUI](https://github.com/open-webui/open-webui) 做二次开发，
管理入口就在 Open WebUI 自己的「设置 → DeepAI Code」里。

```
┌──────────────────────────────┐        ┌────────────────────────┐        ┌──────────────────┐
│ Open WebUI (fork, :3000)     │        │ DeepAI Gateway (:8787) │        │ api.deepai.org   │
│  设置 → DeepAI Code          │─HTTP──▶│ 账号池 / 调度器 / 限额  │───────▶│ /deepai_code/*   │
│  /api/v1/deepai/*            │        │ OAuth / Key / 计费      │        │  托管模型通道     │
│  OpenAI 连接（自动写入）      │◀───────│ /v1  ·  /v1/messages    │        └──────────────────┘
└──────────────────────────────┘        └────────────────────────┘
        ▲                                        ▲
        │                              Cline / Claude Code / 任意 SDK
        └── 浏览器里的 DeepAI OAuth 回跳 http://127.0.0.1:8787/auth-callback
```

---

## 1. 快速开始

```powershell
git clone https://github.com/addxiaoyi/deepai2api.git
cd deepai2api
npm install

# 1) 拉取 Open WebUI 0.13.4 并套用 DeepAI provider（克隆 + 打补丁 + 构建前端）
.\scripts\apply-fork.ps1

# 2) 网关（只提供 API，没有自带 UI）
node src\index.mjs --host 0.0.0.0 --port 8787

# 3) 界面 = Open WebUI fork（overlay 镜像，秒级构建）
docker compose up -d --build
```

打开 **http://localhost:3000** → 注册管理员 → **设置 → Admin → DeepAI Code** →
填网关地址/口令 → 登录 DeepAI → 创建 Key → **接入并启用模型**。

> `vendor/` 不入库（约 1.9 GB）。`scripts/apply-fork.ps1` 负责把它还原出来：
> 克隆固定 tag → `git apply patches/0001-register-deepai-provider.patch` →
> 复制 `patches/files/` 里的新增文件 → `npm run build`。
> 升级 Open WebUI 时改 `-Ref` 参数即可，冲突会明确报出来。

> 网关的 `/` 不再是管理页面：设了 `DEEPAI_WEBUI_URL`（如 `http://127.0.0.1:3000`）
> 就会 302 跳到 Open WebUI，否则只显示一张说明卡片。界面只有 Open WebUI 一处。

只要 API 的话：

```
Base URL : http://127.0.0.1:8787/v1
API Key  : <Open WebUI 的 DeepAI 设置页里创建>
Model    : deepai-cyber
```

---

## 2. 与 sub2api 的能力对照

| sub2api 能力 | 本项目 | 说明 |
| --- | --- | --- |
| 多账号管理（OAuth 订阅账号） | ✅ `accounts[]` | 可连多个 DeepAI 账号；`upsertAccount` 按 apiKey 去重 |
| 账号健康 / 冷却 | ✅ `scheduler.mjs` | 401/403 → 冷却 10 分钟；429/5xx → 45 秒 |
| 智能调度 + 粘性会话 | ✅ | 首次选「最久未用」账号，之后同 key+model 粘 30 分钟 |
| 失败自动切换 | ✅ | 响应体未开始前最多切 3 个账号；全灭回 503 `all_accounts_failed` |
| 按 Key 分发 + 前缀 | ✅ `sk-dcg-…` | 只存 SHA-256，明文仅显示一次 |
| 每 Key 限额 | ✅ | `rpm` / `dailyRequests`，超出回 429 + `Retry-After` |
| 并发限制 | ✅ | 每个账号一条计费槽（通道本身一次只跑一个计费回合） |
| Token 级用量 | ✅ | 每 Key 记录 `prompt/completion_tokens`、请求数、最近使用 |
| 计费接口 | ✅ | `GET /v1/billing`（sub2api 风格 `usage` 块） |
| OpenAI 协议 | ✅ | `/v1/chat/completions`、`/v1/completions`、`/v1/responses`、`/v1/models` |
| Anthropic 协议 | ✅ | `/v1/messages`（含 SSE 事件序列、tool_use、thinking） |
| Gemini 协议 | ❌ | 未做（DeepAI 通道不提供 Gemini 形态） |
| Web 管理后台 | ✅ | Open WebUI fork：账号池 / Key / 模型 / 余额 / 接入 |
| 联网搜索 | ✅ | `POST /v1/search`（同 `/v1/web-search`），走通道自带 `web_search` |

---

## 3. OAuth 登录 → API Key

流程逆向自 `startDeepAISignIn` / `exchangeDeepAICode`（`server.asar` → `binCli-*.mjs`），
标准 **RFC 8252 回环 + PKCE S256**：

| 步骤 | 内容 |
| --- | --- |
| 1 | `verifier=random(32).base64url`、`challenge=sha256(verifier).base64url`、`state=random(32).hex` |
| 2 | 打开 `https://deepai.org/deepai_code/desktop_auth/?state=&port=8787&code_challenge=&code_challenge_method=S256` |
| 3 | 登录授权后回跳 `http://127.0.0.1:8787/auth-callback?code=&state=` |
| 4 | 校验 state（timing-safe）→ `POST https://api.deepai.org/deepai_code/desktop_auth/exchange/v2/` `{code, code_verifier}` |
| 5 | 拿到 `{api_key, username, is_pro}` → 存进账号池 |

与桌面端一致：`v2/` 404 回退无版本路由、5xx 重试、state 不匹配/超时一律拒绝。

**实测证据**（都是真实打 deepai.org / api.deepai.org）：

| 检查 | 结果 |
| --- | --- |
| 授权页接受本网关生成的 PKCE 参数 | `HTTP 200`，标题 `Sign in to DeepAI Code`，219832 字节 |
| `POST /deepai_code/desktop_auth/exchange/v2/` | `HTTP 404` ← 线上目前没有 v2 |
| `POST /deepai_code/desktop_auth/exchange/`（无版本） | `HTTP 400 {"error": "invalid or expired code"}` ← 端点活着 |
| 网关拒绝伪造 state 的回调 | `HTTP 400` |
| 完整交换链路（含 PKCE 校验、写账号池） | mock 上游上跑通（`tests/e2e.mjs`） |

注意第二条：**桌面端代码里写的 `v2/` 现在线上是 404**，真正的端点是**无版本**的
`.../exchange/`。所以「先试 v2、404 就回退」不是可选优化，而是唯一能成功走法 ——
这也解释了为什么照抄桌面端但不做回退的实现会直接失败。

最后一步（浏览器里点「授权」）必须你自己完成，我无法代替。完成后回调落到
`http://127.0.0.1:8787/auth-callback`，账号自动进池，模型立刻可用。

---

## 3.5 批量注册（`tools/deepai-reg`）

手动点授权一次一个太慢，所以另配了一台注册机：**协议引擎 + Camoufox 引擎 + 邮箱域名自动学习**，
产出的 `apiKey` 直接 `upsertAccount` 进同一个账号池 —— 在 **Open WebUI → 设置 → Admin → DeepAI**
的「批量注册 · 自动入池」面板里点一下就行，也可以命令行直推：

```powershell
cd tools\deepai-reg
python -m pip install -r requirements.txt
python -m deepai_reg doctor

# WebUI 面板背后做的事，等价于：
python -m deepai_reg run -n 5 --engine protocol --concurrency 3 --json `
  --push-url http://127.0.0.1:8787 --push-token <网关口令>

# 或事后批量灌
node tools/deepai-reg/scripts/import-to-gateway.mjs tools/deepai-reg/out/accounts.jsonl
```

注册协议（从首页内联 JS 抠的）：`get_user_login_type` → `save_custom_signup_data`
→ `POST api.deepai.org/daily-time-sync/registration/`（`email/username/password1/password2`）
→ `GET api.deepai.org/daily-time-sync/user/`，新号对象里直接带 `userprofile.api_key`，
**不需要邮箱验证，也不需要桌面端 OAuth**；`api_key` 缺失时才回落 PKCE 兑换。

WebUI 侧新增三个管理接口（`deepai.py` 透传到网关）：

| 接口 | 作用 |
| --- | --- |
| `POST /api/admin/accounts` | 入池一个账号（按 apiKey 幂等），调度器与模型目录立即刷新 |
| `GET/POST /api/admin/register` | 查询 / 启动批量注册（`count`、`engine`、`concurrency`、`mailProvider`、`mailDomains[]`、`proxies[]`） |
| `POST /api/admin/register/stop` | 停任务（Windows 整棵进程树） |

邮箱域名是**会学习的池子**：注册成功的域记 `ok_count` 下轮优先用，被 DeepAI 拒
（`Please use your own email address.`）的域记 `bad_count` 并被停放；`mail.tm` 每轮
还会现拉活跃域。两个免费邮箱 API 开盒都限流，所以开盒做了全局节流 + 429 退避。

| 实测 | 结果 |
| --- | --- |
| `protocol` 3 并发 | 3/3 成功，14.7 s |
| `protocol` 4 并发 | 4/4 成功，16.7 s（≈4 s/号） |
| `browser`（Camoufox，无头） | 1/1 成功，43.9 s；UA `Firefox/158.0`、`webdriver=false`、时区随代理 IP |
| 网关拉起 worker（2 并发，2 key 入池） | 2/2 成功，33.5 s，`/health` 账号数 0 → 3 |
| 一次性域名（`mail.tm` 的 `euploading.com`） | `400 {"email": ["Please use your own email address."]}` → 自动换域 |
| 离线用例 | `python -m unittest discover -s tests`，26/26 |

面板能做的不只是「点一下」：进度条 + 预计剩余（按吞吐算 ETA）、失败单元换
persona/域名/代理**自动重跑一轮**并把原因分类留档（`email_rejected` / `blocked` /
`rate_limited` / `no_api_key` / `transport`）、日志按级别筛选并一键导出 `.log`、
域名战绩画成绿/红双色条。细节见
[`tools/deepai-reg/README.md`](tools/deepai-reg/README.md)。

---

## 3.55 Allmodels 号池（新注册机 + 号池协议）

除了 DeepAI 这条线，仓库里现在还有一套**独立的 Allmodels 号池**：`dash.allmodels.io`
面板批量注册 → 自动领面板 Key → 自动发网关 `sk-dcg-` Key → 全部写进一份号池配置文件，
支持并发注册后统一导出。

### 双引擎：协议 + 指纹浏览器

两条腿跑同一套流程，产出记录同形，`engine` 字段标明来源：

| `--engine` | 行为 |
| --- | --- |
| `protocol` | httpx 走完四步，最快；Turnstile 用解题器 token |
| `browser` | 真实浏览器开 `/signup`，人类化填表/点击，Turnstile 自己过；领 key 用页内同源 `fetch` |
| `auto`（默认） | 先协议，遇 `captcha_required` / `blocked` 当场切浏览器重跑并记 `engine=browser` |

指纹内核自动选（也可 `--browser-stack` 指定），参考的都是开源项目：

- **Camoufox**（Firefox，C++ 层指纹改写，托管挑战通过率最高）<https://github.com/daijro/camoufox>
- **Patchright**（Playwright 反检测分支，Chromium 指纹 + 真实 TLS）<https://github.com/Kaliiiiiiiiii-Vinyzu/patchright>
- **Playwright Chromium** 兜底，指纹生成思路见 **browserforge** <https://github.com/daijro/browserforge>；
  同类可替代：**nodriver**（CDP）<https://github.com/ultrafunkamsterdam/nodriver>、SeleniumBase UC 模式
- 已有商业指纹浏览器？`--cdp http://127.0.0.1:50325` 直接接管 AdsPower / BitBrowser / Morelogin 的 profile

### 在编程工具里调用（MCP / HTTP）

工具层一份 schema 三个入口（[`src/allmodels-tools.mjs`](src/allmodels-tools.mjs)）：

| 入口 | 适合 |
| --- | --- |
| `node src/allmodels-mcp.mjs` | Claude Code / Cursor / Codex CLI / VS Code Copilot（MCP over stdio） |
| `POST /api/admin/allmodels/tools/:name` | Open WebUI 工具插件、n8n、Dify、自定义工作流（HTTP） |
| `node src/allmodels-cli.mjs` | 人、shell、CI |

工具：`allmodels_status`、`allmodels_register`、`allmodels_pool`、`allmodels_export`、
`allmodels_import`、`allmodels_keys`、`allmodels_mark_health`、`allmodels_remove`、`allmodels_probe`。
项目已带 [`.mcp.json`](.mcp.json)，在该目录打开 Claude Code 即自动发现；Cursor / Codex / VS Code
的配置片段见 [`docs/ALLMODELS-MCP.md`](docs/ALLMODELS-MCP.md)。

```bash
node src/allmodels-mcp.mjs --print-tools    # 看 tools/list + OpenAI function schema
curl -H "Authorization: Bearer $TOKEN" http://127.0.0.1:8787/api/admin/allmodels/tools
curl -X POST -H "Authorization: Bearer $TOKEN" -H "Content-Type: application/json" \
  -d '{"arguments":{"count":3,"engine":"auto","captcha":"capsolver","captchaKey":"KEY"}}' \
  http://127.0.0.1:8787/api/admin/allmodels/tools/allmodels_register
```

### 与既有模块的隔离（互不冲突）

- **命名空间**：文件 `allmodels-pool.json` / `allmodels-register-job.json` / `allmodels-register.lock`、
  路由 `/api/admin/allmodels/*`、环境变量 `ALLMODELS_*`、MCP 工具 `allmodels_*`、python 产物
  `tools/allmodels-reg/out/`（已 gitignore）—— 与 DeepAI 的 state、路由、变量零重叠。
- **对既有代码只做增量**：`/v1/*`、OAuth、DeepAI 账号池、Open WebUI 补丁均未改写；
  `Store` 只多了一层跨进程合并（见下）。
- **一轮一个任务**：`data/allmodels-register.lock`（pid + 6h 回收）保证网关 / MCP / CLI
  不会同时拉起两个 worker，第二个直接 `409`。
- **写不丢更新**：两个 state 文件都是「`O_EXCL` 锁 + 内容 SHA-1 比对 + 合并」——计数取 max、
  `revoked` 不回退、已发的 `gatewayKey` 不被覆盖、被删条目不被旧内存复活。
  细节见 [`docs/ALLMODELS-POOL.md`](docs/ALLMODELS-POOL.md) 第 7 节。

### 协议（从面板 SPA 还原，线上校验过）

| 步骤 | 请求 | 说明 |
| --- | --- | --- |
| 1 | `POST /api/auth/sign-up/email` + 头 `x-turnstile-token` | Better Auth；无 token 直接 `403 {"code":"captcha_failed"}` |
| 2 | `GET /api/auth/verify-email?token=…` | 邮件里的链接，302 进 SPA |
| 3 | `GET /api/me/overview` | 组织 / 项目 / guardrail（异步 provision） |
| 4 | `GET /api/me/signup-key` → `{"key":"sk_live_…"}` | 注册时自动下发的 Key |
| + | `POST /api/keys` `{displayName,projectId,guardrailId,environment}` | 需要多把 Key 时补建 |

Turnstile 站点 key `0x4AAAAAAFoFe99_cOH0RnFT`（action `signup`）；`gmail.com` /
`googlemail.com` 被拒；人机校验可选 capsolver / 2captcha / Playwright 真机渲染 / 手工 token。

### 用法

```powershell
# 网关侧一条命令：并发注册 + 自动建 key + 写号池 + 导出
node src/allmodels-cli.mjs register -n 20 -c 4 `
  --mail-provider mailtm --captcha capsolver --captcha-key $env:CAPSOLVER_KEY `
  --export jsonl

node src/allmodels-cli.mjs pool                     # 号池当前状态（脱敏）
node src/allmodels-cli.mjs export --format csv      # 账号 + key 一次性导出
node src/allmodels-cli.mjs import accounts.jsonl    # 外部账号导入并补发 key
```

```powershell
# 等价的手工 worker（网关没起也能跑，事后 push 进池）
cd tools\allmodels-reg
python -m pip install -r requirements.txt
python -m allmodels_reg doctor --captcha capsolver --captcha-key $env:CAPSOLVER_KEY
python -m allmodels_reg run -n 20 -c 4 --mail-provider imap --mail-domain example.com
python -m allmodels_reg push --push-url http://127.0.0.1:8787 --push-token <网关口令>
```

### 号池文件

`data/allmodels-pool.json`（`0600`、原子替换、`data/` 已 gitignore），schema
`dcg.pool/allmodels@1`：每个账号带面板密码、面板 Key、项目/guardrail id、健康计数、
会话 cookie，以及 `gatewayKey.plaintext`（唯一保存明文的地方）。
完整字段与写入口径见 [`docs/ALLMODELS-POOL.md`](docs/ALLMODELS-POOL.md)。

### 管理接口

| 接口 | 作用 |
| --- | --- |
| `GET /api/admin/allmodels/status` | 当前/最近一轮 job + 号池统计 |
| `POST /api/admin/allmodels/register` | 起批量注册（`count`、`concurrency`、`engine`、`browserStack`、`cdp`、`headful`、`mailProvider`、`email`、`captcha`、`captchaKey`、`baseUrl`、`proxies[]`、`keysPerAccount`、`verifyEmail`、`export`）；`export` 让任务结束后自动统一导出 |
| `POST /api/admin/allmodels/register/stop` | 停任务（Windows 整棵进程树） |
| `GET /api/admin/allmodels/pool` | 号池脱敏视图 |
| `POST /api/admin/allmodels/pool/import` | worker 回推 / 手工入池（按 `apiKey` 幂等，key 只发一次） |
| `POST /api/admin/allmodels/pool/:id/health` | 回报调用结果（计数 + 冷却） |
| `DELETE /api/admin/allmodels/pool/:id` | 移除账号 |
| `POST /api/admin/allmodels/pool/export?format=jsonl\|csv\|pool[&download=1]` | 统一导出账号 + Key |
| `GET /api/admin/allmodels/keys` | 每个账号的面板 Key 元信息 + 网关 Key 前缀 |

### 测试

| 用例 | 结果 |
| --- | --- |
| `python -m pytest tools/allmodels-reg/tests -q`（离线协议 + 引擎回落） | 13/13 |
| `node --test tests/allmodels-pool.test.mjs`（号池：去重 / 轮询 / 冷却 / 导出 / 重载） | 7/7 |
| `node --test tests/allmodels-concurrency.test.mjs`（多进程：合并 / 删除 / 撤销 / 锁 / 真双进程并发） | 14/14 |
| `node --test tests/allmodels-mcp.test.mjs`（MCP 握手 + 9 个工具调用） | 1/1 |
| `node tests/allmodels-e2e.mjs`（mock 面板 → worker → 网关入池 → 导出，含指纹浏览器与工具桥） | 11/11 |

细节见 [`tools/allmodels-reg/README.md`](tools/allmodels-reg/README.md)。

---

## 3.56 Poe 注册机（`tools/poe-reg`）

给 `poe.com` 造号：**页内 GraphQL 协议引擎** + **Camoufox 表单引擎**，产物进
**独立号池**（`data/poe-pool.json` / `out/poe-pool.sqlite3`），与 DeepAI 的 `accounts[]`
并行且互不影响 —— Poe 的凭据是 `p-b` cookie，不是 api key。

```powershell
cd tools\poe-reg
python -m poe_reg doctor
python -m poe_reg run -n 1 --engine protocol --mail-provider tempmail_lol
python -m poe_reg pool --export out/poe.jsonl
```

协议（实测抓的）：

```
GET  https://poe.com/login            → cf_clearance / p-b
POST /api/gql_POST  …sendVerificationCodeMutation_Mutation   {emailAddress, phoneNumber:null, recaptchaToken}
POST /api/gql_POST  …signupWithVerificationCodeMutation_Mutation {verificationCode, emailAddress, phoneNumber:null, subscriptions:[]}
```

**必须说清的现实**：邮箱段全自动（含一次性域名，验证码秒到），但 Poe 之后要手机号，
且 `signupWithVerificationCode` 会返回 `recently_verified_phone_number_not_found` ——
没有短信供应商时账号停在 `email_verified` / `phone_required`，cookie 与 formkey 已入库，
后续用 `sms.provider = manual | sms_activate | smspool` 补完即可变 `active`。

防封策略针对 Poe 的评分维度（IP/设备声誉）：一号一出口、同代理 120 s 冷却、
`automated_request_detected` 直接 bench 该代理 15 分钟、CF 没过 bench 10 分钟、
每次新指纹不复用 profile、账号间 25–55 s 抖动、`p-b` 入库不在异地重登。

WebUI 同一页下面新增「Poe 号池（独立）」卡片：注册（数量/引擎/邮箱/短信）、
进度条 + ETA、日志与导出、号池表格（状态可手动改 `active` / 删除）。

---

## 3.57 Linux 一键部署

```bash
curl -fsSL https://raw.githubusercontent.com/addxiaoyi/deepai2api/main/deploy/install.sh | sudo bash
# 或者带上 WebUI（会装 docker 并构建 fork）
curl -fsSL …/install.sh | sudo bash -s -- --with-docker --with-camoufox
```

装完的样子：

| 项 | 位置 |
| --- | --- |
| 网关 | `/opt/deepai2api`，`systemctl status deepai-gateway`（默认 0.0.0.0:8787） |
| WebUI | `docker compose up` 的 fork，`systemctl status deepai-webui`（:3000） |
| 口令/环境 | `/etc/deepai2api/env`（0600，首次安装自动生成管理口令） |
| 数据 | `/var/lib/deepai2api/data`（`gateway-state.json`、`register-job.json`） |
| 注册 worker | `/opt/deepai2api/.venv/bin/python` + `tools/deepai-reg` |

脚本是幂等的：`--source .` 从当前目录装、`--dir/--port/--user` 可改、
`--rotate-token` 换口令、`--with-camoufox` 顺带拉 1.3 GB 的浏览器内核；
没有 systemd 或非 root 会直接报错退出。文件在 [`deploy/`](deploy/install.sh)。

---

## 3.58 四个号池的共存约定

仓库里现在同时有 DeepAI、Allmodels、Poe、MoonAI 四套"注册 + 号池"，它们**只共享代码，不共享状态**：

| 边界 | DeepAI 池 | Allmodels 池 | Poe 池 | MoonAI 池 |
| --- | --- | --- | --- | --- |
| 池文件 | `data/gateway-state.json` | `data/allmodels-pool.json` | `data/poe-pool.json` | `data/moonai-pool.json` |
| 凭据形态 | `api_key` | dashboard key + 网关 key | `p-b` cookie | `access_token` + `api_…` + 网关 key |
| 管理路由 | `/api/admin/*` | `/api/admin/allmodels/*` | `/api/poe/*` | `/api/admin/providers/*` |
| worker | `tools/deepai-reg`（`python -m deepai_reg`） | `tools/allmodels-reg` | `tools/poe-reg`（`python -m poe_reg`） | `tools/moonai-reg`（`python -m moonai_reg`） |
| 环境变量 | `DEEPAI_*` | `ALLMODELS_*` | `POE_*` | `MOONAI_*` |
| 消费方 | `scheduler.mjs` → `/v1` | `provider-relay.mjs` | 外部（cookie 直接给客户端） | `provider-relay.mjs` → `/p/moonai/v1` |

保证不打架的几处实现细节：

* **写入口独立**：网关每个池一个 writer 类（`Store` / `AllmodelsPool` / `PoePool`），
  push 端点也各自独立（`/api/admin/accounts` vs `/api/allmodels/...` vs `/api/poe/accounts`），
  一个池的 upsert 不会碰另一个池的文件；`data/` 下三个 JSON 可以同时存在。
* **路由不重叠**：全部是 exact-match 或带前缀的正则，`/api/poe/*` 与
  `/api/admin/allmodels/*` 没有共同前缀；三者都走同一套 admin 鉴权（会话或 Bearer token）。
* **环境变量不串**：`Registrations` 按工具取自己的覆盖变量 ——
  设了 `DEEPAI_REGISTER_DIR` 不会把 Poe worker 指到 DeepAI 的目录，反之亦然
  （`POE_REGISTER_DIR` / `POE_PYTHON` 只作用于 Poe）。
* **任务状态分文件**：`data/register-job.json`、`data/allmodels-*.json`、
  `data/poe-register-job.json`，重启后各自恢复，互不覆盖。
* **WebUI 一页三卡**：DeepAI 池 / 批量注册（DeepAI） / Poe 号池，各自独立轮询与按钮，
  取消或报错只影响自己那块。
* **Python 包不互相 import 状态**：`poe_reg` 只在两处复用 `deepai_reg` 的**纯函数层**
  （邮箱 provider、persona），不读写对方的 SQLite。`moonai_reg` 自成一体（只在包内引用）。

实测（合并后的工作树，同一进程）：

```
GET  /health                    200  含 accounts / allmodels / poe 三段
GET  /api/admin/state           200  allmodels 池状态在内，poe 不在内
GET  /api/admin/allmodels/status 200
GET  /api/admin/providers       200
GET  /api/poe/health            200  {"total":0,...}
GET  /api/admin/register        200  toolDir=tools/deepai-reg
GET  /api/poe/register          200  toolDir=tools/poe-reg
POST /api/poe/accounts          → 只写 poe-pool.json
POST /api/admin/accounts        → 只写 gateway-state.json（poe 计数不变）
```

回归：`node tests/e2e.mjs` **28/28**、`node --test tests/allmodels-*.test.mjs` **22/22**、
`python -m unittest`（deepai 26 / poe 8）全绿。

---

## 3.59 MoonAI 号池（注册机 + 反代出 Key）

第四条独立的号池线：**mooonai.com（MoonAI）**。它的形态和前三条都不一样，所以配了
单独的一层翻译：

```
注册机(tools/moonai-reg)               网关                                mooonai
 协议腿 httpx ─┐            /api/admin/providers/moonai/pool/import    auth.mooonai.com
 浏览器腿 ─────┴─ NDJSON ──▶ data/moonai-pool.json ─┐                    core.mooonai.com
                                   │                │                    /api/v1/services/
                                   │                └─ sk-dcg-… ──▶ /p/moonai/v1/*  llm-proxy
                                   └─ 令牌续期 / 换号 / 冷却
```

| 维度 | 说明 |
| --- | --- |
| 调模型凭据 | **OAuth `access_token`**（不是 `api_…`，后者只给 Agent 渠道 Webhook 用） |
| 上游协议 | 自有形状 `{content, finish_reason, usage}`，**无 choices、无 stream、无 tools** |
| 网关做的事 | 剥字段 → 工具调用模拟 → SSE 合成 → 令牌续期 → 换号重试 |
| 注册 | 表单 + 邮箱验证 + OAuth；**无人机校验** |
| 模型 | `gpt-4.1` / `gpt-4o` / `gpt-4o-mini` / `gpt-5-nano` / `o3-mini` / `o4-mini` |

### 三条命令

```powershell
# 1) 注册（协议腿 + 域名邮箱；被拦自动切指纹浏览器）
node src/moonai-cli.mjs register -n 10 -c 3 --engine auto `
  --mail-provider imap --mail-domains example.com --push

# 2) 看号池 / 自检
node src/moonai-cli.mjs pool
node src/moonai-cli.mjs probe

# 3) 打印编程工具接入配置（cline / continue / claude-code / aider / openai / node / curl）
node src/moonai-cli.mjs config --tool cline
```

### 双引擎

| `--engine` | 行为 |
| --- | --- |
| `protocol` | httpx 走完注册→验证→登录→OAuth→令牌→组织→Key，最快 |
| `browser` | 真实浏览器填表、点验证链接、在页面上下文里 `fetch` 换令牌与建 Key |
| `auto`（默认） | 先协议；遇 `blocked` / `captcha_required` / `no_token` 当场切浏览器，同一个人重跑 |
| `hybrid` | 每个号都走浏览器（更慢更稳） |

指纹内核：**Camoufox** / **Patchright** / Playwright，或 `--cdp` 直接接管
AdsPower / BitBrowser / Morelogin 的 profile。时区与语言跟随出口（`geoAlign`）。

### 邮箱

`imap` / `domain`（自有域名 catch-all，**推荐**）、`mailtm`（兜底，投递不稳）、
`fixed`。验证链接是 `auth.mooonai.com/auth/verify-email/?token=<uuid>`，
邮件里可能被 `*.mjt.lu` 包一层 —— worker 会跟随 302 解开。

### 防封

域名滑动窗口限速 + 健康分（成功 +1 / 失败 −2，低于阈值停用）、同出口串行、
账号间抖动、失败按 `kind` 退避（`rate_limited` 20–45s、`blocked` 20–40s）、
cookie 复用、每号全新指纹。整块参数可用 `--anti-ban '<json>'` 覆盖。

### 反代出 Key（编程工具直接能用的那部分）

```
GET  /p/moonai/v1/models
POST /p/moonai/v1/chat/completions     ← Cline / Roo / Continue / Aider / OpenAI SDK
POST /p/moonai/v1/messages             ← Claude Code（Anthropic 协议）
```

客户端用网关签的 `sk-dcg-…`，上游用号池里的 `access_token`：

```bash
curl -sS http://127.0.0.1:8787/p/moonai/v1/chat/completions \
  -H "Authorization: Bearer sk-dcg-…" -H "Content-Type: application/json" \
  -d '{"model":"gpt-4o-mini","messages":[{"role":"user","content":"hi"}],"stream":true}'
```

因为上游**不吃 `tools`**，网关把工具 schema 编进系统提示、回程解析成
`tool_calls`（`finish_reason:"tool_calls"`，流式按 `arguments` 增量切片），
所以依赖原生工具调用的编程客户端也能跑；上游**没有流**，SSE 由网关合成。

### 管理接口

| 接口 | 作用 |
| --- | --- |
| `GET /api/admin/providers` | 所有 provider 概览（含 moonai 号池统计） |
| `GET /api/admin/providers/moonai` | 详情：端点 / python / job / 号池 |
| `POST /api/admin/providers/moonai/register` | 起批量注册（参数同 worker） |
| `POST /api/admin/providers/moonai/register/stop` | 停任务（Windows 整棵进程树） |
| `GET /api/admin/providers/moonai/pool` | 号池脱敏视图（令牌只报存在与过期时间） |
| `POST /api/admin/providers/moonai/pool/import` | worker 回推 / 手工入池（按 apiKey→email 幂等） |
| `POST /api/admin/providers/moonai/pool/:id/health` | 回报调用结果（计数 + 冷却） |
| `DELETE /api/admin/providers/moonai/pool/:id` | 移除账号 |
| `POST /api/admin/providers/moonai/pool/export?format=jsonl\|csv\|pool` | 统一导出 |
| `POST /api/admin/providers/moonai/probe` | 用号池里的令牌打一次上游 |

### 与现有模块的关系（互不冲突）

| 资源 | moonai | 其它三条线 |
| --- | --- | --- |
| 号池文件 | `data/moonai-pool.json` | `gateway-state.json` / `allmodels-pool.json` / `poe-pool.json` |
| schema | `dcg.pool/moonai@1` | `dcg.pool/allmodels@1` 等 |
| 注册锁 | `data/moonai-register.lock` | 每个 provider 一把，互不遮挡 |
| job 状态 | `data/moonai-register-job.json` | 同名规则 |
| worker | `tools/moonai-reg/moonai_reg` | `tools/<provider>-reg` |
| 路由 | `/p/moonai/v1/*`、`/api/admin/providers/*` | `/v1/*`、`/api/admin/allmodels/*`、`/api/poe/*` |

`DCG_PROVIDERS=moonai` 可只装这一条线；`config.providerEndpoints` /
`DCG_PROVIDER_ENDPOINTS` 可把端点改到自建反代（测试也走这条）。

### 测试

| 用例 | 结果 |
| --- | --- |
| `node --test tests/moonai-pool.test.mjs`（适配层 + 号池 + 合并防丢号） | 17/17 |
| `node --test tests/moonai-fork-surface.test.mjs`（面板接线：路由/导出/挂载/块配对） | 6/6 |
| `node --test tests/moonai-svelte-build.test.mjs`（真实 Svelte 编译器校验两个组件） | 1/1 |
| `node tests/moonai-e2e.mjs`（mock 上游 → 反代：流式/工具/换号/续期） | 10/10 |
| `node tests/moonai-admin.test.mjs`（管理面 + 既有路由不冲突） | 11/11 |
| `node tests/moonai-worker-e2e.mjs`（注册机 → 网关 → 反代出 Key，含面板按钮路径） | 11/11 |
| `python -m unittest discover -s tools/moonai-reg/tests`（协议离线） | 6/6 |
| `node tests/e2e.mjs`（既有网关回归） | 28/28 |

一键：`npm run test:moonai`。协议取证表与字段定义见
[`docs/MOONAI-POOL.md`](docs/MOONAI-POOL.md)，worker 用法见
[`tools/moonai-reg/README.md`](tools/moonai-reg/README.md)。

---

## 4. OpenWebUI 二次开发（fork）

`vendor/open-webui` = **Open WebUI 0.13.4 上游 + DeepAI provider**。
不是插件，是「一级公民」：管理页、API 路由、连接自动写入都在里面。

对上游的改动**只有 14 行**：

```
backend/open_webui/main.py                  +2   注册 deepai router
src/lib/components/admin/Settings/AdminTabIcon.svelte  +2   新 tab 图标
src/lib/components/chat/SettingsModal.svelte +10   import / 分组 / tab / 渲染分支
```

新增 4 个文件：

```
backend/open_webui/routers/deepai.py                  管理侧代理（OAuth/账号/Key/接入）
src/lib/apis/deepai/index.ts                          前端 API 客户端
src/lib/components/admin/Settings/DeepAI.svelte       设置页
DEEPAI_FORK.md                                        改动清单与重新套用说明
```

设置页做的事：网关地址/管理口令 → 账号池（OAuth 登录 / 移除 / 健康状态）→ Key 管理
（备注、rpm、日限额、撤销、删除，创建后一次性显示）→ 「接入并启用模型」（自动建 Key 并写进
`openai.api_base_urls` / `openai.api_keys`）→ 模型目录（含 `X Theos/次` 价格）。

**构建验证**：`npm ci --engine-strict=false && npm run build` → `✓ built in 1m 48s`，
产物 `build/_app/immutable/nodes/2.B2k19R7x.js` 内含本页面全部文案与 DOM id；
`svelte-check` 对本项目改动的 4 个文件 **0 error**（上游自带 5374 条既有类型噪音，
本项目文件只触发了 1 条「未使用 prop」警告，已加注释说明）。

> Node 版本：Open WebUI 的 `engines` 是 `<=22.x`，本机 Node 26 需要
> `npm ci --engine-strict=false`。Docker 构建不受影响。

---

## 5. 联网搜索

```bash
curl -sS http://127.0.0.1:8787/v1/search \
  -H "Authorization: Bearer sk-dcg-…" -H "Content-Type: application/json" \
  -d '{"query":"CVE-2025 heap overflow exploit technique","num_results":3}'
```

返回 `{text, sources[]}`，内容是通道自己抓的网页摘要（实测能返回 HackTricks / 赛事 writeup 等）。
同一端点还有 `/v1/web-search`、`/v1/find-service`。

---

## 6. 目录结构

```
src/
  index.mjs       CLI（起服务 / --list-models / --self-test / --show-admin-token / --import-app-account）
  server.mjs      HTTP：管理台、/api/admin/*、/v1/*、/v1/messages、/auth-callback
  scheduler.mjs   账号调度：候选排序、粘性、冷却、故障切换
  lane.mjs        通道客户端（按账号隔离签到 nonce 与计费槽）
  store.mjs       持久化 v2（accounts[] / keys[] / sessions，原子写）
  anthropic.mjs   OpenAI ⇄ Anthropic 协议转换（含 SSE 事件序列）
  oauth.mjs       PKCE + code 交换
  keys.mjs        sk-dcg-… 铸造与哈希

  config/credentials/machine/dispatcher/semaphore
  allmodels-pool.mjs    Allmodels 号池：落盘格式、去重、健康计数、导出、网关 Key 绑定
  allmodels-register.mjs 批量注册控制器（起 python worker、流式事件、入池）
  allmodels-cli.mjs    register / pool / export / import / keys
  allmodels-tools.mjs  工具层：9 个工具的 schema + handler（MCP / HTTP 共用）
  allmodels-mcp.mjs    MCP server（stdio，Claude Code / Cursor / Codex / VS Code）
patches/
  0001-register-deepai-provider.patch   对上游的 14 行改动
  files/                                新增的 4 个文件 + overlay Dockerfile
vendor/open-webui                      由 apply-fork.ps1 还原（不入库）
tests/
  e2e.mjs         27 项，打 mock 上游，不碰真实账号
  smoke.mjs       真实上游冒烟
  mock-upstream.mjs
  allmodels-pool.test.mjs  号池协议单测
  allmodels-e2e.mjs        mock 面板 → worker → 入池 → 导出
  mock-allmodels.mjs       面板 mock（也可单独跑起来手测）
scripts/
  openwebui.ps1   构建/启动 fork
  screenshot.mjs  headless Chrome 截图 + 渲染后 DOM 快照
tools/deepai-reg/ 批量注册机（协议引擎 + Camoufox，产物灌进 accounts[]）
tools/allmodels-reg/ Allmodels 注册机（Better Auth + Turnstile，产物灌进号池文件）
tools/poe-reg/     Poe 注册机（页内 GraphQL + Camoufox，独立 cookie 号池）
tools/moonai-reg/  MoonAI 注册机（表单 + 邮箱验证 + OAuth，协议/指纹浏览器双引擎）
src/providers.mjs        provider 运行时装配（每个 provider 一份池 + 控制器）
src/provider-admin.mjs   `/api/admin/providers/*` 管理面
src/moonai-adapter.mjs   MoonAI 上游形状 ⇄ OpenAI 形状（剥字段 / 工具模拟 / SSE 合成）
src/moonai-cli.mjs       MoonAI 命令行（register / pool / export / import / probe / config）
deploy/           Linux 一键部署：install.sh + deepai-gateway.service + deepai-webui.service
docs/ALLMODELS-POOL.md 号池协议（schema / 写入口径 / 接口）
docs/MOONAI-POOL.md     MoonAI 号池协议 + 注册取证表 + 反代行为
docs/ALLMODELS-MCP.md  编程工具接入（MCP / HTTP 工具桥 / 各家配置片段）
.mcp.json              项目级 MCP 配置（Claude Code 直接发现）
docker-compose.yml  overlay 镜像（官方 v0.13.4 + 我们的后端/前端文件）
scripts/apply-fork.ps1  一键还原 fork（克隆 + 打补丁 + 构建）
```

---

## 7. 测试与当前状态

```
node tests\e2e.mjs     28/28   管理台登录、伪造 state 拒绝、OAuth 交换、铸 Key、
                              转发、Anthropic（JSON+SSE）、billing、限额、
                              粘性会话、故障切换、全灭 503、撤销、解绑
node tests\smoke.mjs   6/6     真实上游：目录 / 非流式 / 流式 / 配额
node --test tests\allmodels-pool.test.mjs   7/7   号池协议 + 重载保留 id
node --test tests\allmodels-concurrency.test.mjs  14/14  多进程合并 / 删除 / 锁
node --test tests\allmodels-mcp.test.mjs    1/1   MCP 握手 + 工具调用
node tests\allmodels-e2e.mjs                11/11  批量注册 → 入池 → 导出（含指纹浏览器、工具桥）
python -m pytest tools\allmodels-reg\tests -q  13/13 面板注册协议 + 引擎回落
node --test tests\moonai-pool.test.mjs      17/17  MoonAI 适配层（剥字段/工具模拟/SSE）+ 号池
node --test tests\moonai-fork-surface.test.mjs  6/6  Open WebUI 面板接线（路由/导出/挂载）
node --test tests\moonai-svelte-build.test.mjs  1/1  真实 Svelte 编译（需 svelte@4 + preprocess）
node tests\moonai-e2e.mjs                   10/10  mock 上游 → 反代：流式 / 工具调用 / 换号 / 续期
node tests\moonai-admin.test.mjs            11/11  provider 管理面 + 既有路由不冲突
node tests\moonai-worker-e2e.mjs            11/11    注册机 → 网关入池 → 反代出 Key（流式+非流式）
python -m unittest discover -s tools\moonai-reg\tests   6/6  MoonAI 注册协议（本地 mock）
```

真实上游已验证：`/v1/chat/completions`（流式+非流式）、`/v1/messages`（返回标准
Anthropic message，含 `thinking` block）、`/v1/search`、`/v1/models`（28 个模型）。

---

## 8. 排障

| 现象 | 原因 / 处理 |
| --- | --- |
| 容器里连不上网关 | 容器访问宿主机不能用 `127.0.0.1`；网关要 `--host 0.0.0.0` |
| 局域网 IP 访问超时 | Windows 系统代理把该网段也代理了，用 `-NoProxy` 试 |
| `docker compose` 连不上 | Docker Desktop 没启动（CLI 在也没用） |
| `npm ci` 报 EBADENGINE | Node >22，加 `--engine-strict=false` |
| 设置页看不到 | fork 没还原：先跑 `.\scripts\apply-fork.ps1`，再 `docker compose up -d --build` |
| 模型列表为空 | 网关没连账号；DeepAI 设置页先 OAuth 登录，再点「接入并启用模型」 |
| 429 `rate_limit` | 该 Key 触发了 rpm/日限额；管理台改限额或换 Key |

---

## 9. 说明

- 网关只做协议搬运：请求体除 `model` 归一化、`stream` 补全、`prompt_cache_key` 补齐外原样透传。
- 上游拒绝会带状态码与 `error.code`（`auth_required`、`check_in_refused`、`machine_required`、
  `theos_exhausted`、`session_busy` …），不吞消息；账号级拒绝会标记账号并切换。
- `data/gateway-state.json` 存账号 api_key 与 Key 哈希（0600）。管理口令默认自动生成，暴露前请换强口令。
- MoonAI 线的 `/p/moonai/v1/*` 把上游"无 choices / 无 stream / 无 tools"的接口翻译成
  OpenAI 形状；工具调用是**提示词模拟**（不是原生 function calling），复杂多轮工具链
  建议优先用支持原生工具的 provider。
- 这是对本机、自己账号的通道复用；DeepAI 服务条款第 4 条限制第三方应用接入，使用范围自行评估。



