# cleverai-reg — Clever AI Hub 注册机（协议 + 指纹浏览器双引擎）

`cleverai.app/app/chat` 的注册/建号协议实现 + 并发批量注册 + 号池导出。
被网关的「Clever AI 号池」当子进程调用，也可以单独当 CLI 用。

## 面板协议（从官方 SPA 还原 + 线上校验）

两个后端：Firebase（注册）和 Azure Functions（业务）。注册链路四步：

| 步骤 | 请求 | 说明 |
| --- | --- | --- |
| 1 | `POST identitytoolkit/v1/accounts:signUp?key=<web apiKey>` `{email,password,returnSecureToken}` | 建 Firebase 用户，拿 `idToken`/`refreshToken`/`localId` |
| 2 | `POST api.cleverai.app/appserver/api/v1/users`，multipart `json={uid,appId,platform,isAnonymousUser}` | **201** 返回 profile + wallet（30 每日 + 45 欢迎额度） |
| 3 | `POST identitytoolkit/v1/accounts:sendOobCode` `{requestType:"VERIFY_EMAIL",idToken}` | 发验证信；链接里的 `oobCode` 用 `accounts:update` 消费 |
| 4 | `POST …/omni/response` | SSE 聊天，`engine` 用 `/configs?type=engines` 里的 id（94 个） |

要点（全部线上验证过）：

- **App Check 是硬门**：没有 `X-Firebase-AppCheck` 时 `/users`、`/omni/response` 一律
  `401 APP_CHECK_REQUIRED`。令牌由 reCAPTCHA Enterprise(site key `6LcitAst…`) 换得，
  走的端点：
  `POST firebaseappcheck.googleapis.com/v1/projects/genius-ai-app-test-1/apps/<app>:exchangeRecaptchaEnterpriseToken?key=<web apiKey>`
  → `{"token": "...", "ttl": "3600s"}`。**一枚令牌绑定的是应用安装，不是账号**，所以
  整批账号共用；`AppCheckVault` 负责提前 60 s 续期。
- **`x-functions-key`**：Azure Functions host key，前端 bundle 里就有
  `ctJClL5ezIYh…`，不是用户级密钥。
- **版本头**：`Version` / `X-Version: 12.2.0`。缺失时后端不会报错，而是回一条
  "please update the latest version" 的假回答——排查时最容易被骗的一步。
- **注册后立刻能用**：账号建完 profile 就有 75 credit，可以直接对话；不需要邮箱验证
  也能聊（验证只影响账号的可信度与找回）。

## 安装

```bash
cd tools/cleverai-reg
pip install -r requirements.txt
# 取 App Check 令牌 / 跑浏览器引擎需要：
pip install "camoufox[geoip]"
```

## 用法

```bash
# 1) 体检：后端可达性、App Check 交换端点、浏览器内核、域名战绩
python -m cleverai_reg doctor

# 2) 取一枚 App Check 令牌（3600s），写进 config 或环境变量复用
python -m cleverai_reg appcheck

# 3) 用域名邮箱注册 10 个（最稳的一条路）
python -m cleverai_reg run -n 10 -c 1 \
    --mail-provider imap --mail-domain example.com

# 4) 协议被挡时切指纹浏览器引擎
python -m cleverai_reg run -n 5 -c 1 --engine browser --browser-stack camoufox

# 5) 导出 + 回推网关
python -m cleverai_reg export --format pool --out out/pool.json
python -m cleverai_reg push --push-url http://127.0.0.1:8787 --push-token $TOKEN

# 6) 试聊一个账号，确认还能用
python -m cleverai_reg chat --email ada@example.com
```

网关侧一条命令等价（并发注册 + 自动发 Key + 写号池 + 导出）：

```bash
node src/cleverai-cli.mjs register -n 10 -c 1 --mail-provider imap --mail-domain example.com --export jsonl
```

## 两套引擎

