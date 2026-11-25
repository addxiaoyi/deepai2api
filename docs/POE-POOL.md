# Poe 号池协议（`dcg.pool/poe@1`）

Poe 是"只注册、不反代"的一条线：凭据是 **cookie jar**（`p-b` / `cf_clearance`），
不是 API key，所以没有"出 Key → 反代"这两环。它存在的意义是把 `email_verified →
phone_required → active` 这个生命周期管起来，让上层自己决定怎么用。

- 文件：`$DEEPAI_GATEWAY_DATA/poe-pool.json`（默认 `data/poe-pool.json`）
- 写入：500 ms 防抖，`flush()` 落盘（进程退出前记得调一次）
- 凭据：`cookies{}` 是 cookie 名 → 值；对外视图**只报 cookie 名，不报值**
- worker：`tools/poe-reg/poe_reg`（`-m poe_reg`）
- job 状态：`data/poe-register-job.json`
- 路由：`/api/poe/*`（仅管理面）

## 1. 顶层结构

```json
{
  "schema": "dcg.pool/poe@1",
  "version": 1,
  "accounts": []
}
```

老文件没有 `schema` 也能读：`load()` 会补上前缀字段，下次 `flush()` 落盘。

## 2. 账号条目

```json
{
  "id": "5c1e…",
  "email": "ada@example.com",
  "status": "active",
  "engine": "protocol",
  "phone": "+1555…",
  "smsProvider": "sms-activate",
  "cookies": { "p-b": "…", "cf_clearance": "…" },
  "note": "",
  "createdAt": "2026-11-26T04:00:00.000Z",
  "updatedAt": "2026-11-26T04:01:10.000Z"
}
```

`status` 只有四个值，且由上游状态机驱动：

| 状态 | 含义 | 下一步 |
| --- | --- | --- |
| `email_verified` | 邮箱已验，还没绑手机 | 上接码平台过 `phone_required` |
| `phone_required` | 上游要求绑手机号 | 用 `smsProvider` 接码 |
| `active` | 可以正常用 | 交给消费方 |
| `dead` | 封号 / cookie 失效 | 移出池 |

## 3. 写入与读取

| 入口 | 触发 | 行为 |
| --- | --- | --- |
| `POST /api/poe/accounts` | worker 注册完自己回推，或手工补登 | `upsert()`：按 `email` 合并，保留 `id` / `createdAt` |
| `POST /api/poe/accounts/status` | 状态推进 | `mark(email, status)`，非法状态返回 404 |
| `DELETE /api/poe/accounts/:id` | 回收 | 移除条目 |
| `GET /api/poe/accounts?limit=100` | 列表 | `PoePool.public()` 脱敏（只报 cookie 名） |
| `GET /api/poe/health` | 概览 | `counts()`：四个状态各几个 |
| `GET/POST /api/poe/register[/stop]` | 批量注册 | 与其它线同一套控制器（`src/register.mjs` + `tools/poe-reg`） |

## 4. 为什么没有反代面

OpenAI 客户端要的是 `Authorization: Bearer <key>`，Poe 给的是浏览器 cookie。
把 cookie 塞进 Bearer 既跑不通也不安全（cookie 等价于账号全权）。所以这条线止步于
"注册出可用账号"，消费方式由使用方决定（PoE 官方客户端、自家客户端、或后续单独做
一层 cookie 会话代理）。

这也是 `scripts/check-loop.mjs` 里 Poe 的「反代」列显示 `—` 的原因：**有意留空**，
不是没做完。

## 5. 验证

```bash
# 闭环自检（Poe 这一行应当全 ✅，反代列是 —）
npm run loop

# 注册机自身的离线协议测试
python -m unittest discover -s tools/poe-reg/tests
```
