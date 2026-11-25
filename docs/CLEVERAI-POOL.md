# Clever AI Hub 号池协议（`dcg.pool/cleverai@1`）

本文件定义网关里 **Clever AI Hub 号池** 的落盘格式、写入口径和读写 API。任何组件
（网关、注册 worker、外部脚本）只要按这里的字段生产/消费 `data/cleverai-pool.json`，
就能和现有代码互通。

这份号池与 `allmodels-pool.json`、`poe-pool.json`、`<provider>-pool.json` **完全独立**：
文件不同、schema 不同、路由前缀不同（`/cleverai/v1/*`、`/api/admin/cleverai/*`）。

- 文件：`$DEEPAI_GATEWAY_DATA/cleverai-pool.json`（默认 `data/cleverai-pool.json`）
- 权限：`0600`，原子替换写入（先写 `*.tmp` 再 rename）
- 内容：**明文凭据**（Firebase refreshToken、App Check JWT、网关 `sk-dcg-` Key）
- 版本：`schema` 带版本号；`version` 为整数，兼容旧读取方

## 1. 上游协议速查

Clever AI Hub 是两个后端拼起来的：

| 用途 | 地址 | 鉴权 |
| --- | --- | --- |
| 注册/登录 | `https://identitytoolkit.googleapis.com/v1/accounts:*` | 项目 web apiKey（前端公开） |
| 刷新令牌 | `https://securetoken.googleapis.com/v1/token` | `grant_type=refresh_token` |
| 业务面 | `https://api.cleverai.app/appserver/api/v1/*` | `Bearer <idToken>` + `x-functions-key` + `X-Firebase-AppCheck` |

三条硬性约束，缺一条就 401：

1. `x-functions-key`（Azure Functions host key，前端 bundle 里就有）；
2. `X-Firebase-AppCheck`：由 reCAPTCHA Enterprise(site key `6LcitAst…`) 换来的 JWT，
   有效期 3600 s，绑定的是**应用安装**而不是账号，所以整批账号可共用一枚；
3. `Version` / `X-Version`（`12.2.0`）——不带会被回一句 "please update the app"。

聊天走 `POST /appserver/api/v1/omni/response`，SSE 帧形如：

```
data: {"delta":{"content":[{"type":"text","text":"…"}]},"sequenceNumber":1}
data: {"finishReason":"stop","delta":{"content":[]},"sequenceNumber":2}
```

内容项类型：`text` / `thinking_text` / `image` / `video` / `audio` / `file` / `error` /
`metadata` / `tool_call` / `tool_result` / `mcp_app` / `connector_confirm`。
工具调用就是 `tool_call` / `tool_result` 这一对。

## 2. 顶层结构

```json
{
  "schema": "dcg.pool/cleverai@1",
  "version": 1,
  "provider": "cleverai",
  "baseUrl": "https://cleverai.app",
  "apiBase": "https://api.cleverai.app",
  "engine": "openai-gpt-6-luna",
  "updatedAt": "2026-11-26T02:10:00.000Z",
  "accounts": []
}
```

| 字段 | 说明 |
| --- | --- |
| `schema` | 固定 `dcg.pool/cleverai@1`，消费方应校验 |
| `baseUrl` | 站点地址（注册、取 App Check 令牌） |
| `apiBase` | appserver 地址，转发层用 |
| `engine` | 默认 chat engine id |
| `accounts[]` | 账号条目，插入顺序即轮询顺序 |

## 3. 账号条目

```json
{
  "id": "8f1c…",
  "email": "ada.lovelace4821@example.com",
  "password": "Xk9#mQ2vLp7!aB",
  "name": "Ada Lovelace",
  "uid": "Skk1CYcvJAU1OhBjeTBndax8unw1",
  "idToken": "eyJhbGciOi…",
  "refreshToken": "AMf-vBz…",
  "appCheck": "eyJraWQiOiJBNzFLSHci…",
  "appCheckExp": 1795613412.4,
  "wallet": { "totalCredit": 75, "renewableCredit": 30, "welcomeCredit": 45 },
  "device": { "deviceType": "DESKTOP", "deviceOs": "Windows", "deviceTimeZone": "Asia/Shanghai" },
  "emailVerified": true,
  "status": "active",
  "engine": "protocol",
  "createdAt": "2026-11-26T01:50:00.000Z",
  "updatedAt": "2026-11-26T01:50:04.000Z",
  "requests": 0,
  "failures": 0,
  "healthy": true,
  "cooldownUntil": 0,
  "lastError": null,
  "lastUsedAt": null,
  "gatewayKey": {
    "id": "k-3",
    "label": "cleverai:ada.lovelace4821@example.com",
    "prefix": "sk-dcg-AbCdEfGh",
    "plaintext": "sk-dcg-…",
    "createdAt": "2026-11-26T01:50:04.000Z"
  }
}
```

