# Novita 号池（`novita-pool.json`）—— 协议、防封与接入

本文记录 **Novita AI** 这条线在本仓库里的位置：注册 worker、号池文件、反代面、
工具层，以及它和其它 provider（wavespeed / moonai / zo / allmodels / deepai）为什么
不会互相踩。

```
tools/novita-reg/（Python worker：协议 + 指纹浏览器双腿）
        │  NDJSON 事件（start/attempt/account/failed/done…）
        ▼
src/provider-register.mjs ──► src/provider-pool.mjs ──► data/novita-pool.json
        │                                                  │
        │  src/provider-admin.mjs  /api/admin/providers/*   │
        ▼                                                  ▼
src/provider-relay.mjs  /p/novita/v1/*   ◄──── 网关 Key（sk-dcg-…）+ 面板 Key 轮询
        ▲
        │  src/provider-tools.mjs（唯一工具 schema，三个入口共用）
   src/provider-mcp.mjs（MCP stdio） / src/provider-cli.mjs / HTTP 桥
```

---

## 1. 协议：从线上 SPA 还原（2026-11 实测）

面板 `https://novita.ai` 是 Next.js；**账号与 Key 接口在另一个 host**：

| 用途 | 请求 | 说明 |
| --- | --- | --- |
| 面板 API 基址 | `https://api-server.novita.ai` | 前端常量 `I`（chunk 15825 的 `T({base_url:I})`） |
| 注册 | `POST /v1/user/register` | `{email,password,confirmPassword,cloudflareToken,redirectUrl,allowNotification,fromInviteCode,agreementVersion,...utm}` |
| 登录 | `POST /v1/user/login` | 同上少 `confirmPassword`；返回 `{token}` |
| 邮箱激活 | `POST /v1/user/email/verify` | 激活链接 `https://novita.ai/settings/verify?token=…` 里抽 token |
| 用户信息 | `GET /v1/user/info` | Bearer JWT |
| Key 列表 | `GET /v2/user/key` | 只回元信息（`keyPrefix` / `expireTime`），**无明文** |
| **Key 创建** | `POST /v2/user/key` | 明文只出现一次：`{name, expireTime}`（`expireTime` 为秒，`-1` = 永久） |
| Key 改/删 | `PUT·DELETE /v2/user/key[/{id}]` | 模型白名单、IP 白名单 |
| 推理（OpenAI） | `https://api.novita.ai/openai/v1/chat/completions` | `Authorization: Bearer sk_…` |
| 推理（Anthropic） | `https://api.novita.ai/anthropic/v1/messages` | Claude Code 可直接接 |
| 余额 | `GET /openapi/v1/billing/balance/detail` | 单位 **1/10000 USD** |
| 异步图像 | `POST /v3/async/txt2img` → `GET /v3/async/task-result?task_id=` | 参数包在 `request` 对象里 |

### 两条硬约束（实测，不是推测）

1. **Turnstile 是强制的。** 注册缺 token → `500 {"reason":"CLOUDFLARE_CHECK_ERROR"}`；
   登录缺/错 token → `400 {"reason":"VERIFY_TOKEN_FAILED"}`。
   sitekey `0x4AAAAAAAaG28VfN_OxkED8`（chunk 99745 的 `fE`），字段名 `cloudflareToken`。
2. **必须邮箱激活。** 注册成功只发信（"Activation email sent"），不激活则登录报
   `The user is not activated`。所以 worker 的邮箱腿不是可选件 —— 域名 catch-all 是刚需。

### 错误语义

Novita 的失败**不能只看 HTTP 状态**：`500` 也可能是业务错误，`200` 也可能带 `errorNo`。
worker 统一按 `reason`（优先）→ `errorNo` → `code` 取业务码，再映射成 runner 词汇：

