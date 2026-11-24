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
cd D:\qwq\项目\deepaiapi
npm install

# 网关（只提供 API，没有自带 UI）
node src\index.mjs --host 0.0.0.0 --port 8787

# 界面 = Open WebUI fork（构建 overlay 镜像，秒级）
docker compose up -d --build
```

打开 **http://localhost:3000** → 注册管理员 → **设置 → Admin → DeepAI Code** →
填网关地址/口令 → 登录 DeepAI → 创建 Key → **接入并启用模型**。

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
vendor/open-webui  Open WebUI 0.13.4 + DeepAI provider（二次开发）
tests/
  e2e.mjs         27 项，打 mock 上游，不碰真实账号
  smoke.mjs       真实上游冒烟
  mock-upstream.mjs
scripts/
  openwebui.ps1   构建/启动 fork
  screenshot.mjs  headless Chrome 截图 + 渲染后 DOM 快照
docker-compose.yml  overlay 镜像（官方 v0.13.4 + 我们的后端/前端文件）
vendor/open-webui/Dockerfile.deepai   overlay 构建文件
```

---

## 7. 测试与当前状态

```
node tests\e2e.mjs     27/27   管理台登录、伪造 state 拒绝、OAuth 交换、铸 Key、
                               转发、Anthropic（JSON+SSE）、billing、限额、
                               粘性会话、故障切换、全灭 503、撤销、解绑
node tests\smoke.mjs   6/6     真实上游：目录 / 非流式 / 流式 / 配额
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
| 设置页看不到 | fork 没构建：`cd vendor/open-webui && npm run build`，或 `docker compose up -d --build` |
| 模型列表为空 | 网关没连账号；DeepAI 设置页先 OAuth 登录，再点「接入并启用模型」 |
| 429 `rate_limit` | 该 Key 触发了 rpm/日限额；管理台改限额或换 Key |

---

## 9. 说明

- 网关只做协议搬运：请求体除 `model` 归一化、`stream` 补全、`prompt_cache_key` 补齐外原样透传。
- 上游拒绝会带状态码与 `error.code`（`auth_required`、`check_in_refused`、`machine_required`、
  `theos_exhausted`、`session_busy` …），不吞消息；账号级拒绝会标记账号并切换。
- `data/gateway-state.json` 存账号 api_key 与 Key 哈希（0600）。管理口令默认自动生成，暴露前请换强口令。
- 这是对本机、自己账号的通道复用；DeepAI 服务条款第 4 条限制第三方应用接入，使用范围自行评估。
