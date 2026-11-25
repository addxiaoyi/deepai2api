# DeepAI 注册机（protocol + Camoufox 双引擎）

批量注册 deepai.org 账号，拿到账号自带的 **DeepAI API Key**，直接喂给本仓库的网关
（`src/index.mjs` 的 `accounts[]` 账号池）。

两条引擎：

| 引擎 | 做什么 | 速度 | 什么时候用 |
| --- | --- | --- | --- |
| `protocol` | 直接按站点自己的注册协议打 `api.deepai.org`，httpx 会话 + cookie jar | ~5 s/号 | 默认。没有验证码、没有浏览器开销 |
| `browser` | Camoufox（开源反指纹 Firefox）+ 真实弹窗操作，按代理出口 IP 对齐时区/语言/WebGL/字体，Turnstile 自动等或外部打码 | ~15–25 s/号 | protocol 被 Cloudflare/风控拦、或站点加了验证码 |
| `auto` | 先 protocol，遇 `Blocked` 自动升到 Camoufox，同一条记录继续 | — | 推荐默认 |

---

## 1. 协议（从 deepai.org 首页内联 JS 里抠出来的）

注册弹窗把表单发到 `api.deepai.org`，字段名与顺序如下：

```
GET  https://deepai.org/                                   # 拿 csrftoken / sessionid
POST https://api.deepai.org/get_user_login_type            # form: email
     -> {"user_exists": false, "login_with_password": true, "login_with_social_accounts": []}
POST https://api.deepai.org/save_custom_signup_data        # json: referrer/signup_page/desired_username/language（可选，失败不影响）
POST https://api.deepai.org/daily-time-sync/registration/  # form: email, username, password1, password2
     -> 201 {"key": "<40 hex>"} + Set-Cookie: sessionid / csrftoken
GET  https://api.deepai.org/daily-time-sync/user/          # 已登录态
     -> {"pk": ..., "username": ..., "userprofile": {"api_key": "<uuid>"},
         "userprofile2": {"plan": "free", "locked_out_due_to_no_payment_info": true, ...}}
```

关键点：**注册完 `userprofile.api_key` 就已经在用户对象里**，不需要邮箱验证、不需要
桌面端 OAuth。所以最快路径是「注册 → 读 profile → 存 key」三步。

`userprofile.api_key` 缺失的账号会自动走桌面端 PKCE 兑换兜底：

```
GET  https://deepai.org/deepai_code/desktop_auth/?state=..&port=..&code_challenge=..&code_challenge_method=S256
     -> 302 Location: http://127.0.0.1:<port>/auth-callback?code=...
POST https://deepai.org/deepai_code/desktop_auth/exchange/v2/   # {code, code_verifier}
     404 时回落到 /exchange/  （与桌面端行为一致）
     -> {"api_key": ..., "username": ..., "is_pro": ...}
```

这跟本仓库 `src/oauth.mjs` 里那套完全对得上，只是这里用会话 cookie 自动完成，不用人点。

### 站点会挡什么

| 返回 | 含义 | 处理 |
| --- | --- | --- |
| `400 {"email": ["Please use your own email address."]}` | 该域名在一次性邮箱黑名单里（`mail.tm`、`euploading.com` 这种） | 换 mb 域/换 provider 重试，`EmailRejected` |
| `{"username": [...]}` / `{"password1": [...]}` | 用户名/密码策略不通过 | 换 persona，`PasswordRejected` |
| `429` | 出口 IP 频率限制 | 指数退避 + 换代理 |
| `403` + `Just a moment...` | Cloudflare 挑战 | 升 Camoufox |

实测（2026-11）：`tempmail.lol` 的随机域名（`*.gloatinggoat.com` / `*.sablereels.com`）
目前可通过；`mail.tm` 的 `euploading.com` 被拒。

---

## 2. 安装

```powershell
cd tools\deepai-reg
python -m pip install -r requirements.txt
python -m camoufox fetch          # 只下 Camoufox 内核（~200 MB），协议引擎不需要
```

