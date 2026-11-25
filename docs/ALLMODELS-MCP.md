# 在编程工具里调用 Allmodels 号池（MCP / HTTP / CLI）

三种入口共用同一个 handler 与同一份 JSON Schema（[`src/allmodels-tools.mjs`](../src/allmodels-tools.mjs)），
不会出现"文档一套、实现一套"。

| 入口 | 适合 | 传输 |
| --- | --- | --- |
| `src/allmodels-mcp.mjs` | Claude Code、Cursor、Codex CLI、VS Code Copilot、任何 MCP 客户端 | stdio 上的行分隔 JSON-RPC 2.0 |
| `POST /api/admin/allmodels/tools/:name` | Open WebUI 工具插件、n8n、Dify、自定义工作流 | HTTP + 管理员 token |
| `src/allmodels-cli.mjs` | 人、shell 脚本、CI | CLI |

## 工具清单

| 工具 | 作用 | 关键参数 |
| --- | --- | --- |
| `allmodels_status` | 号池统计 + 当前/最近一轮 job、失败分类、最近 40 行日志 | — |
| `allmodels_register` | 批量注册并自动建网关 Key、写号池 | `count`、`concurrency`、`engine`、`browserStack`、`cdp`、`mailProvider`、`email`、`captcha`、`captchaKey`、`keysPerAccount`、`export`、`wait`、`timeoutMs` |
| `allmodels_pool` | 列账号（Key 脱敏） | `limit`、`status`、`email`、`healthyOnly` |
| `allmodels_export` | 导出账号 + 面板 Key + 网关 Key | `format`(jsonl/csv/pool)、`file` |
| `allmodels_import` | 外部账号并入号池（按 `apiKey` 幂等，缺失时补发 Key） | `email`、`apiKey`、`projectId`、`guardrailId` … |
| `allmodels_keys` | 每个账号的面板 Key 元信息 + 网关 Key 前缀 | — |
| `allmodels_mark_health` | 回报一次调用结果（计数 + 冷却） | `id`/`email`、`ok`、`error`、`cooldownMs` |
| `allmodels_remove` | 从号池移除 | `id`/`email` |
| `allmodels_probe` | 面板可达性自检（不注册、不耗额度） | `baseUrl` |

`allmodels_register` 默认**立即返回** jobId，由 Agent 轮询 `allmodels_status`；
要一次等到结束就传 `{"wait": true, "timeoutMs": 180000}`。

## Claude Code

项目已带 [`.mcp.json`](../.mcp.json)，在该目录启动 Claude Code 即会自动发现 `allmodels` 服务器。
也可以用命令注册（全局）：

```bash
claude mcp add allmodels -- node /绝对路径/deepaiapi/src/allmodels-mcp.mjs
claude mcp list
```

## Cursor

`.cursor/mcp.json`（项目级）或 `~/.cursor/mcp.json`（全局）：

```json
{
  "mcpServers": {
    "allmodels": { "command": "node", "args": ["src/allmodels-mcp.mjs"] }
  }
}
```

## Codex CLI

`~/.codex/config.toml`：

```toml
[mcp_servers.allmodels]
command = "node"
args = ["D:/qwq/项目/deepaiapi/src/allmodels-mcp.mjs"]
# 需要时再补
# env = { ALLMODELS_BASE_URL = "https://dash.allmodels.io" }
```

## VS Code（Copilot Agent 模式）

`.vscode/mcp.json`：

```json
{
  "servers": {
    "allmodels": { "type": "stdio", "command": "node", "args": ["src/allmodels-mcp.mjs"] }
  }
}
```

## 其他工具 / 自己写插件：HTTP 桥

先起网关（`node src/index.mjs --token <口令>`），然后：

```bash
# 列工具（同时给 OpenAI function-calling 格式）
curl -H "Authorization: Bearer $TOKEN" http://127.0.0.1:8787/api/admin/allmodels/tools

# 调工具
curl -X POST -H "Authorization: Bearer $TOKEN" -H "Content-Type: application/json" \
  -d '{"arguments":{"count":3,"engine":"auto","captcha":"capsolver","captchaKey":"KEY","export":"jsonl"}}' \
  http://127.0.0.1:8787/api/admin/allmodels/tools/allmodels_register

# 轮询
curl -X POST -H "Authorization: Bearer $TOKEN" -d '{"arguments":{}}' \
  http://127.0.0.1:8787/api/admin/allmodels/tools/allmodels_status
```

OpenAI 兼容的 function 定义直接用返回里的 `openai` 字段（`{"type":"function","function":{...}}`），
不需要再手写 schema。

## 直接手测 MCP

```bash
node src/allmodels-mcp.mjs --print-tools        # 打印 tools/list 与 openai schema
printf '%s\n' '{"jsonrpc":"2.0","id":1,"method":"initialize","params":{"protocolVersion":"2025-06-18","capabilities":{},"clientInfo":{"name":"cli","version":"0"}}}' \
  '{"jsonrpc":"2.0","method":"notifications/initialized"}' \
  '{"jsonrpc":"2.0","id":2,"method":"tools/list"}' | node src/allmodels-mcp.mjs
```

## 设计约束

- MCP 的 stdout 只走协议，日志全部 stderr，避免污染 JSON-RPC。
- 工具读写的号池与网关是同一份 `data/allmodels-pool.json`，因此 Agent 注册的账号立刻能被网关/导出复用。
- 注册工具的内部 push 关闭（`push:false`）：worker 的 NDJSON 事件已经由控制器入池，避免同一账号走两遍 HTTP。
- 错误统一成 `{ok:false, code, error}`，`tools/call` 里带 `isError:true`，Agent 不会把失败当成功。
