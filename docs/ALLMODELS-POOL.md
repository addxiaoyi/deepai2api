# Allmodels 号池协议（`dcg.pool/allmodels@1`）

本文件定义网关里 **Allmodels 号池** 的落盘格式、写入口径和读写 API。任何组件
（网关、注册 worker、外部脚本）只要按这里的字段生产/消费 `data/allmodels-pool.json`，
就能和现有代码互通。

- 文件：`$DEEPAI_GATEWAY_DATA/allmodels-pool.json`（默认 `data/allmodels-pool.json`）
- 权限：`0600`，原子替换写入（先写 `*.tmp` 再 rename），崩溃不会留下半个文件
- 内容：**明文凭据**（面板密码、面板 API Key、网关 `sk-dcg-` Key），因此 `data/`
  已被 `.gitignore` 排除
- 版本：`schema` 字段带版本号；`version` 为整数，兼容旧读取方

## 1. 顶层结构

```json
{
  "schema": "dcg.pool/allmodels@1",
  "version": 1,
  "provider": "allmodels",
  "baseUrl": "https://dash.allmodels.io",
  "apiBase": "https://api.allmodels.io/v1",
  "updatedAt": "2026-11-25T11:07:55.000Z",
  "accounts": [ /* 见下 */ ],
  "tombstones": [{ "id": "d3f1c0d2-…", "at": 1795560000000 }]
}
```

| 字段 | 说明 |
| --- | --- |
| `schema` | 固定 `dcg.pool/allmodels@1`，消费方应校验 |
| `baseUrl` | 面板地址（注册、登录、建 key 的 host） |
| `apiBase` | 真正调用模型的上游地址，供转发层使用 |
| `updatedAt` | 最后一次落盘时间（ISO8601） |
| `accounts[]` | 账号条目，插入顺序即轮询顺序 |
| `tombstones[]` | 删除记录（id + 毫秒时间），保留 30 天 / 最多 500 条；多进程下它让"删除"不会被旧内存复活 |

## 2. 账号条目

```json
{
  "id": "d3f1c0d2-…",
  "email": "ada.lovelace4821@example.com",
  "password": "Xkq…!7",
  "name": "Ada Lovelace",
  "apiKey": "sk_live_…",
  "keyId": "uuid",
  "keyPrefix": "sk_live_",
  "keyLast4": "f3a9",
  "keySource": "signup-key",
  "extraKeys": [{ "key": "sk_live_…", "source": "created" }],
  "projectId": "uuid",
  "guardrailId": "uuid",
  "organizationId": "uuid",
  "emailVerified": true,
  "status": "active",
  "engine": "protocol",
  "createdAt": "2026-11-25T10:00:00.000Z",
  "updatedAt": "2026-11-25T10:00:02.000Z",
  "requests": 0,
  "failures": 0,
  "healthy": true,
  "cooldownUntil": 0,
  "lastError": null,
  "lastUsedAt": null,
  "cookies": [{ "name": "better-auth.session_token", "value": "…", "domain": "dash.allmodels.io", "path": "/" }],
  "gatewayKey": {
    "id": "uuid",
    "label": "allmodels:ada.lovelace4821@example.com",
    "prefix": "sk-dcg-AbCdEfGh",
    "plaintext": "sk-dcg-…",
    "createdAt": "2026-11-25T10:00:02.000Z"
  }
}
```

约定：

- `apiKey` 是**面板**签发的 Key（`sk_live_…`），也是号池最终要用的凭据；
  `keySource` 记录来源：`signup-key`（注册时自动下发）/ `created`（`POST /api/keys` 补建）。
- `extraKeys[]` 是同一账号下额外创建的 Key（`--keys-per-account`），不参与默认轮询。
- `gatewayKey.plaintext` 只存在这里 —— `Store` 里仅保存 SHA-256 与前缀，明文仅此一份。
- `status`：`active` | `revoked` | `disabled`。
- 健康字段（`healthy` / `cooldownUntil` / `lastError` / `requests` / `failures`）由
  调用结果驱动，`pick()` 只挑 `status=active && healthy && cooldownUntil<=now` 的账号。
