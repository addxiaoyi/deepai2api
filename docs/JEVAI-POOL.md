# jev-ai.org 号池（`dcg.pool/jevai@1`）

注册 jev-ai.org、领 `sk-glm5-…` Key、把 Key 池化，并**自动接入**其它工具/线。

- 文件：`$DEEPAI_GATEWAY_DATA/jevai-pool.json`（0600，原子写）
- 产物：账号 + `apiKey`（`sk-glm5-…`）+ 每个账号的网关 Key（`sk-dcg-…`）
- worker：`tools/jevai-reg`（`-m jevai_reg`）
- job：`data/jevai-register-job.json`
- 自动接入：`data/keys/jevai.txt`（一行一把 Key）、`data/keys/jevai.json`（含 `openai_base`）
- 路由：`/api/admin/jevai/*`（管理面）、`/jevai/v1/*`（OpenAI 兼容反代）

## 1. 上游协议（实测）

站点是 Next.js + **Better Auth**，模型 API 在**独立基址** `https://jev-ai.org/api/v1`。

| 用途 | 请求 | 说明 |
| --- | --- | --- |
| 健康 | `GET /api/auth/ok` | `{"ok":true}` |
| 注册 | `POST /api/auth/sign-up/email` `{email,password,name}` | Better Auth 标准入参 |
| 登录 | `POST /api/auth/sign-in/email` `{email,password}` | |
| 会话 | `GET /api/auth/get-session` | 未登录回 `null` |
| 发验证信 | `POST /api/auth/send-verification-email` `{email}` | |
| 验证 | `GET /api/auth/verify-email?token=…` | 邮件里的链接 |
| 账号信息 | `GET /api/user/get-user-info` | |
| 额度 | `GET /api/user/get-user-credits`、`/api/user/get-credit-status` | |
| 签到 | `POST /api/user/checkin` | 每日白拿额度 |
| 模型 API | `GET /api/v1/models`、`POST /api/v1/systemone` | `Authorization: Bearer sk-glm5-…` |
| Key 页面 | `https://jev-ai.org/settings/apikeys` | 前端签发，见下 |

**域名黑名单**（实测）：一次性/占位邮箱直接被拒

```json
{"code":"EMAIL_DOMAIN_NOT_ALLOWED","message":"Temporary or placeholder email addresses cannot be used. Please sign up with a mailbox you can access."}
```

所以要用**自有域名 catch-all + IMAP**（或 `--mail-provider fixed/mailtm` 试运气）。

**Key 端点未知**：`/settings/apikeys` 是客户端渲染，建 Key 的接口名不在首屏 bundle 里。
worker 因此用**候选探测 + 命中缓存**：按 Better Auth/Next 的常见约定路径逐个试
（`/api/user/api-keys`、`/api/apikey`、`/api/keys`、`/api/settings/apikeys` …），
谁的响应里出现 `sk-glm5-…` 就用谁，并写进 `data/key-endpoints.json`，下次直接用。
探测失败会明确报 `no_api_key`，不会假装成功。

## 2. 顶层结构

```json
{
  "schema": "dcg.pool/jevai@1",
  "version": 1,
  "provider": "jevai",
  "baseUrl": "https://jev-ai.org",
  "apiBase": "https://jev-ai.org/api/v1",
  "updatedAt": "2026-11-26T21:40:00.000Z",
  "accounts": []
}
```

### accounts[]

```json
{
  "id": "…",
  "email": "ada@example.com",
  "password": "…",
  "name": "Ada Lovelace",
  "apiKey": "sk-glm5-…",
  "keyPrefix": "sk-glm5-",
  "userId": "…",
  "emailVerified": true,
  "credits": {"credits": 100},
  "status": "active",
  "engine": "protocol",
  "gatewayKey": {"id": "…", "label": "jevai:ada@example.com", "prefix": "sk-dcg-…", "plaintext": "sk-dcg-…"},
  "healthy": true,
  "cooldownUntil": 0,
  "requests": 0,
  "failures": 0,
  "createdAt": "…",
  "updatedAt": "…"
}
```

约定：

