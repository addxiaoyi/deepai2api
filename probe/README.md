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
## cleverai（Clever AI Hub 协议取证）

`probe/cleverai/` 是还原 `cleverai.app` 注册链路的验证用例，结论已固化进
`tools/cleverai-reg`：

| 文件 | 用途 |
| --- | --- |
| `appcheck_probe*.py` | 找 App Check 令牌的取法：`about:blank` 上 reCAPTCHA 不可用，必须站内源；最终用 `page.on("response")` 读 SPA 自己换的那枚 |
| `chain_probe*.py` | 注册 → 建号 → 聊天的完整链路（含代理差异：浏览器走系统代理、httpx 必须显式 `proxy=`） |
| `engines_probe.py` | `/configs?type=engines` 列出 94 个 engine（id/type/cost） |
| `version_probe.py` | 证明缺 `Version/X-Version` 时后端回 "please update the app" 假回答 |
| `spa_shape_probe.py` / `headers_probe.py` | 还原 `omni/response` 的请求体与请求头 |