| `--engine` | 做什么 | 什么时候用 |
| --- | --- | --- |
| `protocol` | Spark 注册 + appserver 建号，纯 HTTP；只借浏览器的一枚 App Check 令牌 | 默认，最省资源 |
| `browser` | Camoufox 打开真实站点，在页面上下文里完成注册（reCAPTCHA / App Check 都是原生行为） | 协议被 reCAPTCHA 降分、或要更真实的创建时序 |
| `auto` | 先 `protocol`，被 `blocked` / `captcha_required` / `app_check_required` 挡下就换 `browser` | 无人值守批跑 |

## App Check 三种拿法

| `--captcha` | 说明 |
| --- | --- |
| `browser` | Camoufox 打开 `cleverai.app/app/chat`，读 SPA 自己换的那枚 JWT（推荐） |
| `capsolver` | `ReCaptchaV3EnterpriseTask`（`pageAction=firebase_app_check`），拿到 reCAPTCHA 令牌后自己调 exchange |
| `manual` | `--appcheck-token` 直接给一枚现成 JWT（调试/复用别处取的令牌） |

## 域名邮箱（`--mail-provider`）

| 值 | 说明 |
| --- | --- |
| `imap` | 自己的 catch-all 域，`任意前缀@yourdomain` 都进同一个收件箱（**推荐**） |
| `fixed` | 一个自己拥有的地址，按 `+tag` 分账号 |
| `mailtm` | 公共 API，盒子活 ~7 天 |
| `tempmail_lol` | 公共 API，每次随机域 |

公共临时邮箱域名被 Firebase 风控的时间通常以小时计，批跑一定用 `imap`。

## 防封策略

`config.example.yaml` 的 `antiban` 段：

| 参数 | 作用 |
| --- | --- |
| `per_proxy_concurrency: 1` | 同一出口同时只跑一个注册 |
| `min_delay` / `max_delay` | 账号之间的随机间隔（默认 25–70 s） |
| `per_domain_burst: 5` | 同一邮箱域名连续成功几个就换域 |
| `domain_cooldown: 900` | 域名被打回后的冷却 |
| `max_failures_per_proxy` / `proxy_cooldown` | 出口连续失败几次就拉黑多久 |
| `warmup: true` | 注册后先聊一句再入池 |

配合 `geoip: true` 的指纹浏览器（时区/语言跟随出口 IP），是这套里最有效的两层。

## 配置

`config.example.yaml` 是完整字段说明，也可用环境变量：

```
CLEVERAI_BASE_URL=https://cleverai.app
CLEVERAI_APPCHECK_PROVIDER=browser
CLEVERAI_APPCHECK_TOKEN=...        # manual 模式
CLEVERAI_CAPTCHA_KEY=...           # capsolver
CLEVERAI_IMAP_HOST / _USER / _PASSWORD
CLEVERAI_MAIL_DOMAIN=example.com
CLEVERAI_GATEWAY_URL / CLEVERAI_GATEWAY_TOKEN
CLEVERAI_PYTHON=python             # 网关找 python 用
CLEVERAI_REGISTER_DIR=...          # 网关找本目录用
```

## 目录

```
cleverai_reg/
  appcheck.py   App Check：浏览器/ capsolver / manual + 令牌缓存与续期
  protocol.py   Firebase 注册 + appserver 建号 + omni SSE 聊天
  browser.py    Camoufox / patchright / playwright 指纹引擎
  mailbox.py    imap(catch-all) / fixed / mailtm / tempmail_lol
  runner.py     并发 + 防封调度 + 预热 + NDJSON 事件
  store.py      sqlite 落库（账号 / 尝试 / 失败 / 域名战绩）
  push.py       POST /api/admin/cleverai/pool/import
  cli.py        run | doctor | probe | list | domains | export | push | chat | appcheck
tests/          离线协议测试（httpx.MockTransport，不联网）
```

## 事件流（`--json`）

```
{"type":"start", ...}      一次 run 的配置
{"type":"attempt", ...}    每个邮箱一次尝试
{"type":"account", ...}    成功，带 uid / refreshToken / wallet / engine
{"type":"pushed", ...}     网关已入池
{"type":"push_error",...}  回推失败
{"type":"failed", ...}     该单元最终失败（含 kind）
{"type":"done","summary":{...}}
```

## 测试

```bash
python -m pytest tests -q       # 离线，不联网、不消耗额度
```
