# gab.ai 号池协议（`dcg.pool/gabai@1`）

gab.ai 的形态是六条线里最省事的一条：**上游本身就说 OpenAI / Anthropic**，所以反代层
不做形状翻译，只做三件事 —— 换 `Authorization`、轮询账号、故障切换。

- 文件：`$DEEPAI_GATEWAY_DATA/gabai-pool.json`（`0600`，原子替换写入）
- 凭据：面板签发的 `gab_…` key（**不是**账号密码；密码只用于补建 key）
- 路由：`/gab/v1/*`、`/api/admin/gabai/*`
- worker：`tools/gabai-reg/gabai_reg`（`-m gabai_reg`）
- job 状态：`data/gabai-register-job.json`

## 1. 账号条目

```json
{
  "id": "1f3c…",
  "email": "ada@example.com",
  "name": "Ada",
  "apiKey": "gab_live_…",
  "keyPrefix": "gab_",
  "status": "active",
  "engine": "protocol",
  "createdAt": "2026-11-26T03:00:00.000Z",
  "updatedAt": "2026-11-26T03:00:02.000Z",
  "requests": 0,
  "failures": 0,
  "plusBlocked": false,
  "healthy": true,
  "cooldownUntil": 0,
  "lastError": null,
  "gatewayKey": {
    "id": "k-7",
    "label": "gabai:ada@example.com",
    "prefix": "sk-dcg-AbCdEfGh",
    "plaintext": "sk-dcg-…",
    "createdAt": "2026-11-26T03:00:02.000Z"
  }
}
```

- `plusBlocked`：上游回 `403 plus_required` 时置位 —— 这类账号不是坏了，是**套餐不够**，
  转发层会跳过它但不会把它标成 unhealthy。
- 健康字段由调用结果驱动：401/403 → 冷却 10 min，429 → 45 s，其余可重试状态 → 10 min。

## 2. 反代面

```
GET  /gab/v1/models
POST /gab/v1/chat/completions     # OpenAI
POST /gab/v1/completions
POST /gab/v1/responses
POST /gab/v1/messages             # Anthropic（Claude Code）
```

- 鉴权：`Authorization: Bearer sk-dcg-…`；上游用号池里的 `gab_…`。
- 直通：请求体原样转发，只覆盖 `authorization`；响应体（含 SSE）原样搬回，
  响应头补 `x-gab-account` 方便排障。
- 故障切换只发生在**响应头发出之前**，所以流式不会被截断。
- `X-Session-Affinity` / `x-gab-session-id` 可粘住同一个账号（多轮工具调用需要）。

## 3. 管理接口

| 接口 | 作用 |
| --- | --- |
| `GET /api/admin/gabai/status` | job + 号池统计 + worker 是否就位 |
| `POST /api/admin/gabai/register` | 起批量注册（`count`/`concurrency`/`mailProvider`/`mailDomains[]`/`captcha`/`proxies[]`/`createApiKey`） |
| `POST /api/admin/gabai/register/stop` | 停任务（Windows 整棵进程树） |
| `GET /api/admin/gabai/pool` | 号池脱敏视图（`apiKey` 只报前缀） |
| `POST /api/admin/gabai/pool/import` | worker 回推 / 手工入池（按 apiKey→email 幂等，补发网关 Key） |
| `POST /api/admin/gabai/pool/:id/mint` | **网页反代出 apikey**：用账号登录态在站点上 `POST /json/api-keys` 取/补一把 `gab_…` |
| `DELETE /api/admin/gabai/pool/:id` | 移除账号 |
| `POST/GET /api/admin/gabai/pool/export?format=jsonl\|csv\|pool[&download=1]` | 统一导出 |

## 4. 闭环位置

```
注册(tools/gabai-reg) ──NDJSON──▶ /api/admin/gabai/pool/import ──▶ data/gabai-pool.json
                                          │                              │
                                          └─ mintApiKey ──▶ gab.ai/json/api-keys
                                                                         │
下游 sk-dcg-… ──▶ /gab/v1/* ──▶ GabaiProxy.forward ──▶ gab.ai/v1/*  ◀────┘
```

自检：`node scripts/check-loop.mjs` 会把这条线的五环点一遍；
`node --test tests/gabai-routes.test.mjs` 覆盖管理面、入池幂等、导出与三条反代面的鉴权边界。
