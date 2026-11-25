# MoonAI 号池协议（`dcg.pool/moonai@1`）

本文件定义 **mooonai.com（MoonAI）** 这条线在网关里的落盘格式、写入口径、反代行
为与注册协议。任何组件（网关、Python 注册机、脚本）按这里的字段生产/消费
`data/moonai-pool.json` 就能互通。

- 文件：`$DEEPAI_GATEWAY_DATA/moonai-pool.json`（默认 `data/moonai-pool.json`）
- 权限：`0600`，原子替换（写 `*.tmp` 再 rename）
- 内容：**明文凭据**（面板密码、`api_…` Key、OAuth access/refresh token），
  所以 `data/` 已在 `.gitignore` 里排除
- 隔离：与 `allmodels-pool.json` / `wavespeed-pool.json` / `novita-pool.json`
  **物理隔离**，三条线可以同时跑且互不覆盖

---

## 1. 为什么 MoonAI 和其它 provider 不一样

| 维度 | wavespeed / novita | **moonai** |
| --- | --- | --- |
| 调模型凭据 | 面板 API Key | **OAuth `access_token`**（`api_…` 不能调模型） |
| 上游协议 | OpenAI 兼容 | **自有形状**（`{content, finish_reason, usage}`） |
| 流式 | 上游支持 SSE | **不支持**，网关合成 |
| tools | 上游支持 | **不支持**（`extra_forbidden`），网关模拟 |
| 注册 | 表单 + 人机校验 | 表单 + **邮箱验证** + OAuth（无需人机校验） |
| 有效期 | Key 长期 | access_token 86400s + refresh_token |

`api_…` 是"Agent 渠道 Webhook Key"（`/api/v1/channels/api/messages`），
注册机仍然会签一把留档，但**反代层用的是 access_token**。

---

## 2. 顶层结构

```json
{
  "schema": "dcg.pool/moonai@1",
  "version": 1,
  "provider": "moonai",
  "panelBase": "https://auth.mooonai.com",
  "apiBase": "https://core.mooonai.com",
  "updatedAt": "2026-11-25T12:00:00.000Z",
  "accounts": [ /* 见下 */ ]
}
```

## 3. 账号条目

```json
{
  "id": "d3f1c0d2-…",
  "email": "ada.lovelace4821@example.com",
  "password": "Xkq…!7",
  "name": "Ada Lovelace",
  "apiKey": "api_…（注册时签发的渠道 Key，仅留档）",
  "keyId": "18",
  "keyPrefix": "api_BzNz",
  "keyLast4": "AZxO",
  "keySource": "org-api-keys",
  "organizationId": "5100",
  "accessToken": "eyJhbGciOi…",
  "refreshToken": "…",
  "accessTokenExpiresAt": 1795694400000,
  "emailVerified": true,
  "status": "active",
  "engine": "protocol",
  "domain": "example.com",
  "proxy": "http://user:pass@host:port",
  "cookies": [{ "name": "sessionid", "value": "…", "domain": "auth.mooonai.com" }],
  "requests": 0,
  "failures": 0,
  "healthy": true,
  "cooldownUntil": 0,
  "gatewayKey": {
    "id": "uuid",
    "label": "moonai:ada.lovelace4821@example.com",
    "prefix": "sk-dcg-AbCdEfGh",
    "plaintext": "sk-dcg-…",
    "createdAt": "2026-11-25T12:00:02.000Z"
  }
}
```

约定：

- `accessToken` 是**上游鉴权凭据**；`apiKey` 只是留档（渠道 Key）。
- `accessTokenExpiresAt` 是毫秒时间戳；`needsRefresh()` 在**剩余 10 分钟**时提前续期。
- `refreshToken` 用于续期；401/403 时反代层会续一次再重试。
- `gatewayKey.plaintext` 只存在这里 —— `Store` 里只保存 SHA-256 与前缀。
- `engine`：`protocol` | `browser:camoufox` | `browser:patchright` | `browser:playwright`。
- 健康字段由调用结果驱动，`pick()` 只挑 `status=active && healthy && cooldownUntil<=now`。
- **重新导入只带 `apiKey` 时，已有的 `accessToken` / `refreshToken` 不会被清空**
  （`ProviderPool.add()` 显式保令牌），这是"worker 重推"安全的前提。

---

## 4. 写入口径

| 入口 | 触发 | 行为 |
| --- | --- | --- |
| `POST /api/admin/providers/moonai/register` | 管理接口批量注册 | 起 python worker，逐条 `account` 事件入池 |
| `POST /api/admin/providers/moonai/pool/import` | worker 回推 / 手工导入 | 校验 `email`/`apiKey`/`accessToken`，`importAccount()` 合并 |
| `POST /api/admin/providers/moonai/pool/:id/health` | 转发层回报结果 | 计数 + 冷却 |
| `DELETE /api/admin/providers/moonai/pool/:id` | 管理 | 移除条目（不动已发的网关 Key） |
| `node src/moonai-cli.mjs import out/accounts.jsonl` | 离线导入 | 同上，并补发网关 Key |

