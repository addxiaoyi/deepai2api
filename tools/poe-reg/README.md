# Poe 注册机（协议 + Camoufox 双引擎）

给 `poe.com` 造号，产物进**独立号池**（`data/poe-pool` / `out/poe-pool.sqlite3`），
和 DeepAI 的 `accounts[]` 完全隔离 —— 两者的凭据形态不同（DeepAI 是 api_key，
Poe 是 `p-b` 会话 cookie），混池会直接让调度器拿错东西。

## 先说清现实：Poe 在手机号这一步设了墙

实测（2026-11-25，US 住宅出口，Camoufox 过 Cloudflare）：

```
GET  https://poe.com/login                      → 200（cf_clearance / p-b 落地）
POST /api/gql_POST  …sendVerificationCodeMutation_Mutation
     {emailAddress, phoneNumber: null, recaptchaToken}
     → {"status":"user_with_confirmed_email_not_found"}   6 位验证码立刻到邮箱
POST /api/gql_POST  …signupWithVerificationCodeMutation_Mutation
     {verificationCode, emailAddress, phoneNumber: null, subscriptions: []}
     → 账号建立，页面直接切到 "Add phone number / +1 / Add"
```

也就是说：**邮箱段全程可自动化（含一次性域名），手机段是硬门槛**，没有 Skip。
所以本工具默认把账号做到 `phone_required` 并存下 cookie；要变 `active`，
需要一个短信供应商（`sms.provider = manual | sms_activate | smspool`）。

## 双引擎

| 引擎 | 怎么干活 | 速度 | 什么时候用 |
| --- | --- | --- | --- |
| `protocol` | 只用 Camoufox 挣 `cf_clearance` + `poe-formkey`，然后用**页内 `fetch`** 直接打 `/api/gql_POST`（带现场取的 reCAPTCHA v3 token），完全不碰表单 | 快，抗改版 | 默认 |
| `browser` | 真填表单：`Go` → 验证码 → `Verify`，拟人打字 + 输入修复 | 慢 | protocol 被拒、或页面流程变了要兜底 |
| `auto` | protocol 失败自动降级 browser | — | 推荐 |

协议模板（queryName + content hash + 变量形状）在 `poe_reg/gql_templates.py`，
Poe 换 hash 时把新值放进 `out/gql_templates.json`（同名 key，自动覆盖），不用改代码。

## 防封策略（Poe 看的是 IP/设备声誉，不是邮箱域名）

| 策略 | 实现 |
| --- | --- |
| 一号一出口 | 代理池轮转；同一代理两次注册之间强制冷却 `account_cooldown`（默认 120 s） |
| 被判定自动化就拉黑出口 | `automated_request_detected` → 该代理 bench 15 分钟，账号换代理重试 |
| CF 挑战别硬刚 | Cloudflare 未过 → 该代理 bench 10 分钟 + 截图留证 |
| 一号一指纹 | 每次注册新开 Camoufox（preset 指纹 + 固定 1366×768 + 16 核），不复用 profile |
| 拟人节奏 | 打字逐字符 55–110 ms、点击带鼠标轨迹、进站先滚动、账号间 25–55 s 抖动 |
| 会话钉住 | `p-b` / `cf_clearance` 入库，后续不在别的 IP 重新登录（避免异地风控） |
| 失败留痕 | `events` 表记 `automated` / `cloudflare` / `email_rejected` / `code_missing` 等 |

## 用法

```powershell
cd tools\poe-reg
python -m pip install -r requirements.txt

python -m poe_reg doctor                       # 依赖 / 出口 IP / Poe 边缘 / 邮箱 / 模板 hash
python -m poe_reg run -n 1 --engine protocol   # 协议引擎跑一个
python -m poe_reg run -n 2 --engine browser --headful
python -m poe_reg pool                         # 看号池
python -m poe_reg pool --export out/poe.jsonl  # 导出

# 带短信（能补完手机号就变 active）
python -m poe_reg run -n 1 --sms-provider sms_activate
#   POE_SMS_API_KEY=… 环境变量或 config.yaml 里 sms.api_key
```

## 配置

见 `config.example.yaml`。关键项：

```yaml
engine: auto
concurrency: 1                 # Poe 强风控，建议 1
delay: [25, 55]                # 账号之间
account_cooldown: 120          # 同一代理两次注册的最小间隔
proxies: [http://127.0.0.1:10808]
mail:
  provider: tempmail_lol       # 复用 tools/deepai-reg 的邮箱层
sms:
  provider: off                # off | manual | sms_activate | smspool
  country: "0"
  service: poe
```

## 号池与联动

`poe_reg/pool.py` 是独立 SQLite：

```
accounts(email, status, engine, proxy, cookies, formkey, phone, sms_provider, …)
statuses: email_verified | phone_required | active | dead
```

给网关用时：`--push-url http://127.0.0.1:8787 --push-token <口令>`，
每个账号建完立刻 `POST /api/poe/accounts`，网关注入 `data/poe-pool/` 并让 WebUI 的
「Poe 号池」面板读同一份数据（与 DeepAI 池并行，互不影响）。
