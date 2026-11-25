# Blackbox AI 号池与注册协议（`dcg.pool/blackbox@1`）

本文记录 **blackbox.ai（BLACKBOX AI）** 这条线在本仓库里的形态：站点协议取证、
号池文件、注册机、防封策略、反代出口，以及与其它 provider 的隔离边界。

- 号池：`data/blackbox-pool.json`（`0600`、原子写、`data/` 已 gitignore）
- 注册锁：`data/blackbox-register.lock`
- job 状态：`data/blackbox-register-job.json`
- worker：`tools/blackbox-reg`（`python -m blackbox_reg`）
- 反代：`/p/blackbox/v1/*`（网关 Key 鉴权）
- 管理面：`/api/admin/providers/blackbox/*`

上面所有名字都由 `src/provider-catalog.mjs` 的 `id: "blackbox"` 派生，和
`allmodels` / `poe` / `deepai` / `wavespeed` / `novita` / `moonai` / `zo` 零重叠。

---

## 1. 站点协议取证（2026-11，全部线上实测）

> 本机 DNS 对部分 `*.blackbox.ai` 返回过错误 A 记录（例如 `app.blackbox.ai` 落到
> 一个不相关的 IP）。下面是**用 DoH 校正后**的结果；自己复现时若遇 TLS 握手失败，
> 先 `curl https://dns.google/resolve?name=app.blackbox.ai&type=A` 对一下。

| # | 请求 | 实测响应 | 说明 |
| --- | --- | --- | --- |
| 1 | `GET https://app.blackbox.ai/` | `200`，Next.js App Router | 应用主体；路由 `/`、`/keys`、`/activity`、`/models`、`/restrict-access`、`/organization` |
| 2 | `GET /api/auth/providers` | `200 {"google":{"id":"google","name":"Google","type":"oidc",…}}` | **只有 Google 一个 provider** |
| 3 | `POST /api/auth/sign-up/email` | `400 "Bad request."`（三种 body 组合都一样） | **邮箱密码注册不存在** |
| 4 | `GET /api/auth/csrf` | `200 {"csrfToken":"…"}` | Auth.js v5 的 CSRF |
| 5 | `POST /api/auth/signin/google`（form `csrfToken`,`callbackUrl`） | `302` → `accounts.google.com/o/oauth2/v2/auth?client_id=985058387028-iaa60lr5e586kvhjp085qcojv3jv2p7h.apps.googleusercontent.com&redirect_uri=https%3A%2F%2Fapp.blackbox.ai%2Fapi%2Fauth%2Fcallback%2Fgoogle&code_challenge_method=S256&scope=openid+profile+email` | 同时 `Set-Cookie: __Secure-authjs.state` / `.pkce.code_verifier` / `.callback-url` |
| 6 | `GET /api/auth/session` | `200 null`（未登录） | 登录后返回 `{user:{id,email,name,plan?}}` |
| 7 | `GET /api/auth/session-challenge` | `401 {"status":"unauthenticated"}` | 会话探针 |
| 8 | `GET /api/auth/*` 的登录页 | 无 Turnstile / reCAPTCHA / hCaptcha | app 侧**没有**人机校验，风控全在 Google 那半程 |
| 9 | `POST https://enterprise.blackbox.ai/chat/completions` | `401/403 {"error":{"message":"blackbox.Error: Invalid API key","type":"auth_error"}}` | OpenAI 兼容推理入口 |
| 10 | `POST https://enterprise.blackbox.ai/v1/chat/completions` | 同上（`/v1` 前缀也可用） | 反代层就用这条 |
| 11 | `GET enterprise.blackbox.ai/v1/models`、`/models` | `404 {"error":"Not Found"}` | **上游没有模型列表接口** → 用描述符白名单 |
| 12 | `api.blackbox.ai`（任意路径） | `404`，`server: Google Frontend` | 历史入口，现已不服务业务（CLI 里仍是默认 baseUrl） |
| 13 | `console.blackbox.ai/login` | `200`，`Email me a code` + `cf-turnstile` | GPU Console 的 Email OTP 腿，页面写明"公司需已在 Console 开通，或管理员加你" |
| 14 | `chat.blackboxcli.ai`（CLI 设备码 OAuth） | NXDOMAIN | 该 OAuth 服务已下线，CLI 里只剩常量 |