`python -m camoufox fetch` 若被 GitHub API 限流（`403 rate limit exceeded`），直接下 release 包手动装 ——
Camoufox 0.5.x 用的是**多版本布局**，别解到根目录（根目录只有 `version.json` 才会被当"旧版平铺安装"，
而且缺 `.0.5_FLAG` 时 `remove_incompatible_data()` 会把整个目录删掉）：

```powershell
# 1) 拿直链（不要走 api.github.com，走 expanded_assets）
#    https://github.com/daijro/camoufox/releases/expanded_assets/v158.0.1-beta.41
curl.exe -L -o camoufox.zip `
  https://github.com/daijro/camoufox/releases/download/v158.0.1-beta.41/camoufox-158.0.1-beta.41-win.x86_64.zip
```

```python
# 2) 解到 browsers/<repo>/<version>-<build>/ 再写三份元数据
import json, zipfile
from pathlib import Path
from camoufox.pkgman import INSTALL_DIR
from camoufox.multiversion import BROWSERS_DIR, CONFIG_FILE, COMPAT_FLAG

target = BROWSERS_DIR / "official" / "158.0.1-beta.41"
target.mkdir(parents=True, exist_ok=True)
zipfile.ZipFile("camoufox.zip").extractall(target)
(target / "version.json").write_text(json.dumps({"version": "158.0.1", "build": "beta.41"}))
CONFIG_FILE.write_text(json.dumps({"active_version": "browsers/official/158.0.1-beta.41"}))
COMPAT_FLAG.write_text("")
```

```powershell
python -c "from camoufox.pkgman import installed_verstr; print(installed_verstr())"   # 158.0.1-beta.41
```

直连下不动就挂代理（本机 10808 为例）：`curl.exe -L --proxy http://127.0.0.1:10808 ...`，
单连接会被掐断，用 16 MB 分块 + 8~12 并发拉，实测 1.25 GB 包能稳定在 ~5 MB/s 完成。

---

## 3. 跑

```powershell
# 自检：依赖 / 代理出口 IP / 站点连通 / 邮箱可用性
python -m deepai_reg doctor

# 只看 Camoufox 抬头纹：UA / 时区 / webdriver / canvas / 屏幕（截图落地）
python -m deepai_reg browser-check --headful

# 注册 5 个（协议引擎，3 并发）
python -m deepai_reg run -n 5 --engine protocol --concurrency 3

# 风控严的时候：走 Camoufox，可看窗口，失败留截图
python -m deepai_reg run -n 2 --engine browser --headful

# 指定代理池（也支持 socks5://user:pass@host:port）
python -m deepai_reg run -n 20 --proxy http://127.0.0.1:10808,http://10.0.0.8:8080

# 用自己的域名（catch-all + IMAP，最耐用，不在任何一次性域名黑名单里）
python -m deepai_reg run -n 20 --mail-provider imap --mail-domain yourdomain.com
#   配合环境变量 DEEPAI_IMAP_HOST / DEEPAI_IMAP_USER / DEEPAI_IMAP_PASSWORD

# 结果
python -m deepai_reg list --limit 20
python -m deepai_reg export --format jsonl --file out/accounts.jsonl

# 给网关/WebUI 用：NDJSON 事件流 + 每个账号即时入池
python -m deepai_reg run -n 5 --json --push-url http://127.0.0.1:8787 --push-token <口令>
```

每次运行都会落地：

```
out/accounts.sqlite3     # 全量库（含密码、sessionid、profile 原始 JSON、每次尝试记录）
out/accounts.jsonl       # 网关可直接吃的 {apiKey, username, isPro, ...}
out/accounts.csv
out/run.log              # 逐条时间线
out/screenshots/         # browser 引擎失败现场
```

### 本机实测（2026-11-25，出口 US 代理，DeepAI 未加验证码）

| 场景 | 结果 |
| --- | --- |
| `run -n 3 --engine protocol --concurrency 3` | 3/3 成功，14.7 s |
| `run -n 4 --engine protocol --concurrency 4` | 4/4 成功，16.7 s（≈4 s/号） |
| `run -n 1 --engine browser` | 1/1 成功，43.9 s（Camoufox 冷启动占大头） |
| `browser-check` | UA `Firefox/158.0`、`webdriver=false`、时区跟代理 IP 落到 `America/New_York` |
| 网关拉起 `--json --push-url`（2 并发） | 2/2 成功、2 个 Key 自动入池，33.5 s |
| `unittest` | 24/24 |

