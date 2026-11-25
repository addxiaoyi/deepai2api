# 闭环总览：一条线从"注册"走到"回款"

这份文档是仓库的**功能地图**：每条上游线在同一个五环闭环里各占一行，缺哪一环、
为什么缺、用什么验证，都在这里。读完它应该能回答三个问题：

1. 这个仓库到底有哪几条线？各自能干什么？
2. 每条线的凭据、文件、路由、Key 分别在哪？
3. 加一条新线要动哪几个文件，怎么证明它闭环了？

自检脚本：`npm run loop`（等价 `node scripts/check-loop.mjs`，静态检查，有缺口 exit 1）。

---

## 1. 五环定义

| 环 | 做什么 | 产物 | 失败时 |
| --- | --- | --- | --- |
| ① 注册 | 建号 / 建 Key / 过人机校验 | python worker 的 NDJSON `account` 事件 | 按 `kind` 分类重试或换号 |
| ② 入池 | 合并去重 + 发网关 Key | `data/<line>-pool.json`（`dcg.pool/<line>@1`） | 幂等：重推不会发两把 Key |
| ③ 出 Key | 把账号凭据兑成对外的 `sk-dcg-…` | `Store` 里存哈希，池里存明文 | 池里已有 Key 就复用 |
| ④ 反代 | 下游协议 → 上游协议 | `/v1/*`、`/p/*`、`/cleverai/v1/*`、`/gab/v1/*` | 分级冷却 + 换号，冷却中不参与轮询 |
| ⑤ 观测/回收 | 健康、额度、导出、停任务 | `/health`、`/api/admin/state`、导出文件 | job 可强杀，重启后状态可见 |

**③ 是这套东西的关键点**：下游永远只拿 `sk-dcg-…`，永远拿不到上游凭据。上游换号、
换 Key、换套餐，对下游完全透明。

---

## 2. 六条线

| 线 | 上游 | 凭据形态 | 号池文件 | worker | 反代前缀 |
| --- | --- | --- | --- | --- | --- |
| DeepAI Code | `api.deepai.org` | OAuth `api_key` | `gateway-state.json`（无独立池） | `tools/deepai-reg` | `/v1/*` |
| Allmodels | `dash.allmodels.io` | 面板 `sk_live_…` | `allmodels-pool.json` | `tools/allmodels-reg` | 无（工具层/MCP） |
| Poe | `poe.com` | `p-b` cookie | `poe-pool.json` | `tools/poe-reg` | 无（仅注册+池） |
| Provider 运行时 | wavespeed / novita / moonai / zo / blackbox | 各自的 key 或 OAuth | `<provider>-pool.json` | `tools/<provider>-reg` | `/p/<provider>/v1/*` |
| Clever AI Hub | `api.cleverai.app` | Firebase `refreshToken` + App Check JWT | `cleverai-pool.json` | `tools/cleverai-reg` | `/cleverai/v1/*` |
| Gab AI | `gab.ai` | 面板 `gab_…` key | `gabai-pool.json` | `tools/gabai-reg` | `/gab/v1/*` |

上游协议差异决定了翻译层厚度：

| 线 | 上游说什么 | 要不要翻译 | 翻译在哪 |
| --- | --- | --- | --- |
| DeepAI | OpenAI 形状 | 否（Anthropic 面另翻） | `src/anthropic.mjs` |
| Allmodels | 只有建 Key 的 REST | 不涉及（产物是 Key 本身） | — |
| Provider 运行时 | 各不同（moonai 无 stream/无 tools、zo 问答式…） | 是 | `src/provider-relay.mjs` + `<provider>-adapter.mjs` |
| Clever AI | 自有 `omni` 信封 + `tool_call` 内容项 | 是（双向） | `src/cleverai-relay.mjs` |
| Gab AI | OpenAI / Anthropic 都存在 | 否（直通） | — |

---

## 3. 契约（所有线必须一致的六件事）

1. **文件命名**：`data/<line>-pool.json`、`data/<line>-register-job.json`；
   worker 目录 `tools/<line>-reg/<line>_reg/`，入口 `cli.py` 或 `__main__.py`。
2. **schema**：池文件顶层带 `schema: "dcg.pool/<line>@1"` 与整数 `version`。
   有新字段必须可选，破坏性变更抬主版本。
3. **去重口径**：先按最强凭据（`apiKey` / `refreshToken` / `email`）匹配，命中就刷新
   凭据、保留 `id` / `createdAt` / 计数 / 已发的 `gatewayKey`。
4. **Key 只发一次**：`importAccount({ pool, store, record, mintKey })` 是唯一写入口，
   面板导入、worker 回推、CLI import 都走它。
5. **路由前缀不重叠**：`/v1/*`、`/p/<provider>/v1/*`、`/cleverai/v1/*`、`/gab/v1/*`、
   `/api/admin/<line>/*`、`/api/poe/*`。
6. **失败分类**：worker 用同一套 `kind`（`email_taken` / `rate_limited` / `blocked` /
   `captcha_required` / `verification_pending` / `no_api_key` / `transport`…），
   网关按 `kind` 决定重试、换号还是冷却。

---

## 4. 现状矩阵（`npm run loop` 的输出）

```
线               号池 schema                 worker   反代   管理   结论
deepai          （state 内）                 ✅       ✅     ✅     ✅
allmodels       dcg.pool/allmodels@1        ✅       —      ✅     ✅
poe             dcg.pool/poe                ✅       —      ✅     ✅
providers       dcg.pool/<id>@1             ✅       ✅     ✅     ✅
cleverai        dcg.pool/cleverai@1         ✅       ✅     ✅     ✅
gabai           dcg.pool/gabai@1            ✅       ✅     ✅     ✅
```