- `cookies[]` 让后续「登录态复用」不需要重新过 Turnstile。

## 3. 写入口径（谁写、怎么写）

| 入口 | 触发 | 行为 |
| --- | --- | --- |
| `POST /api/admin/allmodels/register` | 管理接口批量注册 | 起 python worker，逐条 `account` 事件入池 |
| `POST /api/admin/allmodels/pool/import` | worker 回推 / 手工导入 | 校验 `apiKey`/`email`，`importAccount()` 合并 |
| `POST /api/admin/allmodels/pool/:id/health` | 转发层回报结果 | 计数 + 冷却 |
| `DELETE /api/admin/allmodels/pool/:id` | 管理 | 移除条目（不动已发的网关 Key） |
| `node src/allmodels-cli.mjs import file.jsonl` | 离线导入 | 同上，另补发网关 Key |

`importAccount()` 是唯一合并逻辑，幂等规则：

1. 先按 `apiKey` 匹配，再按 `email` 匹配；
2. 命中则**刷新凭据与项目信息**，保留 `id`、`createdAt`、`requests`、`failures`、
   `gatewayKey`；
3. 未命中则新建，`id` 现生成；
4. `mintKey=true` 且该条目没有 `gatewayKey` 时才调用 `Store.addKey()` 发新 Key ——
   所以 worker 推送 + 事件流两条路同时到达也不会发两把 Key。

## 4. 读接口

```
GET  /api/admin/allmodels/pool?limit=200   # 脱敏视图（apiKeyPreview / gatewayKey.prefix）
GET  /api/admin/allmodels/keys             # 每个账号的面板 Key 元信息 + 网关 Key 前缀
GET  /api/admin/allmodels/status           # 当前/最近一轮 job + 号池统计
GET  /health                               # allmodels.accounts / healthy / keys
POST /api/admin/allmodels/pool/export?format=jsonl|csv|pool[&download=1]

# 工具桥（与 MCP 同一份 schema，见 docs/ALLMODELS-MCP.md）
GET  /api/admin/allmodels/tools            # 工具清单 + OpenAI function-calling 定义
POST /api/admin/allmodels/tools/:name      # {"arguments": {...}} 调用一个工具
```

导出格式（三选一，均含明文，写到 `data/exports/`）：

- `jsonl`：一行一个账号条目（与落盘条目同构）
- `csv`：`email,password,api_key,key_prefix,project_id,guardrail_id,gateway_key,status,created_at`
- `pool`：完整顶层结构（可直接替换号池文件或作为备份）

## 5. 批量注册 / 并发语义

- 一次只允许一轮 job（`POST /register` 在跑时返回 `409`），`POST /register/stop` 可强杀
  `taskkill /T /F`。
- 并发由 worker 的 `ThreadPoolExecutor` 承担，`concurrency` 建议 ≤ 代理数量；同一出口 IP
  并发过高会直接吃 Turnstile 失败。
- 每个账号的所有权：面板 `POST /api/auth/sign-up/email`（`x-turnstile-token`）→ 邮箱验证链接
  → `GET /api/me/overview` → `GET /api/me/signup-key`（或 `POST /api/keys`），细节见
  `tools/allmodels-reg/README.md`。
- 双引擎：`engine=protocol`（纯 HTTP）、`engine=browser`（指纹浏览器：camoufox / patchright /
  playwright / 外部 CDP）、`engine=auto`（协议优先，被 Turnstile 拦住自动切浏览器）。
  账号条目里的 `engine` 记录实际生效的那条腿。
- 失败分类进入 `failures` 表与 job 日志：`email_taken` / `email_rejected` /
  `captcha_required` / `verification_pending` / `rate_limited` / `blocked` / `no_api_key`。

## 6. 兼容性

新增字段必须可选；读取方遇到未知字段应忽略。破坏性变更提升 `schema` 主版本
（`dcg.pool/allmodels@2`），旧读取方按 `version` 决定是否降级处理。

## 7. 多进程共存（网关 / MCP / CLI 互不冲突）