`importAccount()` 是唯一合并逻辑，幂等规则：

1. 先按 `apiKey` 匹配，再按 `email` 匹配；
2. 命中则刷新凭据与组织信息，保留 `id` / `createdAt` / `requests` / `failures` / `gatewayKey`；
3. 未命中则新建，`id` 现生成；
4. `mintKey=true` 且该条目没有 `gatewayKey` 时才发新 Key —— 所以"worker 推送 +
   事件流"两条路同时到达也只会有一把 Key。

---

## 5. 反代：OpenAI 兼容面

```
GET  /p/moonai/v1/models                 6 个白名单模型
POST /p/moonai/v1/chat/completions       OpenAI 兼容（流式/非流式/工具调用）
POST /p/moonai/v1/completions            legacy 补全
POST /p/moonai/v1/responses              Responses 兼容
POST /p/moonai/v1/messages               Anthropic 兼容（含 SSE 事件序列）
```

鉴权两段：

1. 客户端侧：网关自己的 `sk-dcg-…`（与 `/v1/*` 同一把尺子，走同一份限额/计费）；
2. 上游侧：号池里轮询出来的 `access_token`（`Authorization: Bearer <access_token>`）。

失败自动换号：401/402/403/429/5xx 换下一个账号，最多 3 个；403 先尝试续期一次。

### 5.1 字段剥除（必须）

上游 pydantic 是 `extra_forbidden`，多一个字段就 422。所以请求体按白名单重建：

```
允许：model, messages, temperature, top_p, max_tokens
剥掉：stream, stream_options, tools, tool_choice, parallel_tool_calls, stop, n, user,
      frequency_penalty, presence_penalty, logit_bias, logprobs, top_logprobs,
      response_format, seed, metadata, store, modalities, audio, reasoning,
      reasoning_effort, service_tier, verbosity
```

`max_completion_tokens` 归一成 `max_tokens`；`tool` 角色的消息折成 `user`（附 `tool_call_id`）。

### 5.2 工具调用模拟

上游不接受 `tools`，所以：

1. 把 tools 编译成 system 提示里的 JSON 协议：
   `{"tool":"<name>","arguments":{...}}`（含每个工具的 JSON Schema 原文）；