结论一句话：**Blackbox 的"注册"= Google OAuth 授权**，落地成 app 侧的
`__Secure-authjs.session-token`；真正的凭据是面板签发的 `sk-…` Key。

---

## 2. 号池文件

```json
{
  "schema": "dcg.pool/blackbox@1",
  "version": 1,
  "provider": "blackbox",
  "baseUrl": "https://app.blackbox.ai",
  "apiBase": "https://enterprise.blackbox.ai",
  "updatedAt": "2026-11-25T14:00:00.000Z",
  "accounts": [
    {
      "id": "9f2c…",
      "email": "ada@own.example",
      "apiKey": "sk-…",
      "keyId": "key_…",
      "keyPrefix": "sk-abcde",
      "keyLast4": "9f31",
      "keySource": "created",
      "extraKeys": [],
      "plan": "pro",
      "userId": "u-…",
      "engine": "protocol",
      "status": "active",
      "cookies": [{ "name": "__Secure-authjs.session-token", "value": "…", "domain": "app.blackbox.ai", "path": "/" }],
      "requests": 3,
      "failures": 0,
      "healthy": true,
      "cooldownUntil": 0,
      "gatewayKey": { "id": "…", "label": "blackbox:ada@own.example", "prefix": "sk-dcg-AbCdEfGh", "plaintext": "sk-dcg-…" }
    }
  ]
}
```

约定（与 `docs/ALLMODELS-POOL.md` 一致，只是多了 blackbox 自己的字段）：

| 字段 | 说明 |
| --- | --- |
| `apiKey` | 面板 `sk-…` Key，反代层用它调上游 |
| `keySource` | `listed`（面板列出来的）/ `created`（worker 新建的）/ `dom`（浏览器腿从页面读的） |
| `plan` / `userId` | 从 `/api/auth/session` 读出来的套餐与外部用户 id，面板直接显示 |
| `cookies` | app 会话；复用它可以跳过整段 OAuth（`antiban.reuseSession`） |
| `gatewayKey.plaintext` | 唯一存明文的地方；`Store` 里只有 SHA-256 |

写入口：

| 入口 | 触发 | 行为 |
| --- | --- | --- |
| `POST /api/admin/providers/blackbox/register` | 面板 / CLI / 工具 | 起 python worker，逐条 `account` 事件入池 |
| `POST /api/admin/providers/blackbox/pool/import` | worker 回推 / 手工 | `importAccount()` 按 `apiKey`→`email` 幂等合并 |
| `POST /api/admin/providers/blackbox/pool/:id/health` | 反代层回报 | 计数 + 冷却 |
| `DELETE /api/admin/providers/blackbox/pool/:id` | 管理 | 移除条目（不动已发的网关 Key） |

---

## 3. 三条注册腿

`--engine` 决定走哪条；`auto` 先协议、被拦当场切浏览器。

| 引擎 | 需要什么 | 适用 |
| --- | --- | --- |
| `protocol` | `--session-file`（app 会话 cookies）或 `--google-cookies`（已登录 Google）或 `--authorization-code` | **最快**，批量刷 Key 的主力；一次 OAuth 之后可以一直复用会话 |
| `browser` | Google 账号密码（`--google-pool`）+ 指纹浏览器内核 | **最稳**，真的点一遍 Google 登录；能过设备验证之外的绝大多数风控 |
| `console` | work email + Turnstile 解题器 | GPU Console 的 OTP 腿；**仅当公司已开通**，默认不走 |

