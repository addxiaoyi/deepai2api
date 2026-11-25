# allmodels-reg — Allmodels 面板批量注册机

`dash.allmodels.io` 的注册/建 Key 协议实现 + 并发批量注册 + 号池导出。
被网关的「Allmodels 号池」当作子进程调用，也可以单独当 CLI 用。

## 面板协议（从官方 SPA 还原 + 线上校验）

面板是 Vite/React + **Better Auth**，注册链路四步：

| 步骤 | 请求 | 说明 |
| --- | --- | --- |
| 1 | `POST /api/auth/sign-up/email` `{name,email,password,callbackURL}` + 头 `x-turnstile-token` | 成功即下发 session cookie，并发验证邮件 |
| 2 | `GET /api/auth/verify-email?token=…`（邮件里的链接） | 302 进 SPA；token 过期会带 `?error=` |
| 3 | `GET /api/me/overview` | 组织 / 项目 / guardrail，异步 provision，需要稍等 |
| 4 | `GET /api/me/signup-key` → `{"key":"sk_live_…"}` | 注册时自动下发的 Key |

需要更多 Key 时：`POST /api/keys` `{displayName,projectId,guardrailId,environment}`
→ `{"key":"sk_live_…"}`；撤销 `POST /api/keys/{id}/revoke`。

要点：

- Turnstile 由服务端强校验（无 token 直接 `403 {"code":"captcha_failed"}`）。站点 key
  `0x4AAAAAAFoFe99_cOH0RnFT`，action `signup`，token 走 `x-turnstile-token`。
- `gmail.com` / `googlemail.com` 被前端拒绝，必须用别的域名。
- 账号密码、会话 cookie、API Key 都要按账号保存，后续补 Key、看用量都要登录态。

## 双引擎：协议 + 指纹浏览器

同一套流程两条腿，产出记录形状完全一致（`store` / 导出 / 号池不区分来源）：

| `--engine` | 行为 |
| --- | --- |
| `protocol` | 纯 HTTP（httpx）打完四步，最快最省；Turnstile 用解题器 token |
| `browser` | 真实浏览器打开 `/signup`，人类化填表 + 点按钮，Turnstile 由浏览器自己过；拿 key 用页内 `fetch`（同源 cookie） |
| `auto`（默认） | 先协议；一旦 `captcha_required` / `blocked` 就当场切浏览器重跑，事件里记 `engine=browser` |

指纹内核按质量自动选，也可以 `--browser-stack` 指定：

1. **Camoufox**（Firefox）— C++ 层指纹改写，canvas/WebGL/fonts/screen/时区自洽，
   `humanize` 内置人类化输入；Turnstile 托管挑战通常自己过。
   <https://github.com/daijro/camoufox>
2. **Patchright**（Chromium）— Playwright 的源码级反检测分支（去 Runtime 域、
   隔离执行上下文、`navigator.webdriver` 等），保留真实 TLS/HTTP2 指纹。
   <https://github.com/Kaliiiiiiiiii-Vinyzu/patchright>
3. **Playwright Chromium** — 兜底：随机视口/UA/DPR + 最小 `webdriver`/`plugins`/`chrome` 修补；
   指纹生成可参考 **browserforge** <https://github.com/daijro/browserforge>，
   另有 **nodriver**（CDP、无需 chromedriver）<https://github.com/ultrafunkamsterdam/nodriver>
   与 SeleniumBase UC 模式可作为同类替代。

```bash
pip install "camoufox[geoip]"          # 推荐：指纹质量最高
pip install patchright                 # 可选：Chromium 系
pip install playwright && playwright install chromium   # 兜底

# 协议优先，被拦自动切 Camoufox
python -m allmodels_reg run -n 5 -c 2 --engine auto --captcha capsolver --captcha-key $KEY

# 全程浏览器，能看到窗口
python -m allmodels_reg run -n 2 --engine browser --headful

# 接管你自己的指纹浏览器（AdsPower / BitBrowser / Morelogin 的本地 CDP）
python -m allmodels_reg run -n 5 --engine browser --cdp http://127.0.0.1:50325
```

浏览器路径的细节：按 `autocomplete`/`placeholder` 定位输入框（shadcn 表单 id 不稳），
逐字符输入 + 随机停顿；等待 `cf-turnstile-response` 出现 token，托管模式超时则点一次
复选框；提交后等 `/verify-email`，出错就读 `role=alert` 文案并映射成
`email_taken` / `email_rejected` / `captcha_required`；成功后在同一页面上下文里
`fetch('/api/me/overview' | '/api/me/signup-key' | '/api/keys')` 领 key，
所以 cookie 和指纹天然一致。失败会落 `out/screenshots/`。