---

## 4. 接进网关 / WebUI（自动入池）

两条路，产物完全一样（都落到网关的 `accounts[]`）：

**A. WebUI 面板（推荐）** — `Open WebUI → 设置 → Admin → DeepAI → 批量注册 · 自动入池`。
填数量/并发/引擎/邮箱来源（可选域名池、代理池）→ 开始，面板 2.5 s 轮询一次：

```
面板 ──▶ /api/v1/deepai/register ──▶ 网关 /api/admin/register ──spawn──▶ python -m deepai_reg run --json --push-url …
  ▲                                                                              │ 每个账号一条 NDJSON
  └──────────── 任务状态 / 日志 / 域名战绩 ◀──────────────────────────────────────┘
                    │ account 事件
                    ▼
      POST /api/admin/accounts → store.upsertAccount + lane.resetCatalog()  （调度器与模型目录立刻刷新）
```

**B. 命令行直推** — 本机跑，账号一边生成一边入池：

```powershell
python -m deepai_reg run -n 10 --engine protocol --concurrency 3 `
  --push-url http://127.0.0.1:8787 --push-token <网关管理口令>

# 或者事后批量灌
node ../../tools/deepai-reg/scripts/import-to-gateway.mjs out/accounts.jsonl --data ../../data
```

网关新增的三个管理接口（都要管理口令）：

| 接口 | 作用 |
| --- | --- |
| `POST /api/admin/accounts` | 入池一个账号 `{apiKey, username, isPro, label}`，按 apiKey 幂等 |
| `GET /api/admin/register` | 任务快照：状态、计数、**进度/ETA**、当前邮箱、`failureReasons`、最近 240 行日志、域名战绩 |
| `POST /api/admin/register` | 起任务 `{count, engine, concurrency, mailProvider, mailDomains[], proxies[], retryRounds}` |
| `POST /api/admin/register/stop` | 停任务（Windows 上整棵进程树 taskkill） |
| `GET /api/admin/register/log?level=&limit=&download=1` | 按级别取日志尾；`download=1` 直接下 `.log` |
| `GET /api/admin/register/domains?force=1` | 域名战绩（现跑 `deepai_reg domains --json`，缓存 30 s） |

### 面板能看到的

| 能力 | 实现 |
| --- | --- |
| 进度条 + 预计剩余 | 网关按 `settled/requested` 与吞吐算 `progress.ratio`、`etaSeconds`、`ratePerMinute`，面板每 2.5 s 刷新 |
| 失败自动重试 | worker 先跑一轮，失败的单元进入 `--retry-rounds`（默认 1）重跑，**换 persona / 域名 / 代理**；`retry` 事件在日志里单列 |
| 失败原因留档 | `failures` 表记 `kind`（`email_rejected`/`blocked`/`rate_limited`/`no_api_key`/`transport`…）+ 原文，面板聚合展示 |
| 日志按级别筛选/导出 | 面板 chips（全部/info/warn/error/debug）+「导出日志」下载 `deepai-register-<level>.log` |
| 域名可视化 | 每个域一条绿/红双色条（成功/失败），数据来自成绩表，可手动「刷新域名」 |

### 邮箱域名自动学习

`mail_domains` 表在账号库同一个 SQLite 里，worker 每轮都在写：

- 成功 → `ok_count += 1`，下轮优先用（`_domains_for` 先排已知可用的域）；
- 被 DeepAI 拒（`Please use your own email address.`）→ `bad_count += 1`，只有失败记录的域被“停放”，不再白试；
- `mailtm` 每次还会拉 `/domains` 现取活跃域，和人工填的 `--mail-domains` 合并；
- 两个免费邮箱 API 同时开盒都会 429，所以开盒做了全局节流（1.5 s）+ 429 退避重试；
- 随时看战绩：`python -m deepai_reg domains`（`--json` 给面板用）。

### 指纹更像真人（`browser` 引擎）

| 做法 | 为什么 |
| --- | --- |
| `fingerprint_preset=True` | 用内置真机档（屏幕/字体/WebGL/语音成套），不是 fpgen 拼的合成指纹 |
| `screen=1280×720..1920×1080` + `window=1366×768` | 无头模式默认报 1088×612，是个明显破绽 |
| `pin_cpu_cores=True` | `hardwareConcurrency` 跟指纹一致（16 核），不暴露宿主 32 核 |
| `humanize=2.2` | 鼠标轨迹用贝塞尔式缓动，不是瞬移 |
| 自写 `_human_click` / `_human_type` | 先靠近、微调、再落下；逐字符 38–118 ms，偶发 160–420 ms 停顿 |
| `_warm_up` | 进站先晃动鼠标、上下滚一段，像真人在读页面 |
| `geoip=True` + 代理 | 时区/语言/经纬度跟出口 IP 对齐（实测落到 `America/New_York`） |

实测（2026-11-25）：`browser` 引擎连跑 2 个号 **2/2 成功**，`submit via human-click`，单号 ~40 s。

> 新号 `plan=free`、`locked_out_due_to_no_payment_info=true`：
> 池子里只是"能登录的账号"，能不能真跑通 `deepai_code` 付费通道，取决于该账号有没有订阅。
> 注册机只保证身份与 Key 合法，不改变计费状态。

---

## 5. 配置

见 `config.example.yaml`（复制成 `config.yaml` 即生效，命令行优先于文件）。要点：

```yaml
engine: auto
concurrency: 2        # 并发 = 独立 cookie jar + 独立 persona + 独立代理出口
attempts: 3           # 单号失败重试：换邮箱、换代理、必要时换引擎
delay: [4, 9]         # 两次尝试之间随机停顿，别把出口 IP 打死
geoip: true           # Camoufox 按代理 IP 对齐时区/语言/经纬度
proxies: [http://127.0.0.1:10808]
mail:
  provider: tempmail_lol   # tempmail_lol | mailtm | imap | fixed
```

`mail.provider=fixed` 时传 `--email you+tag@gmail.com`，程序自动给每个账号加不同 `+tag`。

---

## 6. 结构

```
deepai_reg/
  protocol.py   # 注册协议 + profile 取 key + PKCE 兜底（httpx，无浏览器）
  browser.py    # Camoufox 引擎：开弹窗、拟人输入、Turnstile 等待/注入、截图
  captcha.py    # capsolver AntiTurnstile 打码（可选）
  mailbox.py    # tempmail_lol / mail.tm / 自有 catch-all(IMAP) / fixed
  mail_pool.py  # 域名战绩表：哪些域注册成功过、哪些被 DeepAI 拒
  push.py       # 把新账号 POST 进网关 /api/admin/accounts
  identity.py   # persona：姓名、本地部分、密码、locale/timezone
  net.py        # 代理感知的 httpx 客户端（强制 trust_env=False）
  store.py      # SQLite + JSONL/CSV 导出
  runner.py     # 并发、重试策略、引擎升级、NDJSON 事件、doctor
  cli.py        # run | doctor | browser-check | mail | list | export
tests/test_offline.py   # 协议层 + 域名学习（MockTransport，不碰网络）
tests/test_config.py
```

离线测试：

```powershell
python -m unittest discover -s tests -v
```

---

## 7. 排错

| 现象 | 原因 / 处理 |
| --- | --- |
| `httpx.InvalidURL: Invalid port: ':1]'` | 环境里 `NO_PROXY` 含 `[::1]`，httpx 解析炸；本工具已强制 `trust_env=False`，自己传 `--proxy` |
| 全是 `EmailRejected` | 当前 provider 的域名进黑名单了，换 `--mail-provider imap` 或换域 |
| 全是 `Blocked` | 出口 IP 被标记；换干净住宅代理，或 `--engine browser --headful` 手动过挑战 |
| `NoApiKey` | 账号建出来了但 profile 没给 key，且 PKCE 兜底也失败；看 `out/run.log` 里的 profile JSON |
| Camoufox 起不来 | `python -m camoufox fetch` 没跑完，`python -m camoufox path` 看内核路径 |
| 想确认账号真能用 | 取 `sessionid` 打 `GET api.deepai.org/daily-time-sync/user/`，或拿 key 打网关 `/v1/models` |