检查器点的是**六类证据**，不是口号：

| 证据 | 判据 |
| --- | --- |
| worker | `tools/<line>-reg/<line>_reg/` 下有 `cli.py` 或 `__main__.py` |
| worker 离线测试 | `tools/<line>-reg/tests/` 下至少一个 `.py` |
| 号池 | `src/<line>-pool.mjs` 存在且**含 `dcg.pool/<line>@1` 字符串** |
| 反代/管理路由 | 字面量或前缀出现在 `src/server.mjs` 里 |
| 文档 | `docs/<LINE>-POOL.md` 之类在 `LINES.docs` 里列了且文件存在 |
| 环境变量 | `.env.example` 里能查到该线的 `*_` 前缀 |
| 孤儿 | `tools/*-reg` 与 `src/*-pool.mjs` 必须全部登记，或在 `EXEMPT_WORKERS` 写明理由 |

「—」不是缺口，是**有意的设计决定**，见下一节。

---

## 5. 有意留空的环（以及为什么）

| 线 | 空的环 | 原因 |
| --- | --- | --- |
| Allmodels | 反代 | 它的产物就是面板 Key，下游直接拿去调 `api.allmodels.io/v1`；网关只负责批量产出 + 发 `sk-dcg-` Key + 工具桥（MCP）。再套一层反代只会多一跳。 |
| Poe | 反代 | 凭据是 `p-b` cookie，不能当 OpenAI 端点转发；用途是"注册出号"，消费方式由使用方决定（PoE 官方客户端 / 自家客户端）。 |
| DeepAI | 独立号池文件 | 账号是 OAuth 登录写入的，`gateway-state.json` 已经是它的池；再拆一份会分裂唯一写入口。 |

工具侧另有两个入口（不是反代面）：
`src/allmodels-mcp.mjs`（MCP over stdio）与 `src/provider-mcp.mjs`，
供 Claude Code / Cursor / Codex 直接发现。

---

## 6. 已声明豁免 / 已知边界

| 项 | 现状 | 处置 |
| --- | --- | --- |
| `tools/overchat-reg` | 只有 `errors.py` / `net.py` 骨架 + `_raw/` 逆向留档（`_raw/` 已在 `.gitignore`） | 登记在 `scripts/check-loop.mjs` 的 `EXEMPT_WORKERS`，理由写在 `tools/overchat-reg/README.md`；要变成第七条线按 §8 走 |
| Clever AI 的模型正文 | 上游是占位实现（见 `docs/CLEVERAI-POOL.md` 取证表） | 已用 `X-Cleverai-Notice` 打标；上游接真后代码不用改 |
| Zo 的 `/zo/ask` | 一次调用 = 一次完整 agent 运行，没有 cancel；没有原生 tools | 由 `relay.kind="zo-ask"` 合成 SSE + 提示词模拟工具；复杂多轮工具链建议用原生 provider |

"已声明豁免"和"未完成"是两件事：前者是**有意的**（有文档、有理由、检查器认账），
后者是**没人管**。`npm run loop` 只允许前者存在 —— 任何新出现的孤儿目录/号池模块
都会立刻被点出来，逼着你补登记或写明理由。


---

## 7. 怎么验证（三层 + 一条命令）

```bash
# 一条命令验完全部静态 + 接口 + worker + 回归
npm run test:all
#   = npm run loop && npm run test:loop && npm run test:workers
#     && node tests/e2e.mjs && npm run test:providers
```

分层看：

```bash
# 第一层：静态闭环（文件/路由/schema/文档/离线测试/环境变量/孤儿目录）
npm run loop

# 第二层：网关侧接口（不联网，mock 上游）
npm run test:loop        # cleverai 号池 + 双协议反代 + gabai 管理面/反代边界 + 面板 state
npm run test:allmodels   # 号池协议 + 并发合并 + MCP + e2e
npm run test:providers   # provider 运行时 + novita 池 + 两个 worker 离线协议
npm run test:moonai      # MoonAI 适配层 + 面板接线 + worker e2e

# 第三层：10 个 Python worker 的离线测试（197 项）
npm run test:workers

# 既有网关不回归
node tests/e2e.mjs       # 28 项
npm run smoke            # 真实上游冒烟（可选，会打真实通道）
```

`test:workers` 用 `--import-mode=importlib`，因为 10 个 worker 的测试文件同名叫
`test_offline.py`；不带这个参数 pytest 会因为模块名冲突整批报错。

---

## 8. 新增一条线的 checklist

一条线要算"闭环"，按顺序补齐这七样（缺哪样 `npm run loop` 会红）：

1. `tools/<line>-reg/<line>_reg/`：`config.py` / `protocol.py` / `runner.py` /
   `store.py` / `push.py` / `cli.py` / `__main__.py`，跑 `-m <line>_reg run --json`。
2. `src/<line>-pool.mjs`：`POOL_SCHEMA`、`normalizeAccount`、`importAccount`、
   `maskAccount`、`exportTo`。
3. `src/<line>-register.mjs`：spawn worker、读 NDJSON、一轮一 job、`taskkill /T /F`。
4. `src/<line>-relay.mjs`（或复用 `provider-relay`）：协议翻译 + 冷却 + 换号。
5. `src/server.mjs`：`/api/admin/<line>/*` 管理面 + `/<line>/v1/*` 反代面 +
   `/health` 与 `/api/admin/state` 里的统计块。
6. `docs/<LINE>-POOL.md`：schema、路由、失败分类、取证表。
7. `tests/<line>-*.test.mjs` + worker 的离线协议测试；再把这条线登记进
   `scripts/check-loop.mjs` 的 `LINES`。

最后跑 `npm run loop && npm run test:<line> && node tests/e2e.mjs`。