同一份 `data/` 可能被三个进程同时使用：网关（`src/index.mjs`）、MCP server
（`src/allmodels-mcp.mjs`）、CLI（`src/allmodels-cli.mjs`）。各自独立进程、独立内存，
靠下面三条规则避免互相踩：

### 7.1 命名空间隔离

| 资源 | 命名 | 归属 |
| --- | --- | --- |
| 号池文件 | `data/allmodels-pool.json` | 只用 `dcg.pool/allmodels@1`，与 `gateway-state.json` 完全不同文件 |
| job 状态 | `data/allmodels-register-job.json` | 只描述 Allmodels 注册任务 |
| 任务锁 | `data/allmodels-register.lock` | 跨进程互斥（见 7.2） |
| 写锁 | `<目标文件>.lock` | 毫秒级写窗口，`O_EXCL` |
| 导出 | `data/exports/allmodels-accounts-*.{jsonl,csv,json}` | 不覆盖别的导出 |
| 管理路由 | `/api/admin/allmodels/*` | 不与 `/api/admin/register*`、`/api/admin/keys` 重叠 |
| 环境变量 | `ALLMODELS_*` | 不与 `DEEPAI_*` 共用 |
| MCP server / 工具 | `allmodels` / `allmodels_*` | 工具名全局唯一 |
| python 产物 | `tools/allmodels-reg/out/` | 已 gitignore |

对既有模块的唯一改动是**增量**的：`Store` 增加跨进程合并（下一节），
`/v1/*`、DeepAI 账号池、OAuth 流程、Open WebUI 补丁都没有被改写。

### 7.2 任务互斥（一轮一个）

`AllmodelsRegistrations.start()` 先抢 `data/allmodels-register.lock`（写 pid + 时间），
抢不到返回 `409`（"另一个进程正在跑"）。锁的回收条件：持有进程已不存在，或锁龄 > 6 小时。
`finish` / `stop` / 进程退出都会释放。这样网关面板、MCP Agent、CLI 不会同时拉起两个
python worker（否则会重复注册同一批地址、互相覆盖 job 进度）。

### 7.3 写入合并（不丢更新）

两个文件都用「锁 + 内容指纹 + 合并」写入：

1. `withFileLock()` 拿 `O_EXCL` 写锁（5s 超时，20ms 轮询）；
2. 比较文件 SHA-1 与"我上次写出去的内容"是否一致；不一致说明别人写过 →
   先合并再写（不依赖 mtime，规避 Windows/POSIX 时间戳精度问题）；
3. 合并规则：
   - 账号/Key 结构取并集，`requests`/`failures`/token 计数取 **max**；
   - `healthy`/`lastError`/`cooldownUntil` 取 `updatedAt`/`lastUsedAt` 更新的一方，冷却取更长；
   - `revoked` 一旦为真不会回退；
   - 已发出的 `gatewayKey` 永远不被覆盖（MCP 发的 Key 不会被网关冲掉，反之亦然）；
   - **所有权集合**：本进程创建/更新过的 id 记在内存里（`ownedIds` / `ownedAccounts`），
     它们即使暂时从磁盘消失（被另一个基于旧快照的写者覆盖）也会在下一次合并时找回；
     不属于自己的、又只在本地存在的条目，视为"别人删了"，不再写回；
   - **删除靠墓碑**：`remove()` 往文件里写 `tombstones`，任何进程合并时都会据此过滤，
     所以删除是终态，不会被旧内存复活；
   - 同邮箱 / 同 `apiKey` 的并发重复条目收敛成一条（取更新的一方，计数合并）。
4. 写失败（锁忙）不会丢数据：`dirty` 保持为真，下次 `save()` 再来。

删除语义：谁删谁写墓碑；另一个持有旧内存的进程下次写盘时，墓碑仍在文件里，条目不会复活。
**推荐在网关侧做删除**（MCP/CLI 也支持）。

### 7.4 并发读

读取不做加锁（原子 rename 保证读到的是完整 JSON）。`Store` 只在启动时读一次，
之后靠 7.3 在写盘时把别人的改动并进来；`AllmodelsPool` 每次 `save()` 也做同样合并，
所以面板上看到的号池最多滞后一次写窗口（≤ 750ms 的 Store 批量写延迟）。