| 线上 reason / 文案 | worker 异常 | kind | runner 的动作 |
| --- | --- | --- | --- |
| `CLOUDFLARE_CHECK_ERROR` / `VERIFY_TOKEN_FAILED` | `CaptchaRequired` | `captcha_required` | 退避 + 换引擎（协议→浏览器） |
| `USER_ALREADY_EXIST` | `EmailTaken` | `email_taken` | 换地址，零退避 |
| `INVALID_EMAIL` / `please use your own email address` | `EmailRejected` | `email_rejected` | 换域名（该域名健康分降权） |
| `USER_IS_LOCKED` | `RateLimited` | `rate_limited` | 退避 + 换出口 |
| 401/403 + `identity verification` | `VerifyRequired` | `verify_required` | 弃号（实名门槛） |
| 402 | `QuotaExhausted` | `quota_exhausted` | 弃号 |
| 建 Key 响应无明文 / 仅 `maskedKey` | `KeyNotObtainable` | `key_not_obtainable` | 换引擎（浏览器点 UI） |

> `maskedKey` 也以 `sk_` 开头，但带 `*`。worker 的明文提取要求
> `sk_` 前缀 + 长度 ≥ 20 + 不含 `*`，避免把空壳 key 写进号池。

---

## 2. 号池文件

`data/novita-pool.json`（`0600`、原子替换、`data/` 已在 `.gitignore`）：

```jsonc
{
  "schema": "dcg.pool/novita@1",
  "version": 1,
  "provider": "novita",
  "panelBase": "https://novita.ai",
  "apiBase": "https://api.novita.ai/openai",
  "accounts": [
    {
      "id": "…", "email": "…", "password": "…",
      "apiKey": "sk_…", "keyId": "…", "keyPrefix": "sk_…", "keyLast4": "…",
      "extraKeys": [], "expireTime": "", "engine": "protocol",
      "domain": "example.com", "proxy": "",
      "cookies": [], "emailVerified": true,
      "status": "active", "healthy": true, "cooldownUntil": 0,
      "requests": 0, "failures": 0,
      "gatewayKey": { "id": "…", "label": "novita:…", "prefix": "sk-dcg-…", "plaintext": "sk-dcg-…" }
    }
  ]
}
```

**隔离约定**（新增 provider 时照抄这三条就不会冲突）：

| 资源 | 命名 | 由谁派生 |
| --- | --- | --- |
| 号池文件 | `data/<provider>-pool.json` | `provider.id` |
| 注册锁 | `data/<provider>-register.lock` | `provider.id` |
| job 状态 | `data/<provider>-register-job.json` | `provider.id` |
| worker 数据 | `data/<provider>-reg/`（sqlite + jsonl） | 网关启动 worker 时传 `--out-dir` |
| 路由 | `/p/<provider>/v1/*`、`/api/admin/providers/<provider>/*` | 同上 |

`src/provider-pool.mjs` 在读写时会校验 `schema`/`provider` 字段，**别的 provider 的
池子一律不读**；`src/providers.mjs` 只装配 `DCG_PROVIDERS` 里列出的 provider。
`tests/novita-pool.test.mjs` 对这两点有断言（写 novita 不会让 wavespeed 池子出现
别人的账号，反之亦然）。

---

## 3. 双引擎（协议 + 指纹浏览器）

| `--engine` | 行为 |
| --- | --- |
| `protocol` | 纯 HTTP：注册 → 等信 → 激活 → 登录 → `POST /v2/user/key` 拿明文。最快 |
| `browser` | 真浏览器开 `/user/register` 过 Turnstile、填表提交；登录后在 `/settings/key-management` 点 "Add key" 读明文 |
| `auto` / `hybrid` | 先协议；遇到 `captcha_required` / `blocked` / `key_not_obtainable` 当场切浏览器重跑，`engine` 字段记真实来源 |

浏览器内核按可用性自动挑：`camoufox`（Firefox，指纹改写最彻底）→ `patchright`
（Playwright 反检测分支）→ `playwright`；也可以用 `--cdp http://127.0.0.1:50325`
直接接管 AdsPower / BitBrowser / Morelogin 的 profile。

浏览器腿**不碰邮箱**：激活仍然由协议腿抽 token → `POST /v1/user/email/verify`，
比在浏览器里点邮件链接稳。两条腿共享同一个 persona（同一套 UA / 时区 / 语言），
所以平台看到的始终是"同一台机器"。

