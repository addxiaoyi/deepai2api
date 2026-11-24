# 探针脚本（逆向过程留档）

这些脚本是逆向 `DeepAI Code` 托管通道时的验证用例，不是网关运行时的一部分。

| 文件 | 用途 |
| --- | --- |
| `probe.mjs` / `probe.ps1` / `probe2.ps1` | 第一次打通 `/theos`、`/models`、`/chat/completions`，确认请求头组合 |
| `stream.mjs` | 确认 `stream:true` 走 SSE，且可以经 `127.0.0.1:10808` 代理 |
| `agents.mjs` / `agents2.mjs` | 对比 umdici dispatcher：定位到 `EnvHttpProxyAgent({connectTimeout})` 会破坏代理 |
| `debug.mjs` | 确认「请求体没有 stream 字段时上游默认流式」这一行为 |
| `debug2.mjs` | 在网关内层直接打印 502 响应体 |
| `responses.mjs` | 确认 `/responses` 端点只对 `api: "openai-responses"` 的模型开放 |

运行前提：桌面端已登录，`~\.deepai-code\userdata\secrets\deepai-account.bin` 存在。

```powershell
node probe\agents2.mjs     # 需要先 npm install（undici）
node probe\debug.mjs
```