# MoonAI 注册机（`moonai_reg`）

针对 **mooonai.com（MoonAI）** 的批量注册 worker：**协议腿 + 指纹浏览器腿**双引擎，
域名邮箱收验证信，产出直接进网关的 MoonAI 号池（`data/moonai-pool.json`），
自动领一把 `sk-dcg-…` 网关 Key。

它只做三件事：

1. 按真实表单把账号注册出来并过邮箱验证；
2. 走一遍 OAuth 拿到 `access_token` + `refresh_token`（这才是能调模型的凭据）；
3. 建组织、签 `api_…` Key 留档，把整条记录回推网关。

---

## 1. 安装

```powershell
cd tools\moonai-reg
python -m pip install -r requirements.txt

# 只跑协议腿：到这就够了
python -m moonai_reg doctor

# 要浏览器腿，再装一个内核（三选一）
python -m pip install "camoufox[geoip]"    # 推荐：托管挑战通过率最高
# python -m pip install patchright && python -m patchright install chromium
# python -m pip install playwright && python -m playwright install chromium
```

`doctor` 会报：httpx、可用浏览器内核、两个站点可达性、IMAP 配置与登录。

---

## 2. 邮箱：域名邮箱是推荐路线

| `--mail-provider` | 说明 |
| --- | --- |
| `imap` / `domain` | **推荐**。自有域名 catch-all + IMAP 收信，域名可控、可轮换；`domain` 用 `--mail-domains` 轮换 |
| `mailtm` | `api.mail.tm` 现开临时信箱。**投递不稳定、常被站点按域名拒**，只当兜底 |
| `fixed` | 用 `--email` 指定地址，仍需 IMAP 才能收到验证信 |

IMAP 环境变量（`imap` / `domain` / `fixed`）：

```powershell
$env:MOONAI_IMAP_HOST  = "imap.example.com"
$env:MOONAI_IMAP_PORT  = "993"
$env:MOONAI_IMAP_USER  = "catchall@example.com"
$env:MOONAI_IMAP_PASS  = "…"
$env:MOONAI_IMAP_SSL   = "1"          # 0 = 明文
$env:MOONAI_IMAP_FOLDER= "INBOX"
```

地址由 `名.姓+4位数字@域` 生成（`identity.local_part_for`），冲突概率极低。

---

## 3. 用法

```powershell
# 一轮 5 个，2 并发，先协议腿，被拦自动切浏览器
python -m moonai_reg run -n 5 -c 2 --engine auto `
  --mail-provider imap --mail-domains example.com `
  --keys-per-account 1 --proxy http://user:pass@1.2.3.4:8080

# 只跑浏览器腿（更慢更稳），并放慢 1.5 倍
python -m moonai_reg run -n 2 -c 1 --engine browser --browser-stack camoufox --humanize 1.5

# 接管已有的商业指纹浏览器 profile
python -m moonai_reg run -n 1 --engine browser --cdp http://127.0.0.1:50325

# 自定义防封参数（整块 JSON，字段见 docs/MOONAI-POOL.md §7）
python -m moonai_reg run -n 10 -c 3 --anti-ban '{"domain_rpm":2,"jitter_ms":[2500,6000]}'
```

回推网关（`--push-url` 也能直接在 `run` 里给）：

```powershell
python -m moonai_reg push --push-url http://127.0.0.1:8787 --push-token <管理口令>
```

其它：

```powershell
python -m moonai_reg pool                 # 本地 sqlite 统计（含失败分类）
python -m moonai_reg export --format csv  # 导出 out/accounts.csv
python -m moonai_reg doctor               # 依赖 / 站点 / IMAP 自检
```

产物：`out/accounts.sqlite3`（可查询、断点续跑）、`out/accounts.jsonl`（一行一个账号）。

---

## 4. 事件流（网关读的就是这个）

`--json` 时 stdout 每行一个 JSON 对象：

```json
{"type":"start","count":5,"concurrency":2,"mail_provider":"imap","engine":"auto","captcha":"none","anti_ban":{…}}
{"type":"attempt","email":"a.b1234@example.com","domain":"example.com","provider":"moonai","engine":"auto","ip":"direct"}
{"type":"log","level":"info","message":"等验证邮件…"}
{"type":"backoff","kind":"rate_limited","seconds":27.4,"email":"…"}
{"type":"domain","domains":[{"domain":"example.com","ok":4,"bad":0,"health":1.0}]}
{"type":"account","account":{…},"domain":"example.com","engine":"protocol"}
{"type":"pushed","email":"…"}
{"type":"failed","index":0,"email":"…","kind":"email_taken","reason":"…"}
{"type":"done","summary":{"created":4,"failed":1,"seconds":73.2}}
```

失败 `kind`：`email_taken` / `email_rejected` / `verification_pending` / `rate_limited` /
`blocked` / `captcha_required` / `no_token` / `no_api_key` / `login_rejected` /
`transport` / `upstream`。网关按 `kind` 统计并查退避表。

---

## 5. 协议要点

完整取证表在 [`docs/MOONAI-POOL.md`](../../docs/MOONAI-POOL.md#6-注册协议线上实测2026-11)。
最容易踩的三个：

1. **OAuth 的 `redirect_uri` 只能是 `https://dashboard.mooonai.com/oauth`** ——
   `core/api/v1/auth/url?redirect_url=…` 传别的值会被 authentik
   以 `Mismatching redirect URI` 拒掉。所以代码里**不传** `redirect_url`。
2. **`api_…` 不能调模型**。`llm-proxy` 只认 `access_token`（`Bearer`）。
3. **上游请求体是 `extra_forbidden`**：`stream` / `tools` / `stop` / `n` … 多一个就 422。
   这层剥除在网关侧（`src/moonai-adapter.mjs`），worker 只做注册。

---

## 6. 目录

```
moonai_reg/
  cli.py       命令入口（run / doctor / push / export / pool）
  config.py    站点常量 + RunConfig + AntiBanConfig
  identity.py  persona / UA / 时区 / 视口 / 密码生成
  mailbox.py   mail.tm、IMAP catch-all、验证链接提取（含 mjt.lu 跟随）
  net.py       httpx 会话（cookie jar、代理、重试、错误分类）
  antiban.py   域名速率窗 / 健康分 / 出口串行 / 抖动 / 退避
  protocol.py  注册→验证→登录→OAuth→令牌→组织→Key→自检
  browser.py   同一套流程的指纹浏览器实现
  store.py     sqlite + jsonl
  push.py      回推网关
  runner.py    并发调度 + 事件流 + auto 引擎回落
tests/
  test_protocol_offline.py   本地 mock 跑完整协议（6 项）
```

## 7. 注意

- `out/` 里有明文密码与 Key，已在 `.gitignore` 里排除，别提交。
- 同一个域名别把 `domain_rpm` 调高：站点是按域名看风险的，不是按 IP。
- `--engine auto` 只在被拦时切浏览器；如果一开始就知道要过严格风控，直接 `--engine browser`。