2. 回程用 `parseToolCall()` 解析（裸 JSON / ```json 围栏 / 带前言都能吃）；
3. 成功则返回 OpenAI `tool_calls` + `finish_reason:"tool_calls"`，流式时按 `arguments`
   增量切片发出（带 `index`）。

这样 Cline / Roo / Continue / Aider 这类重工具调用的客户端才能工作。

### 5.3 流式合成

上游没有 `stream`，网关把整段 content 按 24 字符切片，发出：

```
data: {"choices":[{"delta":{"role":"assistant","content":""}}]}
data: {"choices":[{"delta":{"content":"…"}}]}   × N
data: {"choices":[{"delta":{},"finish_reason":"stop"}],"usage":{…}}
data: [DONE]
```

Anthropic 面则把这串喂给现成的 `streamAsAnthropic()`，输出
`message_start → content_block_* → message_delta → message_stop`。

---

## 6. 注册协议（线上实测，2026-11）

端点全部来自实测，不是猜测：

| 步骤 | 请求 | 观察 |
| --- | --- | --- |
| 1 | `GET auth.mooonai.com/auth/signup/` | Django 表单，`csrfmiddlewaretoken` + `csrftoken` cookie |
| 2 | `POST /auth/signup/` | `username` / `email` / `password1` / `agree_*` → `302 /auth/email-verification-sent/` |
| 3 | 邮件 | 主题 `Подтвердите ваш адрес электронной почты`，链接 `auth.mooonai.com/auth/verify-email/?token=<uuid>`（有时被 `*.mjt.lu` 包裹） |
| 4 | `GET /auth/verify-email/?token=…` | `302 dashboard?registration_success=true` |
| 5 | `POST /auth/signin/` | `username`(邮箱或用户名) + `password` → `sessionid` |
| 6 | `GET core.mooonai.com/api/v1/auth/url` | 返回 authentik `auth_url`；**默认 redirect_uri 是 `/oauth`** |
| 7 | `GET <auth_url>` | 带 `sessionid` → `302 dashboard.mooonai.com/oauth?code=…&state=…` |
| 8 | `POST /api/v1/auth/token` | form `auth_response_url=<完整回调>` → `{access_token, refresh_token, expires_in:86400, id_token}` |
| 9 | `POST /api/v1/auth/userinfo` | Bearer → `sub` / `preferred_username` / `email_verified` |
| 10 | `GET /api/v1/organizations/` | `{count, results[]}`；空则 `POST /api/v1/organizations {"name":…}` |
| 11 | `POST /api/v1/organizations/{org}/api-keys` | `{"name":…}` → `{id, name, key:"api_…", is_active}` |
| 12 | `POST /api/v1/services/llm-proxy/chat/completions` | Bearer access_token → `{content, finish_reason, model, usage}` |

踩过的坑（都写进了代码）：

| 现象 | 原因 | 处理 |
| --- | --- | --- |
| `400 invalid_request Mismatching redirect URI` | `auth/url?redirect_url=` 传什么就被 authentik 拒什么 | **不传** `redirect_url`，用默认 `/oauth` |
| `422 extra_forbidden: stream` | 上游请求模型禁止多字段 | 白名单重建请求体 |
| `403 This endpoint requires user authentication` | 用 `api_…` 当 Bearer | 换成 access_token |
| `400 Model 'x' is not available` | 模型白名单固定 6 个 | 非法模型回落到 `gpt-4o-mini` |
| 注册后直接登录失败 | 邮箱未验证 | 必须先点验证链接再登录 |

---

## 7. 防封策略

与 `src/provider-register.mjs` 的 `DEFAULT_ANTI_BAN` 同名字段，worker 侧
`antiban.py` 实现：

| 层 | 参数 | 行为 |
| --- | --- | --- |
| 域名 | `domainRpm` / `domainRph` / `domainHealthFloor` | 滑动窗口限速；成功 +1 / 失败 -2，健康分低于阈值停用该域 |
| 出口 | `ipConcurrency` / `proxyRotate` | 同一代理串行；失败计数决定轮换顺序 |
| 节奏 | `jitterMs` / `humanize` | 账号之间随机抖动；`humanize` 倍率放慢浏览器操作 |
| 会话 | `reuseSession` / `sessionTtlHours` | cookie jar 复用，避免重复过 OAuth |
| 退避 | `backoff[kind]` | `rate_limited` 20–45s、`blocked` 20–40s、`verification_pending` 5–10s… |
| 指纹 | `geoAlign` / `freshProfile` | 时区/语言跟随出口；每号全新 UA/视口/硬件并发 |

`--engine auto` 的回落：协议腿遇到 `blocked` / `captcha_required` / `no_token`
当场切指纹浏览器，同一个人、同一封邮箱重跑，`engine` 记成 `browser:<stack>`。

---

## 8. 测试

| 用例 | 命令 | 结果 |
| --- | --- | --- |
| 适配层 + 号池单测（含合并防丢号回归） | `node --test tests/moonai-pool.test.mjs` | 17/17 |
| Open WebUI 面板接线（静态） | `node --test tests/moonai-fork-surface.test.mjs` | 6/6 |
| Open WebUI 组件真实编译 | `npm run check:svelte`（需 `svelte@4 svelte-preprocess@5 typescript@5`） | 2/2 |
| 反代端到端（mock 上游） | `node tests/moonai-e2e.mjs` | 10/10 |
| 管理面 + 既有路由不冲突 | `node tests/moonai-admin.test.mjs` | 11/11 |
| 注册机 → 网关 → 反代全链路（含面板按钮路径） | `node tests/moonai-worker-e2e.mjs` | 11/11 |
| Python 协议离线 | `python -m unittest discover -s tools/moonai-reg/tests` | 6/6 |
| 既有网关回归 | `node tests/e2e.mjs` | 28/28 |

一键：`npm run test:moonai`。

---

## 8.1 Open WebUI 面板

`设置 → Admin → DeepAI` 页面底部挂了 `MoonAI 号池` 卡片
（`patches/files/src/lib/components/admin/Settings/MoonAI.svelte`），
通过 `patches/files/backend/open_webui/routers/deepai.py` 的
`/api/v1/deepai/providers/*` 透传到上面这些管理接口。

卡片提供：号池计数（账号 / 可用 / 网关 Key / job 状态）、python 与 worker 是否就绪、
注册表单（数量 / 并发 / 引擎 / 指纹内核 / 邮箱来源 / 域名 / 代理 / CDP / 放慢倍率 /
每号 Key 数 / 结束后导出）、开始与停止、进度条 + 失败原因分组 + 30 行日志、
**手工导入**（一行一个账号 JSON 或一个数组，可选同时发网关 Key，新 Key 只显示一次）、
号池表（面板 Key 预览、令牌到期时间、网关 Key 前缀、引擎、调用计数、移除）、
导出文件，以及可复制的 **OpenAI Base URL**（`/p/moonai/v1`）与
**Anthropic Base**（`/p/moonai`）。

面板不返回也不显示任何明文凭据：`access_token` / `refresh_token` 只在网关的
号池文件里，视图里只有"有没有 / 什么时候过期"。

> worker 的 sqlite 现在落在 `data/moonai-reg/`（由 `provider-register.mjs`
> 传 `--out-dir`），与号池文件同源，便于一起备份；也避免不同 dataDir 的
> 两套网关共用一份去重库。

---

## 9. 兼容性

新增字段必须可选；读取方遇到未知字段应忽略。破坏性变更提升 `schema` 主版本
（`dcg.pool/moonai@2`）。`api_…` 与 `access_token` 的语义差异属于 v1 约定，
不会在 v1 内改变。
