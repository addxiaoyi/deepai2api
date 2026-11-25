# Novita AI 注册 worker（`novita-reg`）

Novita AI 的账号流水线：邮箱注册 → 邮件激活 → 建 key（拿明文）→ 入池。

- 代码：`tools/novita-reg/novita_reg/`
- 数据：`out/accounts.sqlite3`
- 号池推送：`POST /api/admin/providers/novita/pool/import`

## 1. 站点事实（含线上 chunk 反查）

面板 `https://novita.ai`，注册 `/user/register`，登录 `/user/login`。

从线上 SPA chunk 里读出来的真实常量与接口（不是猜测）：

| 项 | 值 | 来源 |
| --- | --- | --- |
| 面板 API 基址 | `https://api-server.novita.ai` | chunk 15825：`I = "https://api-server..."` |
| Turnstile sitekey | `0x4AAAAAAAaG28VfN_OxkED8` | chunk 99745：`fE = "0x4AAA..."` |
| Turnstile 字段名 | `cloudflareToken` | LoginForm / SignupForm 实参 |
| 注册 | `POST /v1/user/register` | `a.kz({email,password,confirmPassword,cloudflareToken,...})` |
| 登录 | `POST /v1/user/login` | `s.iD({email,password,cloudflareToken,...})` |
| 邮箱验证 | `POST /v1/user/email/verify` | chunk 15825 |
| Key 列表 | `GET /v2/user/key` | `l.Lw()` |
| **Key 创建** | `POST /v2/user/key` | `c.eX({...expireTime})` |
| Key 修改 | `PUT /v2/user/key` | `u._P(data)` |
| Key 删除 | `DELETE /v2/user/key/{id}` | `E.sV(id)` |
| OpenAI 兼容 | `https://api.novita.ai/openai` | chunk 3442：`r="https://api.novita.ai/openai"` |
| Anthropic 兼容 | `https://api.novita.ai/anthropic` | 同上：`a="https://api.novita.ai/anthropic"` |
| 异步图像 | `POST /v3/async/txt2img` → `GET /v3/async/task-result` | 官方文档 |
| 余额 | `GET /openapi/v1/billing/balance/detail`（单位 **1/10000 USD**） | 官方文档 |

**关键差别**：面板自己有创建 key 的接口（`POST /v2/user/key`），所以**协议腿能拿到
明文**；不像 WaveSpeed 必须点 UI。这让 Novita 的协议引擎是完整可用的。

另有一条重要限制：OpenAPI 侧的 `/openapi/v2/user/key` 只能**读**（返回 `maskedKey`），
不能创建。创建必须走面板的 `/v2/user/key`。

## 2. 快速开始

```bash
cd tools/novita-reg

python -m novita_reg doctor
python -m novita_reg probe          # 打印 panel_api / sitekey / openai_base

# 协议腿完整注册（需要 Turnstile token）
python -m novita_reg run -n 5 -c 2 \
  --mail-provider imap \
  --mail-domains your-domain.com \
  --captcha capsolver --captcha-key CAP-xxx \
  --key-policy '{"per_account":2,"expire_time":"90d"}' \
  --json

# 协议被 Cloudflare 拦时自动切浏览器（engine=auto 默认行为）
python -m novita_reg run -n 3 --engine auto --cdp http://127.0.0.1:50325

# 校验 / 余额 / 导出
python -m novita_reg verify
python -m novita_reg balance --key sk_xxx
python -m novita_reg export --format pool
```

## 3. 一个账号多把 key

Novita 每账号最多 10 把 key，有效期 Permanent / 90d / 30d / 24h，**创建后不可改**。

```bash
--key-policy '{
  "per_account": 3,
  "expire_time": "90d",
  "open_model_access": true
}'
```

- `per_account`：建几把（1–10）
- `expire_time`：可读值，内部经 `_normalize_expire()` 转成面板接受的秒数
  （`permanent → -1`、`90d → 7776000`、`30d → 2592000`、`24h → 86400`）
- `open_model_access`：建完用 `PUT /v2/user/key` 放开 model-access。
  Novita 默认会挡掉一部分模型（`model_access_denied`），打开就能用全量。

全部 key 都进 SQLite 的 `keys` 表，`pool_records()` 会把额外 key 塞进 `extraKeys`。

## 4. 域名邮箱

与 wavespeed worker 共用同一套 provider（`imap` / `domain` / `fixed` / `mailtm` /
`tempmail_lol`）。Novita 对一次性域名有名单，**自有 catch-all 域名是首选**：

```yaml
mail:
  provider: imap
  domain: your-domain.com
  imap_host: imap.your-domain.com
  imap_user: catchall@your-domain.com
  imap_password: "********"
```

激活邮件里的 token 两种都试：`?token=` 查询参数，或直接跟随激活链接
（`_token_from_link()` 先解析，解析不到就 GET 链接）。

## 5. 防封策略（`anti_ban`）