---

## 4. 防封策略（`anti_ban`）

网关的 `DEFAULT_ANTI_BAN` 用 **camelCase**（JS 习惯），worker 的 `AntiBanConfig` 用
**snake_case**（Python 习惯）。两者之间有显式别名表（`AntiBanConfig.CAMEL_ALIASES`），
**没有这张表，网关下发的策略会被静默丢弃** —— 面板显示"防封已就绪"，实际全跑默认值。
`tests/test_novita_offline.py::test_camel_case_from_gateway_actually_applies` 锁住了这一点。

| 维度 | 字段 | 作用 |
| --- | --- | --- |
| 域名邮箱 | `domain_rpm` / `domain_rph` / `domain_health_floor` | 单域名分钟/小时双窗 + 健康分降权（失败率高的域名暂停） |
| 出口 | `ip_concurrency` | 同一出口同时只跑 1 个（并发最容易踩的坑） |
| 出口 | `proxy_rotate` | 按账号轮换代理；`account_locked`/`blocked` 的出口直接退休 |
| 节奏 | `jitter_ms` | 区间随机，避免固定间隔的机器节奏 |
| 指纹 | `geo_align` | 时区/语言跟随出口地理 |
| 设备 | `fresh_profile` / `isolate_profile` / `clear_storage_between` | 每号独立 profile + 独立噪声种子 + 清 storage（同设备多号的 voucher 撤销主因） |
| 会话 | `reuse_session` / `session_ttl_hours` | 同一出口的登录态复用，减少重复挑战 |
| 失败语义 | `backoff.{kind}` | 按 kind 决定退避秒数；`0` 表示"别等，直接换身份" |

---

## 5. 网页反代出 apikey

注册机产出的**面板 Key** 直接进号池；对外暴露的是网关自己的 `sk-dcg-…` Key：

```bash
# 网关（自带 /v1 与 /p/<provider>/* 两套面）
node src/index.mjs --port 8787

# 客户端只认网关 Key，不碰面板 Key
curl http://127.0.0.1:8787/p/novita/v1/chat/completions \
  -H "Authorization: Bearer sk-dcg-…" -H "Content-Type: application/json" \
  -d '{"model":"deepseek/deepseek-v4.1-flash","messages":[{"role":"user","content":"ping"}]}'
```

- `/p/novita/v1/models`、`/v1/chat/completions`、`/v1/completions`、`/v1/responses`
- `/p/novita/v1/messages`（Anthropic 形状，网关翻译成上游 chat）
- `/p/novita/v1/async/*`（异步图像提交 / 轮询）
- 401/402/403/429/5xx 自动换号并回报冷却（`recordResult`）

路径拼装：`provider.apiBase` 已经是 `https://api.novita.ai/openai`，反代只再拼
`/v1/chat/completions`。**不要**再叠一层 `/openai`（测试里有断言防回归）。

### 接进编程工具

| 工具 | 配置 |
| --- | --- |
| Cursor / Cline / Continue | OpenAI Base URL `http://127.0.0.1:8787/p/novita/v1`，Key = 网关 Key，Model = 号池可用模型 |
| Claude Code | `ANTHROPIC_BASE_URL=http://127.0.0.1:8787/p/novita/v1`、`ANTHROPIC_AUTH_TOKEN=<网关 Key>`（走 `/v1/messages`） |
| 任何 MCP 客户端 | `.mcp.json` 里已注册 `providers` → `node src/provider-mcp.mjs` |

MCP 工具（schema 与 HTTP 桥、CLI 同一份 `src/provider-tools.mjs`）：

```
provider_list / provider_status / provider_register / provider_register_stop /
provider_pool / provider_export / provider_import / provider_mark_health /
provider_remove / provider_probe / provider_relay_info / provider_antiban
```

---

## 6. 用法