## 安装

```bash
cd tools/allmodels-reg
pip install -r requirements.txt
# 指纹浏览器内核（至少一个）
pip install "camoufox[geoip]"
# 可选：Chromium 系
pip install patchright
# 或兜底
pip install playwright && playwright install chromium
```

## 用法

```bash
# 1) 先体检：面板可达性、邮箱、解题器
python -m allmodels_reg doctor --captcha capsolver --captcha-key $KEY

# 2) 注册 5 个，并发 3，自动领 key，完后导出
python -m allmodels_reg run -n 5 -c 3 \
    --mail-provider mailtm \
    --captcha capsolver --captcha-key $CAPSOLVER_KEY \
    --keys-per-account 1

# 3) 指定自己的 catch-all 邮箱（最稳）
python -m allmodels_reg run -n 20 -c 4 --mail-provider imap \
    --mail-domain example.com

# 4) 导出（jsonl / csv / pool）
python -m allmodels_reg export --format pool --file out/pool.json

# 5) 回推网关号池（会自动补发 sk-dcg- Key）
python -m allmodels_reg push --push-url http://127.0.0.1:8787 --push-token $TOKEN
```

网关侧一条命令等价（并发注册 + 自动建 key + 写号池 + 导出）：

```bash
node src/allmodels-cli.mjs register -n 20 -c 4 \
  --mail-provider mailtm --captcha capsolver --captcha-key $KEY --export jsonl
```

## 人机校验三种拿法

| `--captcha` | 说明 |
| --- | --- |
| `capsolver` | `AntiTurnstileTask(Proxyless)`，带 `pageAction=signup`；配了代理就用同出口的 `AntiTurnstileTask` |
| `2captcha` | `method=turnstile` 轮询 |
| `browser` | Playwright 在真实页面上渲染 widget，读 callback token（需要干净住宅 IP） |
| `manual` | `--captcha-key` 直接当 token 用（调试/自备 token） |
| `none` | 不发 token；上游关闭校验时才可用，否则按 `captcha_required` 重试 |

## 配置

`config.example.yaml` 是完整字段说明，也可用环境变量：

```
ALLMODELS_BASE_URL=https://dash.allmodels.io
ALLMODELS_ENGINE=auto              # protocol | browser | auto
ALLMODELS_BROWSER_STACK=auto       # auto | camoufox | patchright | playwright
ALLMODELS_CDP=http://127.0.0.1:50325
ALLMODELS_CAPTCHA_PROVIDER=capsolver
ALLMODELS_CAPTCHA_KEY=...
ALLMODELS_IMAP_HOST / _USER / _PASSWORD
ALLMODELS_GATEWAY_URL / ALLMODELS_GATEWAY_TOKEN
ALLMODELS_PYTHON=python            # 网关找 python 用
ALLMODELS_REGISTER_DIR=...         # 网关找本目录用
```

## 目录

```
allmodels_reg/
  protocol.py   面板协议（注册/验证/overview/建 key/撤销）
  browser.py    指纹浏览器引擎（camoufox / patchright / playwright / 外部 CDP）
  captcha.py    Turnstile：capsolver / 2captcha / browser / manual
  mailbox.py    tempmail_lol / mailtm / imap / fixed
  runner.py     引擎路由（auto 回落）+ 并发 + 重试 + NDJSON 事件
  store.py      sqlite 落库 + jsonl/csv/pool 导出
  push.py       POST /api/admin/allmodels/pool/import
  cli.py        run | doctor | probe | list | export | push
tests/          离线协议测试 / 引擎回落测试（不联网、不开浏览器）
```

## 事件流（`--json`）

```
{"type":"start", ...}      一次 run 的配置
{"type":"attempt", ...}    每个邮箱一次尝试
{"type":"account", ...}    成功，带 api_key / key_id / project / guardrail / cookies
{"type":"pushed", ...}     网关已入池
{"type":"push_error",...}  回推失败
{"type":"failed", ...}     该单元最终失败（含 kind）
{"type":"done","summary":{...}}
```

## 测试

```bash
python -m pytest tests -q          # 离线，不联网、不消耗解题额度
```