Novita 的风控比 WaveSpeed 多两点：**voucher 同设备多账号会被撤销**，
以及**部分模型要实名验证**。对策：

| 参数 | 默认 | 作用 |
| --- | --- | --- |
| `domain_rpm` / `domain_rph` | 3 / 40 | 单域名建号速率窗 |
| `domain_health_floor` | 0.25 | 健康分过低的域名暂停 |
| `ip_concurrency` | 1 | 同出口 IP 串行化 |
| `geo_align` | true | 时区/语言跟随代理出口 |
| **`isolate_profile`** | **true** | 每账号独立 profile 目录 |
| **`clear_storage_between`** | **true** | 进页面前清 localStorage / sessionStorage |
| `reuse_session` | true | 复用 cookie jar |

`profile_isolation_plan()` 负责算出每账号的隔离方案：

```python
from novita_reg.antiban import profile_isolation_plan

plan = profile_isolation_plan("a@example.com", root="profiles", seed=11)
# {'profile_dir': 'profiles/a_example.com', 'isolated': True,
#  'clear_storage': True, 'noise_seed': 11}
```

每个账号拿到**不同的 profile 目录**和**不同的 canvas/WebGL 噪声种子**，
这是"同设备多账号"检测的直接对策。

失败退避表比 wavespeed 多两项零退避：

```python
{
  "verify_required": [0, 0],    # 实名门槛，换账号
  "quota_exhausted": [0, 0],    # 余额/voucher，换账号
  "key_not_obtainable": [0, 0],
  "email_taken": [0, 0],
  "rate_limited": [20, 45],
  "blocked": [20, 40],
  "captcha_required": [15, 30],
}
```

## 6. 失败分类

| kind | 含义 | 处置 |
| --- | --- | --- |
| `email_taken` | 地址已注册 | 换地址 |
| `email_rejected` | 地址被拒 | 换域名 |
| `password_rejected` | 密码不合规 | 换更强密码 |
| `blocked` | Cloudflare | 换引擎 + 退避 |
| `captcha_required` | Turnstile 拒绝 | 换引擎 + 退避 |
| `verification_pending` | 没收到激活信 | 短退避 |
| `key_not_obtainable` | 拿不到明文 / 达上限 | 换引擎或换账号 |
| `verify_required` | 需实名（403） | **换账号** |
| `quota_exhausted` | 402 / voucher 撤销 | **换账号** |
| `rate_limited` | 429（RPM/TPM） | 退避 + 换代理 |

## 7. 推理接口

### LLM（OpenAI 兼容）

base 已含 `/openai`，SDK 自己再拼 `/v1/chat/completions`：

```python
from novita_reg.protocol import Novita

with Novita() as client:
    response = client.chat(key, {
        "model": "deepseek/deepseek-v4.1-flash",
        "messages": [{"role": "user", "content": "hi"}],
        # 私有扩展字段原样透传
        "enable_thinking": True,
        "top_k": 40,
        "min_p": 0.05,
    })
```

注意：`top_logprobs` 不支持；`json_schema` 文档自相矛盾（chat-completion 页列出了，
LLM FAQ 说暂不支持），所以 fixture 校验允许透传未知字段。

### 图像（异步任务）

```python
task_id = client.async_image(key, "sd_xl_base_1.0.safetensors", {"prompt": "a cat"})
result = client.poll_task(key, task_id, poll=3.0, timeout=300)
```

状态机：`TASK_STATUS_QUEUED` → `TASK_STATUS_PROCESSING` → `TASK_STATUS_SUCCEED`
/ `TASK_STATUS_FAILED`（带 `progress_percent` / `eta` / `reason`）。

参数结构与 WaveSpeed 不同：Novita 把参数包在 `request` 对象里，WaveSpeed 直接平铺。

### 余额

```python
client.balance()   # {'availableBalance': 125000, 'usd': 12.5}
```

原始值是 1/10000 USD 的整数，`balance()` 会额外算一个 `usd` 字段。

## 8. 测试

```bash
cd tools/novita-reg
python tests/test_offline.py
```

覆盖：错误状态映射（401/402/403/429）、`model_access_denied` 不被误判为拦截、
退避表、域名速率窗、多 key 与 `keys` 表、余额单位换算、异步状态机、
注册请求体字段（`cloudflareToken` / `confirmPassword` / `agreementVersion`）、
HTTP 200 + `errorNo` 的处理、建 key 拿明文、profile 隔离计划。

## 9. 限制

- **Turnstile 必过**：sitekey 已知，但 token 需要解题器或真实浏览器；
  `--captcha auto` 会先不带 token 试一次，被拒再按配置解题。
- **实名门槛**：部分模型 403 `Model requires identity verification`，
  这是账号级限制，代码只能识别并换账号。
- **voucher 风控**：同设备多账号会被撤销赠金 —— `isolate_profile` 就是为此存在。
- **OpenAPI 不能建 key**：只有面板的 `/v2/user/key` 能建，且明文只出现一次。