```powershell
# 1) 依赖
python -m pip install -r tools\novita-reg\requirements.txt

# 2) 自检（端点 / sitekey / 邮箱 / 浏览器内核 / 打码配置）
cd tools\novita-reg
python -m novita_reg doctor
python -m novita_reg probe

# 3) 跑一批（域名邮箱 + 协议优先，被拦切指纹浏览器）
python -m novita_reg run -n 5 -c 2 --engine auto `
  --mail-provider domain --mail-domain example.com `
  --captcha capsolver --captcha-key $env:NOVITA_CAPTCHA_KEY `
  --proxy http://127.0.0.1:7890 `
  --push-url http://127.0.0.1:8787 --push-token <网关口令> --json

# 4) 网关侧（等价入口）
curl -X POST http://127.0.0.1:8787/api/admin/providers/novita/register \
  -H "Authorization: Bearer <网关口令>" -H "Content-Type: application/json" \
  -d '{"count":5,"concurrency":2,"engine":"auto","mailProvider":"domain","mailDomains":["example.com"],"captcha":"capsolver","export":"jsonl"}'
curl http://127.0.0.1:8787/api/admin/providers/novita/pool
curl -X POST "http://127.0.0.1:8787/api/admin/providers/novita/pool/export?format=csv&download=1" \
  -H "Authorization: Bearer <网关口令>" -o novita-accounts.csv
```

环境变量（前缀 `NOVITA_`，与 `ALLMODELS_*` / `DEEPAI_*` / `WAVESPEED_*` 互不覆盖）：
`NOVITA_PANEL_BASE`、`NOVITA_PANEL_API`、`NOVITA_API_BASE`、`NOVITA_CAPTCHA_KEY`、
`NOVITA_IMAP_HOST` / `_USER` / `_PASSWORD`、`NOVITA_PROXY`、`NOVITA_GATEWAY_URL` / `_TOKEN`。

---

## 7. 测试

| 用例 | 结果 |
| --- | --- |
| `python tools/novita-reg/tests/test_offline.py`（既有：错误映射/退避/速率窗/多 key/余额/异步） | 21/21 |
| `python -m pytest tools/novita-reg/tests/test_novita_offline.py -q`（协议体/错误映射/JWT/明文校验/防封映射/配置归一） | 15/15 |
| `node --test tests/novita-pool.test.mjs`（号池隔离/幂等发 Key/反代路径/CLI 契约/路由前缀） | 8/8 |
| `node --test tests/providers-wavespeed-novita.test.mjs`（provider 层与反代，既有） | 22/22 |

---

## 7.5 排障：三个"看起来很难，其实是配置"的坑

| 现象 | 原因 | 处理 |
| --- | --- | --- |
| `doctor` 说 `novita.ai` 不可达（`SSL: UNEXPECTED_EOF`） | 机器上的 `HTTPS_PROXY` 被当成默认出口 | 只认 `NOVITA_PROXY`；要用系统代理就显式 `NOVITA_USE_ENV_PROXY=1` |
| 面板显示"防封已就绪"但实际没生效 | 网关传 camelCase、worker 读 snake_case | 已在 `AntiBanConfig.CAMEL_ALIASES` 映射，测试锁住 |
| 注册任务秒退（exit 2） | worker CLI 缺网关会传的参数（`--out-dir` / `--engine hybrid`） | 已对齐，`tests/novita-pool.test.mjs` 静态比对参数与事件名 |
## 8. 与其它模块的边界清单

- **不碰** `data/allmodels-pool.json`、`data/deepai-*`、`data/poe-*`、`data/moonai-*`：
  号池按 provider 物理隔离，`readPoolFile()` 会拒绝 schema/provider 不匹配的文件。
- **不注册** 与 `allmodels_*` 重名的 MCP 工具：provider 工具一律 `provider_*`。
- **不改** 既有 `/v1/*`（DeepAI 面）与 `/api/admin/allmodels/*`：novita 只在
  `/p/novita/*` 与 `/api/admin/providers/novita/*` 下新增路由。
- **不共享** worker 数据：每个 provider 的 sqlite/jsonl 落在 `data/<provider>-reg/`，
  由网关传 `--out-dir`，避免两个 dataDir 的网关共用同一份去重库。
- **不假设** 端口：MCP / CLI / 网关共用 `config.port`，`--data` 可把三者指到同一目录。