约定：

- `refreshToken` 是**长期凭据**，号池能不能续命全看它；`idToken` 只是缓存，过期由
  转发层自动刷新并回写。
- `appCheck` 会过期（`appCheckExp`），过期后由转发层调 worker 重新取；一枚令牌
  全池共用，`AppCheckVault` 提前 60 s 续期。
- `wallet` 上游每轮会回传新的额度快照，`recordWallet()` 合并进条目。
- `status`：`active` | `revoked` | `disabled`。
- 健康字段由调用结果驱动，`pick()` 只挑 `status=active && healthy && cooldownUntil<=now`。
- `engine`：`protocol`（纯 HTTP）| `browser`（指纹浏览器里跑完注册）。

## 4. 写入口径（谁写、怎么写）

| 入口 | 触发 | 行为 |
| --- | --- | --- |
| `POST /api/admin/cleverai/register` | 管理接口批量注册 | 起 python worker，逐条 `account` 事件入池 |
| `POST /api/admin/cleverai/pool/import` | worker 回推 / 手工导入 | 校验 `email`+`refreshToken`，`importAccount()` 合并 |
| `POST /api/admin/cleverai/pool/:id/health` | 转发层回报结果 | 计数 + 冷却 |
| `DELETE /api/admin/cleverai/pool/:id` | 管理 | 移除条目（不动已发的网关 Key） |
| `node src/cleverai-cli.mjs import file.jsonl` | 离线导入 | 同上，另补发网关 Key |

`importAccount()` 是唯一合并逻辑，幂等规则：

1. 先按 `refreshToken` 匹配，再按 `email` 匹配；
2. 命中则刷新凭据 / 钱包 / 设备信息，保留 `id`、`createdAt`、`requests`、`failures`、
   `gatewayKey`；
3. 未命中则新建；
4. `mintKey=true` 且条目没有 `gatewayKey` 时才 `Store.addKey()` —— worker 推送与
   事件流两条路同时到达也不会发两把 Key。

## 5. 读接口

```
GET  /api/admin/cleverai/status              # job + 号池 + relay 状态
GET  /api/admin/cleverai/pool?limit=200      # 脱敏视图
GET  /api/admin/cleverai/keys                # 每个账号的网关 Key 前缀 + 钱包
POST /api/admin/cleverai/appcheck            # 立刻续期一枚 App Check 令牌
POST /api/admin/cleverai/pool/export?format=jsonl|csv|pool[&download=1]
GET  /health                                 # cleverai.accounts / healthy / keys / credits
```

## 6. 反代面（出 Key）

```
GET  /cleverai/v1/models
POST /cleverai/v1/chat/completions     # OpenAI 形状，支持 stream 与 tools
GET  /cleverai/v1/usage
```

- 鉴权：`Authorization: Bearer sk-dcg-…`（任何网关 Key 都能用；真实上游账号由号池
  轮询决定，`X-Session-Affinity` 可粘住某个账号）。
- `tools` / `tool_calls` 双向翻译：OpenAI 的 `tools[]` → `options.tools[]`，
  上游 `tool_call` 内容项 → `choices[].delta.tool_calls`，`role:"tool"` → `tool_result`。
  所以 Cline / Continue / Roo / Claude Code（走 Anthropic 兼容层）能直接当 OpenAI 端点用。
- 失败策略：401 且带 `APP_CHECK` → 强刷令牌重试一次；401 其它 → 清 `idToken` 冷却
  30 s；429 → 冷却 5 min；`no credit` → 冷却 30 min；其余 60 s。冷却中的账号不参与轮询。

## 7. 批量注册 / 并发语义

- 一次只允许一轮 job（`POST /register` 在跑时返回 `409`），`POST /register/stop` 强杀
  `taskkill /T /F`。
- 防封四层（`tools/cleverai-reg/cleverai_reg/runner.py`）：

  | 维度 | 手段 |
  | --- | --- |
  | 出口 | `per_proxy_concurrency=1`，连续失败 3 次冷却 30 min |
  | 邮箱域名 | 每域连续成功 `per_domain_burst=5` 就换域，被打回冷却 15 min |
  | 指纹 | Camoufox 每账号一个全新上下文；`geoip` 让时区/语言跟随出口 |
  | 行为 | 注册后立刻预热一次对话，避免"注册完没动静"的静默号 |

- 失败分类进入 `failures` 表与 job 日志：`email_taken` / `email_rejected` /
  `password_rejected` / `rate_limited` / `blocked` / `app_check_required` /
  `captcha_required` / `verification_pending` / `no_credit` / `transport`。

## 8. 兼容性

新增字段必须可选；读取方遇到未知字段应忽略。破坏性变更提升 `schema` 主版本
（`dcg.pool/cleverai@2`），旧读取方按 `version` 决定是否降级处理。