### 为什么协议腿吃"会话"而不是"账号密码"

Google 登录页的交互（identifier → password → 设备验证 → 同意）在纯 HTTP 下没有
稳定实现，且强风控下必被拦。所以 worker 把"人"的部分交给浏览器腿，把"领 Key"
的部分留给协议腿——两者产出的号池记录同形，`engine` 字段标明来源。

### 浏览器腿：为什么默认 Camoufox

<https://camoufox.com/>（Firefox + **C++ 层**指纹改写，不是 JS 注入）。选它的理由：

| 点 | 说明 |
| --- | --- |
| 不是 Chromium | 反爬厂商大量按 "Chromium vs 真 Chrome" 区分；Firefox 系更少被针对性打 |
| Juggler < CDP | Playwright 的 Firefox 通道比 CDP 更底层，JS 侧泄漏更少 |
| 指纹自洽 | 由 BrowserForge 按真实流量分布生成设备/屏幕/WebGL/字体，不会出现"Windows UA + Apple GPU" |
| geoip | `geoip=True` 直接按出口 IP 定经度纬度/时区/locale，比我们手拍时区靠谱 |
| humanize | 自带人类化鼠标轨迹；`disable_coop=True` 才能点 cross-origin iframe 里的 Turnstile |

因此 worker 的 camoufox 分支有两条硬约束（都写进了单测）：

1. **绝不注入 Chrome UA / sec-ch-ua**——Camoufox 是 Firefox，注进去等于自曝；
2. **`geoip` 与 `locale` 不同时给**——时区/语言必须由出口 IP 决定。

安装与内核：

```bash
python -m pip install "camoufox[geoip]"
camoufox fetch          # pip 包不含内核；GitHub 限流时先 export GITHUB_TOKEN=…
```

`python -m blackbox_reg doctor` 会分别报告 pip 包与内核两件事。

### 面板 Key 的获取（`protocol` 腿）

面板没有公开的 Key 接口文档，所以 worker 做**候选路径探测**：

```
/api/keys  /api/v1/keys  /api/user/api-keys  /api/user/keys  /api/agent-api/keys
/api/dashboard/keys  /api/me/keys  /api/api-keys  /api/settings/api-keys  …
```

判据是"HTTP 2xx 且响应里带 `sk-…` 或 keys 数组"。命中后写进
`data/blackbox-reg/key-endpoints.json`，后续直接复用，不再打空炮。人工也可以：

```powershell
python -m blackbox_reg run -n 1 --keys-path /api/keys --keys-create-path /api/keys `
  --keys-create-body '{"name":"bbx-1"}' --session-file cookies.json