- 去重键：先 `apiKey`，再 `email`（同一账号换 Key 只更新，不新建）。
- `healthy=false` 或 `cooldownUntil>now` 的账号不会接流量（`pick()` 跳过）。
- **网关 Key 只发一次**：`importAccount()` 里 `mintKey`，已有就复用；
  `gatewayKey.plaintext` 是唯一一次能拿到明文的地方，Store 里只有哈希。

## 3. 反代面 `/jevai/v1/*`

```
GET  /jevai/v1/models              -> 上游 /api/v1/models
POST /jevai/v1/systemone           -> 上游 /api/v1/systemone
POST /jevai/v1/chat/completions    -> 原样透传（上游若未开放会回 404 + 人话）
```

- 下游只看到网关的 `sk-dcg-…`；上游 `Authorization` 由网关换成池里的 `sk-glm5-…`。
- 401/403/429/5xx → 该账号进冷却（401/403 冷却 10 分钟，429 冷却 1 分钟）并**换号重试**（最多 3 个）。
- 其它 4xx（400/404）**不算 Key 的问题**，不会把账号打死。
- 上游回 `{"error":{"message":"Unknown API endpoint…"}}` 时翻成
  `code: "upstream_endpoint_missing"`，明确告诉你"不是鉴权问题"。
- SSE 直通（`text/event-stream` + `X-Accel-Buffering: no`）。

## 4. 自动接入其它工具 / 其它线

任务 `done` 后自动 `apply()`，写两个文件：

```
data/keys/jevai.txt     一行一把 sk-glm5-…（喂给任何 OpenAI 客户端）
data/keys/jevai.json    {"provider":"jevai","openai_base":"https://jev-ai.org/api/v1","keys":[…]}
```

接入点：

| 入口 | 用途 |
| --- | --- |
| `POST /api/admin/jevai/apply` | 重新导出上面两个文件 |
| `GET /api/admin/jevai/connect` | 返回 `gatewayBase=/jevai/v1`、上游基址、可用网关 Key 前缀 |
| `POST /api/admin/jevai/pool/import` | 外部账号+Key 入池，顺便补发网关 Key 并刷新导出 |
| `node src/jevai-cli.mjs apply` / `import <file>` | 命令行版 |

Open WebUI 里接它：类型 **OpenAI**，Base URL 填 `http://<网关>:8787/jevai/v1`，
Key 用管理台发的 `sk-dcg-…`；也可以直接拿 `data/keys/jevai.txt` 里的
`sk-glm5-…` + 上游基址接（绕过网关，但就没有轮询/故障切换了）。

## 5. 免费渠道（能刷的 / 刷不动的）

从 bundle 与真实响应里能确认的免费入口只有四个，其余都要付钱：

| 渠道 | 证据 | 能不能靠注册机放大 |
| --- | --- | --- |
| **注册赠送额度** | UI 里 `sign_in_to_claim` + `credits`；未付费账号 `chatTier="free"` | ✅ 一个号一份，号池线性放大 |
| **每日签到** `POST /api/user/checkin` | 签到弹层有 `status_today` / `status_claimed` / `status_bonus`（连签加成）和 `+N` 额度 | ✅ 每号每天一次，号越多日总量越大 |
| **免费档每日限额** `free_daily_limit` | 额度不足弹窗的 `reason` 枚举里明确有 `free_daily_limit` | ✅ 每个号每天都有一份，轮换着吃 |
| 免费模型子集 | `premium_model` 是另一种 `reason`：free 档只能用部分模型 | ✅ 配合上面的额度用 |
| ~~邀请奖励~~ | `referral` 只出现在 `affonso` / `promotekit` 的**付款** metadata 里 | ❌ 注册不带奖励，别白费号 |
| ~~试用付费档~~ | 只有 `/api/payment/checkout` + `/api/subscription/*`，没有 trial 端点 | ❌ |

所以"刷免费"的正确姿势是**广撒号 + 每日签到 + 吃满每日限额**，而不是找邀请码：

