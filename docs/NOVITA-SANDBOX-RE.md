# Novita 客户端逆向：`novita-sandbox-cli@2.1.4`

> 目的：把「注册机产出的 Key 能不能在编程工具里被官方客户端用起来」这件事验证清楚，
> 并留一个**离线模拟运行环境**，让调用链不依赖线上沙箱也能跑。
>
> 取证对象：`npm pack novita-sandbox-cli@2.1.4` → `dist/index.js`（3.2 MB，tsup 打包，
> 未混淆，只做了压缩）。Python SDK `novita_sandbox@2.1.4` 同源。

## 1. 它是干什么的

`novita` / `novita-sandbox-cli` 两个 bin 指向同一个 `dist/index.js`：管理 Novita 的
**Agent Sandbox**（云端常驻 Linux 容器）——建沙箱、跑命令、传文件、PTY、模板构建、
快照、卷、配额。它不是模型推理客户端；模型推理走 `api.novita.ai/openai/v1/*`。

## 2. 鉴权（从代码里读出来的）

```js
// 控制面请求头（两条腿，二选一）
apiKey      → { "X-API-KEY": apiKey }
accessToken → { Authorization: `Bearer ${accessToken}` }
```

| 环境变量 | 作用 |
| --- | --- |
| `NOVITA_API_KEY` | 沙箱控制面 Key（`X-API-KEY`） |
| `NOVITA_ACCESS_TOKEN` | 等价凭据（`Authorization: Bearer`），从 dashboard 拿 |
| `NOVITA_API_URL` | 控制面基址；缺省由 `NOVITA_DOMAIN` 推 |
| `NOVITA_DOMAIN` | 默认 `us-phx-1.sandbox.novita.ai` |
| `NOVITA_SANDBOX_URL` | 沙箱数据面基址 |
| `NOVITA_DASHBOARD_BASE` / `NOVITA_DOCS_BASE` | 深链用 |
| `NOVITA_TEAM_ID` | 多团队 |
| `NOVITA_DEBUG` | `true` 打详细日志 |

持久化配置：`~/.novita/config.json`（读 `apiKey` / `accessToken` / `domain`）。
代理不走环境变量，走 SDK 的 `dispatcher`（`undici` ProxyAgent）。

## 3. 端点（从代码里的模板字符串还原）

| 方法 | 路径 | 说明 |
| --- | --- | --- |
| GET | `/sandboxes` | 列沙箱 |
| POST | `/sandboxes` | 建沙箱（`templateID`、`timeout`、`metadata`、`envs`） |
| GET | `/sandboxes/{id}` | 详情 |
| DELETE | `/sandboxes/{id}` | 销毁 |
| POST | `/sandboxes/{id}/timeout` | 续期 |
| POST | `/sandboxes/{id}/commands` | 跑命令（流式 stdout/stderr） |
| POST | `/sandboxes/{id}/processes` | 后台进程 |
| GET | `/sandboxes/quota` | 配额（并发数 / vCPU / 内存） |
| GET/POST | `/v2/templates` | 模板列表 / 构建 |
| GET/PATCH | `/volumes/{id}` | 卷与配额 |
| GET | `https://docker.${domain}/v2/token` | 私有镜像仓库 token |

## 4. 模拟运行环境（`tools/novita-reg/sandbox-sim.mjs`）

实现了上表里最常用的那几条，鉴权口径与官方一致：

```powershell
# 1) 起模拟器
node tools/novita-reg/sandbox-sim.mjs --port 8790

# 2) 把官方 CLI 指过来（离线跑通调用链）
$env:NOVITA_API_KEY = "sk-sim"
$env:NOVITA_API_URL = "http://127.0.0.1:8790"
npx novita-sandbox-cli sandbox list
npx novita-sandbox-cli sandbox create --template tpl-base
```

设计取舍：`/sandboxes/{id}/commands` **只回显不执行**（返回 `mocked:true`）。
模拟器存在的意义是验证「鉴权 / 路径 / 请求体 / 解析」这一层，而不是假装能跑代码——
把假执行做得像真的，反而会在排查真实环境问题时误导人。

验证记录：

```
GET  /            → 200 {"mock":"novita-sandbox",...}
GET  /sandboxes/quota → 200 {"limit":{"concurrentSandboxes":5,...}}
POST /sandboxes   → 200 {"sandboxID":"sbx-90096f20","status":"running",...}
POST /sandboxes/sbx-90096f20/commands → 200 {"exitCode":0,"mocked":true}
```

## 5. 和注册机 / 反代的关系

| 产物 | 用途 |
| --- | --- |
| 注册机拿到的 `sk_…` | 调 `api.novita.ai`（模型推理）**和**沙箱控制面都能用；沙箱用的是 `X-API-KEY` |
| 网关 `sk-dcg-…` | 只用于 `/p/novita/v1/*`，不暴露面板 Key |
| 模拟器 | 验证客户端调用链；真 key 与假 key 的行为差异一眼可见 |

结论：注册机产出的 Key 与官方客户端是同一套凭据体系，所以「注册 → 入池 → 反代出
网关 Key → 编程工具调用」这条链是闭合的；沙箱能力另有 `tools/novita-reg/sandbox-sim.mjs`
可在离线环境验证。
