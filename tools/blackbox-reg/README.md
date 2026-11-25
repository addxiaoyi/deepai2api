# blackbox-reg —— Blackbox AI 批量注册机

把 `app.blackbox.ai` 的账号（Google 身份）批量落地成面板 `sk-…` Key，并回推本仓库
的网关号池 `data/blackbox-pool.json`，再由 `/p/blackbox/v1/*` 对外出 Key。

Blackbox 的账号体系只有 Google 登录，`POST /api/auth/sign-up/email` 恒
`400 "Bad request."`，所以这里"注册"= **一次 Google OAuth 授权**，
产物是 app 侧会话 + 面板 Key。取证表见 [`docs/BLACKBOX-POOL.md`](../../docs/BLACKBOX-POOL.md)。

---

## 安装

```powershell
cd tools\blackbox-reg
python -m pip install -r requirements.txt
# 指纹浏览器（可选，browser 引擎才需要）：任选其一
python -m pip install camoufox[geoip]     # 推荐：Firefox + C++ 层指纹改写
python -m pip install patchright && patchright install chromium
python -m playwright install chromium     # 兜底
```

## 自检

```powershell
python -m blackbox_reg doctor --mail-provider domain --mail-domains own.example.com
```

输出里重点看：

| 项 | 期望 |
| --- | --- |
| `site.providers` | `HTTP 200` 且含 `google` |
| `site.csrf` | `HTTP 200`（Auth.js 还活着） |
| `site.inference` | `HTTP 401/403`（端点活着，只是没 Key） |
| `google_pool` | 有身份，或明确显示未配置 |
| `mailbox` | 能造出 `xxx@own.example.com` |
| `browser_stacks` | 至少一个内核 |

---

## 跑一轮

```powershell
# 最快的一条：复用已登录的 app 会话（浏览器里 F12 导出 cookies 为 JSON）
python -m blackbox_reg run -n 5 -c 2 --engine protocol `
  --session-file cookies.json --keys-per-account 1 --json

# 真跑 Google 登录（需要身份池 + 浏览器内核）
python -m blackbox_reg run -n 3 -c 1 --engine browser `
  --google-pool google.jsonl --mail-provider domain --mail-domains own.example.com `
  --browser-stack camoufox --headful

# 直接推到网关（网关在 8787，管理口令是 <TOKEN>）
python -m blackbox_reg run -n 5 -c 2 --engine auto --session-file cookies.json `
  --push-url http://127.0.0.1:8787 --push-token <TOKEN> --json
```

`--json` 时 stdout 只有 NDJSON 事件（网关就是这么读的），日志走事件里的 `type=log`。

### 身份池格式（`--google-pool`）

`google.jsonl`，一行一个身份：

```json
{"email":"ada@own.example.com","password":"…","totp_secret":"JBSWY3DPEHPK3PXP","note":"batch-1"}
{"email":"grace@own.example.com","cookies":[{"name":"SID","value":"…","domain":".google.com"}]}
```

- 有 `cookies` 时优先用 cookies（等价于"这个 Google 已经登录过"）；
- 有 `totp_secret` 时浏览器腿会自动算 6 位码；
- 一个身份只对应一个 Blackbox 账号，重复跑会命中 `email_taken` 跳过。

### 关键参数

| 参数 | 说明 |
| --- | --- |
| `--engine` | `auto`（协议→被拦切浏览器）/ `protocol` / `browser` / `hybrid` / `console` |
| `--session-file` | app 会话 cookies（协议腿最快路径） |
| `--google-cookies` | 已登录 Google 的 cookies（协议腿能自己走完 OAuth） |
| `--authorization-code` | 手工抓的一次性授权码（配合 `authorization_code_state`，见代码） |
| `--keys-path` / `--keys-create-path` / `--keys-create-body` | 覆盖面板 Key 接口探测结果 |
| `--anti-ban` | 防封策略 JSON（与网关 `DEFAULT_ANTI_BAN` 同名字段） |
| `--mail-provider` | `domain`（自有域轮换）/ `imap` / `mailtm` / `fixed` |
| `--out-dir` | sqlite + jsonl + key-endpoints.json + screenshots 的落点 |

IMAP（域名邮箱）环境变量：

```
BLACKBOX_IMAP_HOST  BLACKBOX_IMAP_PORT(993)  BLACKBOX_IMAP_USER  BLACKBOX_IMAP_PASS
BLACKBOX_IMAP_SSL(1)  BLACKBOX_IMAP_FOLDER(INBOX)
```

---

## 防封策略

四层护栏，与网关 `src/provider-register.mjs` 的 `DEFAULT_ANTI_BAN` 字段一一对应：

1. **身份层**：一个 Google 身份绑定一个出口 IP（`proxyStickyPerIdentity`）；
2. **域名层**：单域 RPM/RPH 滑动窗口 + 健康分（成功 +1、失败 -2），低于阈值停用；
3. **节奏层**：注册抖动、授权页停留、批次冷却；
4. **接口层**：单账号建 Key 频控、建完自检、会话复用（`reuseSession`）。

失败分类（决定退避与换腿）：`rate_limited` / `blocked` / `captcha_required` /
`oauth_denied` / `session_expired` / `plan_required` / `no_api_key` /
`key_not_obtainable` / `transport` / `upstream`。

`auto` 模式下 `session_expired`、`oauth_denied`、`no_api_key`、`key_not_obtainable`、
`blocked`、`captcha_required` 会当场切 `browser` 重跑（同身份、同出口）。

---

## 其它子命令

```powershell
python -m blackbox_reg push --push-url http://127.0.0.1:8787 --push-token <TOKEN>
python -m blackbox_reg export --format csv
python -m blackbox_reg pool
```

## CLI 运行环境模拟

```powershell
python -m blackbox_reg cli-env `
  --gateway-url http://127.0.0.1:8787/p/blackbox/v1 `
  --gateway-key sk-dcg-… --run
```

造一个隔离的 `HOME`（`.blackboxcli/settings.json` 指向本网关）并跑一次
`blackbox -p …`，用来验证"编程工具链"端到端可用。逆向细节见
[`docs/BLACKBOX-CLI-RE.md`](../../docs/BLACKBOX-CLI-RE.md)。

## 测试

```powershell
python -m unittest discover -s tests -v
```

12 项离线用例，覆盖 Auth.js 的 csrf/signin/回调、Key 候选探测与缓存、建 Key 兜底、
推理 403 → `plan_required`、429/401 映射、TOTP RFC 向量、CLI 配置形状。
