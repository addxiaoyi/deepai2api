# Zo Computer 号池（`dcg.pool/zo@1`）

Zo 是 provider 运行时里形态最特殊的一个：**上游没有 OpenAI 形状的对话端点**，
只有一个 `POST /zo/ask`，返回的是 Zo agent 的整段输出（工具调用在 Zo 侧已经跑完），
流式还是自有 SSE 事件。所以它靠 `src/zo-adapter.mjs` + `relay.kind = "zo-ask"` 翻译，
不是直通。

- 号池：`data/zo-pool.json`（`dcg.pool/zo@1`，由 `src/provider-pool.mjs` 统一管）
- worker：`tools/zo-reg/zo_reg`（`-m zo_reg`），入口 `cli.py` / `__main__.py`
- 文档里的端点表就是它的事实来源：`src/provider-catalog.mjs` 的 `id: "zo"` 段

## 1. 注册链路（魔法链，不是密码）

```
① POST /api/email-login/request  {email}        → 发魔法链邮件
② 邮件里点 /email-login/verify?token=…          → 换到 confirm 页
③ POST /api/email-login/confirm {token, turnstileToken}
                                                → 下发会话 cookie
④ GET  /signup/{handle}/available               → 用户名查重
⑤ POST /signup/                                 → 建工作区
⑥ POST /api-keys/                               → 建 `zo_sk_…` token（对话凭据）
```

要点：

- **一次魔法链只对应一个邮箱**，`confirm` 必须在收到信之后立刻做（`runner.py` 里
  "定邮箱 → 发链 → 等信 → 拿 token → 跑引擎"就是为此）。
- Turnstile sitekey `0x4AAAAAADTRT4Uvmz3G7YH5`，在 confirm 那一步校验。
- 桌面端（Electron）UA 带 `Zo/`，前端用 `/Zo\//.test(navigator.userAgent)` 判断；
  `--desktop-ua` 就是模拟这个身份。
- 对话凭据是 `accessToken`（`zo_sk_…`），不是面板 API key；`apiKey` 字段留空。

## 2. 账号条目

```json
{
  "email": "ada@example.com",
  "handle": "ada-zo",
  "apiKey": "",
  "accessToken": "zo_sk_…",
  "refreshToken": "",
  "accessTokenExpiresAt": 0,
  "plan": "",
  "status": "active",
  "engine": "protocol",
  "domain": "example.com",
  "proxy": "",
  "createdAt": "2026-11-26T05:00:00.000Z",
  "gatewayKey": { "id": "k-9", "label": "zo:ada@example.com", "prefix": "sk-dcg-…", "plaintext": "sk-dcg-…" }
}
```

`schema` 由 `provider-pool.mjs` 的 `poolSchemaFor("zo")` 生成，恒为 `dcg.pool/zo@1`。

## 3. 反代面

```
GET  /p/zo/v1/models
POST /p/zo/v1/chat/completions     # OpenAI 形状 → /zo/ask → OpenAI chunk
POST /p/zo/v1/messages             # Anthropic 形状（同一 adapter）
```

`relay.kind = "zo-ask"`、`synthesizeStream: true`、`emulateTools: true`：上游没有
增量流、也没有原生工具协议，所以 SSE 由网关合成，工具调用按提示词约定模拟。
**复杂多轮工具链建议优先用支持原生工具的 provider。**

## 4. 已知边界

- 上游 `/zo/ask` 一次调用 = 一次完整 agent 运行，取消只能断开连接，没有 cancel 端点。
- 没有原生 `tools`，工具调用是模拟；`finish_reason` 由 adapter 推断。
- 魔法链依赖收信，邮箱域名健康度直接决定成功率（`antiban.domain_rph` 就是为此）。

## 5. 验证

```bash
node scripts/check-loop.mjs                         # zo 这一行应在 providers 里全 ✅
python -m pytest tools/zo-reg/tests -q              # 26 项：身份/配置/号池/限速/错误分类
node src/provider-cli.mjs register --provider zo -n 1 --mail-provider imap --mail-domains example.com
node src/provider-cli.mjs pool --provider zo
```
