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

## 3.6 Linux 一键部署

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
deploy/           Linux 一键部署：install.sh + deepai-gateway.service + deepai-webui.service
docs/ALLMODELS-POOL.md 号池协议（schema / 写入口径 / 接口）
docs/ALLMODELS-MCP.md  编程工具接入（MCP / HTTP 工具桥 / 各家配置片段）
.mcp.json              项目级 MCP 配置（Claude Code 直接发现）
docker-compose.yml  overlay 镜像（官方 v0.13.4 + 我们的后端/前端文件）
scripts/apply-fork.ps1  一键还原 fork（克隆 + 打补丁 + 构建）
```

---

## 7. 测试与当前状态

```
node tests\e2e.mjs     27/27   管理台登录、伪造 state 拒绝、OAuth 交换、铸 Key、
                              转发、Anthropic（JSON+SSE）、billing、限额、
                              粘性会话、故障切换、全灭 503、撤销、解绑
node tests\smoke.mjs   6/6     真实上游：目录 / 非流式 / 流式 / 配额
node --test tests\allmodels-pool.test.mjs   7/7   号池协议 + 重载保留 id
node --test tests\allmodels-concurrency.test.mjs  14/14  多进程合并 / 删除 / 锁
node --test tests\allmodels-mcp.test.mjs    1/1   MCP 握手 + 工具调用
node tests\allmodels-e2e.mjs                11/11  批量注册 → 入池 → 导出（含指纹浏览器、工具桥）
python -m pytest tools\allmodels-reg\tests -q  13/13 面板注册协议 + 引擎回落
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
- 这是对本机、自己账号的通道复用；DeepAI 服务条款第 4 条限制第三方应用接入，使用范围自行评估。