```

浏览器腿不依赖这个接口：它在 `/dashboard`、`/keys` 页面上直接扫 `sk-…` 文本，
扫不到就点 `Get an API Key` / `Create key` 再扫一次。

---

## 4. 防封策略（`--anti-ban` / `DEFAULT_ANTI_BAN`）

| 层 | 参数 | 做什么 |
| --- | --- | --- |
| 身份 | `proxyStickyPerIdentity` | 一个 Google 身份全程一个出口；Google 最忌"同一账号跨国跳" |
| 域名 | `mailDomains` / `domainRpm` / `domainRph` / `domainHealthFloor` | 域名滑动窗口限速 + 健康分（成功 +1、失败 -2），低于阈值自动停用 |
| 出口 | `ipConcurrency` / `proxyRotate` | 同一出口串行，失败多的代理排到后面 |
| 节奏 | `jitterMs` / `oauthPauseMs` / `cooldownAfterBatch` | 注册间隔抖动、授权页停留、批次冷却 |
| 接口 | `keyCreateRpm` / `probeAfterKey` / `reuseSession` | 单账号建 Key 频控；建完自检一次；会话复用避免重复 OAuth |
| 退避 | `backoff{rate_limited,blocked,captcha_required,oauth_denied,session_expired,…}` | 按失败分类退避，`session_expired`/`oauth_denied` 不退避而是换腿 |

另外两条经验写在代码里：

- 401 一律当 `session_expired`（会话没了，不是 Key 错），直接换腿而不是重试；
- 403 且响应含 `Invalid API key` 时是"Key 无效"，403 且是 `needs Pro` 才是
  `plan_required`——两者对号池的动作不同（前者报废，后者保留但标记）。

---

## 5. 网页反代出 Key（`/p/blackbox/v1/*`）

worker 把 `sk-…` 拿回来之后，网关用**自己的** Key 对外提供服务，下游工具拿到的是
`sk-dcg-…`，永远看不到上游 Key。

```powershell
# 1) 号池里有什么
curl -H "Authorization: Bearer $TOKEN" http://127.0.0.1:8787/api/admin/providers/blackbox/pool

# 2) 模型列表（上游没有 /models，走描述符白名单）
curl -H "Authorization: Bearer sk-dcg-…" http://127.0.0.1:8787/p/blackbox/v1/models

# 3) 对话（OpenAI 形状，tools / stream 原样透传）
curl -X POST http://127.0.0.1:8787/p/blackbox/v1/chat/completions `
  -H "Authorization: Bearer sk-dcg-…" -H "Content-Type: application/json" `
  -d '{"model":"blackboxai/blackbox-pro","messages":[{"role":"user","content":"hi"}],"stream":true}'

# 4) Anthropic 形状（Claude Code 这类工具）
curl -X POST http://127.0.0.1:8787/p/blackbox/v1/messages `
  -H "Authorization: Bearer sk-dcg-…" -H "Content-Type: application/json" `
  -d '{"model":"blackboxai/blackbox-pro","max_tokens":256,"messages":[{"role":"user","content":"hi"}]}'
```

反代层行为：

- 401/403/429/5xx 时换号重试（最多 3 个账号），成功即清冷却；
- 上游 4xx 原样回传状态码与 `error` 体，不吞消息；
- SSE 逐块转发，`X-Accel-Buffering: no`；
- 响应头带 `X-Gateway-Provider: blackbox` 与 `X-Gateway-Account: <id>`，排障时一眼看出用了哪个号。

---

## 6. 与其它模块的边界

| 维度 | blackbox | 其它 |
| --- | --- | --- |
| 号池文件 | `data/blackbox-pool.json` | `allmodels-pool.json` / `moonai-pool.json` / … |
| 注册锁 | `data/blackbox-register.lock` | 每个 provider 一把 |
| 路由 | `/p/blackbox/*`、`/api/admin/providers/blackbox/*` | DeepAI `/v1/*`、allmodels `/api/admin/allmodels/*`、poe `/api/*` |
| 环境变量 | `BLACKBOX_*` | `ALLMODELS_*` / `MOONAI_*` / `DEEPAI_*` |
| worker 目录 | `tools/blackbox-reg` | `tools/<provider>-reg` |
| 失败分类 | 见 `errors.py` 的 `KIND_LABELS` | 同名 kind 语义一致，便于统一退避 |

`createProviderRuntime` 只装 `DCG_PROVIDERS` 里列出的 provider；不设就是全装。
blackbox 缺 worker 目录时不影响其它 provider 启动，只会在
`/api/admin/providers` 里显示 `toolPresent: false`。

---

## 7. 自检与测试

```powershell
cd tools\blackbox-reg
python -m pip install -r requirements.txt
python -m blackbox_reg doctor --mail-provider domain --mail-domains own.example.com
python -m unittest discover -s tests -v          # 12 项离线协议用例

cd ..\..
node --test tests/blackbox-provider.test.mjs      # 6 项 provider/号池结构用例
node tests/blackbox-e2e.mjs                       # 8 项全链路（mock 站点 → worker → 号池 → 反代）
```