```bash
# 1) 铺号（每个号自带一份注册额度 + 每天一份免费限额）
python -m jevai_reg run -n 20 -c 1 --mail-provider imap --mail-domain example.com

# 2) 每天把额度刷一遍（--only-today 让重复触发变空转）
python -m jevai_reg checkin --only-today --pause 5

# 网关侧同一件事
node src/jevai-cli.mjs checkin
curl -XPOST -H "Authorization: Bearer $TOKEN" http://127.0.0.1:8787/api/admin/jevai/checkin -d '{"onlyToday":true}'
```

cron（每天 09:10 一次，签到有连签加成，别断）：

```cron
10 9 * * * cd /opt/deepai2api && ./node_modules/.bin/… 2>/dev/null || node src/jevai-cli.mjs checkin --only-today >> data/jevai-checkin.log 2>&1
```

`checkin` 的行为：逐账号 `sign-in`（用池里存的密码换新 session）→ 读签到前额度 →
`POST /api/user/checkin` → 读签到后额度 → 落库（`last_checkin`、`checkin_count`、`credits`）。
跳过今天已签的、跳过没存密码的，失败按 `kind` 记录，不会中断整批。

> 注意：`POST /api/user/get-credit-status` 是 **POST**（前端就是 `fetch(..., {method:"POST"})`），
> 用 GET 只会拿到 Next 的 404 HTML —— worker 已按 POST 实现，回 `{code, data}` 且 `code!==0` 视为不可用。

## 6. 接口
```
GET    /api/admin/jevai/status
POST   /api/admin/jevai/register            {count, concurrency, mailProvider, mailDomains[], keyName, proxies[], verifyEmail, checkin, export}
POST   /api/admin/jevai/register/stop
POST   /api/admin/jevai/checkin             {onlyToday?, limit?, pause?, proxies?}  每日签到刷免费额度
GET    /api/admin/jevai/pool?limit=200      账号（脱敏，Key 只给 preview）
POST   /api/admin/jevai/pool/import         {email, apiKey, emailVerified?, credits?, mintKey?}
POST   /api/admin/jevai/pool/export?format=keys|jsonl|csv|pool[&download=1]
DELETE /api/admin/jevai/pool/{id}
GET    /api/admin/jevai/keys                每账号的 Key preview + 网关 Key
POST   /api/admin/jevai/apply               写 data/keys/jevai.{txt,json}
GET    /api/admin/jevai/connect             接入信息（网关基址 / 上游基址 / Key 前缀）
```

用户面：`/jevai/v1/*`（需 `sk-dcg-…`）。

## 7. 命令

```bash
cd tools/jevai-reg && pip install -r requirements.txt

python -m jevai_reg doctor                      # 站点 / 认证面 / 模型 API 探针 / 端点缓存
python -m jevai_reg run -n 5 -c 1 \
    --mail-provider imap --mail-domain example.com   # 注册 + 建 Key
python -m jevai_reg keys                        # 一行一把
python -m jevai_reg checkin --only-today        # 签到（免费额度）
python -m jevai_reg export --format keys --out out/keys.txt

# 网关侧
node src/jevai-cli.mjs register -n 5 --mail-domain example.com
node src/jevai-cli.mjs keys
node src/jevai-cli.mjs apply
node src/jevai-cli.mjs test-key                 # 用池里的 Key 打 /api/v1/models
```

## 8. 闭环位置

```
注册(tools/jevai-reg) ──▶ POST /api/admin/jevai/pool/import ──▶ jevai-pool.json
                                     │                              │
                                     ├─ 发网关 Key sk-dcg-…          ├─ apiKey sk-glm5-…
                                     └─ apply() ──▶ data/keys/jevai.{txt,json} ──▶ 其它工具/线
                                                                     │
                                              /jevai/v1/*  ◀── 号池轮询 + 冷却 + 故障切换
```

## 9. 验证

```bash
node scripts/check-loop.mjs                        # 这条线在矩阵里
node --test tests/jevai-pool.test.mjs              # 池：合并/去重/轮询/导出/网关 Key
node --test tests/jevai-routes.test.mjs            # 路由：管理面 + 反代鉴权 + 不冲突
python -m pytest tools/jevai-reg/tests -q          # 协议：注册/验证/Key 端点探测/落库
```
