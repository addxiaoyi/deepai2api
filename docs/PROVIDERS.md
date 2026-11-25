# 多 Provider 号池：注册 → 号池 → 反代出 Key

本文件是 **WaveSpeed / Novita**（以及同框架下其他 provider）的接入总览。
目标是三句话：

1. **能注册**：域名邮箱 + 指纹浏览器 / 协议双腿 + 防封策略；
2. **能入池**：每个 provider 一份独立号池，互不覆盖、互不阻塞；
3. **能出 Key**：反代成 OpenAI 兼容接口，任何编程工具填 `base_url + sk-dcg-…` 就能用。

---

## 1. 五分钟上手

```bash
# 看有哪些 provider、号池几个账号、worker 是否就位
node src/provider-cli.mjs list

# 注册 3 个 Novita 账号（域名邮箱 + 协议腿，被 Cloudflare 拦会自动切浏览器）
node src/provider-cli.mjs register --provider novita -n 3 -c 1 \
  --mail-provider imap --mail-domain your-domain.com \
  --captcha capsolver --captcha-key CAP-xxxx \
  --key-policy '{"per_account":2,"expire_time":"90d"}'

# 看进度 / 失败分类
node src/provider-cli.mjs status --provider novita

# 拿到反代地址，填进任何 OpenAI SDK
node src/provider-cli.mjs relay --provider novita
```

`relay` 会打印：

```
base_url = "http://127.0.0.1:8787/p/novita/v1"
api_key  = "sk-dcg-…"
```

然后：

```python
from openai import OpenAI

client = OpenAI(base_url="http://127.0.0.1:8787/p/novita/v1", api_key="sk-dcg-…")
print(client.chat.completions.create(
    model="deepseek/deepseek-v4.1-flash",
    messages=[{"role": "user", "content": "hi"}],
).choices[0].message.content)
```

---

## 2. 两个 provider 的事实差异（决定了架构）

| | WaveSpeed | Novita |
| --- | --- | --- |
| 注册方式 | **只有 OAuth**（Google/GitHub，AWS 需审核） | **邮箱密码 + 邮件激活**，也有三种 OAuth |
| 注册入口 | `/sign-in`（`/sign-up` 不存在） | `/user/register` |
| 人机校验 | 未公开（按出口 IP 评分决定） | **Turnstile**，sitekey `0x4AAAAAAAaG28VfN_OxkED8`，字段 `cloudflareToken` |
| 建 Key 接口 | **没有**（只有 UI 点 Generate） | **有**：`POST /v2/user/key`（明文只出现一次） |
| Key 数量 | 不限（但明文一次性） | 每账号 ≤ 10，有效期 Permanent/90d/30d/24h，建后不可改 |
| LLM 基址 | `https://llm.wavespeed.ai/v1` | `https://api.novita.ai/openai` |
| 生成任务 | `https://api.wavespeed.ai/api/v3`（`data.urls.get` 由服务端下发） | `https://api.novita.ai/v3/async/*`（参数包在 `request` 里） |
| 余额 | `/api/v3/balance` | `/openapi/v1/billing/balance/detail`（单位 **1/10000 USD**） |
| 主要风控 | OAuth 重复授权、IP 聚类 | voucher 同设备多账号、实名门槛、RPM/TPM |

### 2.1 Blackbox AI（第五条线，形态又不一样）

| | Blackbox |
| --- | --- |
| 注册方式 | **只有 Google OAuth**（`/api/auth/providers` 只回 google；`POST /api/auth/sign-up/email` 恒 400） |
| 注册入口 | `https://app.blackbox.ai/login`（`/signup`、`/register` 都不存在） |
| 人机校验 | app 侧**没有**（风控在 Google 那半程）；console 的 Email OTP 腿有 Turnstile |
| 建 Key 接口 | 面板里有，但**没有公开文档**：worker 候选路径探测 + 命中缓存（`--keys-path` 可覆盖） |
| Key 形态 | `sk-…`（官方文档：Agent API 的 key 也是这个形状） |
| LLM 基址 | `https://enterprise.blackbox.ai`（`/chat/completions` 与 `/v1/chat/completions` 都在） |
| 模型列表 | 上游 `/models`、`/v1/models` 都 404 → 用描述符白名单 |
| 主要风控 | Google 设备验证、OAuth 重复授权、免费档不给 Key（`plan_required`） |

协议取证与 worker 用法见 [`BLACKBOX-POOL.md`](BLACKBOX-POOL.md) /
[`BLACKBOX-CLI-RE.md`](BLACKBOX-CLI-RE.md) /
[`../tools/blackbox-reg/README.md`](../tools/blackbox-reg/README.md)。

<!-- 兼容旧锚点：blackbox 走同一套 provider 骨架，engine / 邮箱 / 防封参数含义不变 -->

> Novita 的接口路径是从线上 SPA chunk 反查出来的，不是猜的。
> 细节与出处见 [tools/novita-reg/README.md](../tools/novita-reg/README.md)。

**直接后果**：

- WaveSpeed 的协议腿**无法从零建号**，只能在"已有会话"或"只校验 key"时用；
  `engine=protocol` 且无会话会立刻抛 `OAuthUnavailable`（不浪费时间重试）。
- Novita 的协议腿是**完整可用**的：注册 → 激活 → 建 key → 校验，全程 HTTP。

---

## 3. 双腿注册：协议 + 指纹浏览器

`engine` 三档：

| engine | 行为 |
| --- | --- |
| `protocol` | 纯 HTTP；被拦直接失败（不自动切） |
| `browser` | 直接开指纹浏览器 |
| `auto`（默认） | 协议优先，遇到 `blocked` / `captcha_required` / `key_not_obtainable` 自动切浏览器 |

浏览器内核按指纹质量自动降级：

1. **Camoufox** —— Firefox + C++ 层指纹改写，canvas/WebGL/fonts/screen/时区自洽；
2. **Patchright** —— Playwright 的反检测分支；
3. **Playwright Chromium** —— 最小 patch 兜底；
4. **外部 CDP** —— `--cdp http://127.0.0.1:50325` 接管 AdsPower / BitBrowser / Morelogin 的
   已登录 profile（OAuth 场景最省事）。

```bash
# OAuth 走已登录的指纹浏览器
node src/provider-cli.mjs register --provider wavespeed -n 5 --cdp http://127.0.0.1:50325
```

---

## 4. 域名邮箱

四个来源，按耐用程度排序：

| provider | 说明 |
| --- | --- |
| `imap` / `domain` | **自有 catch-all 域名**，IMAPS 轮询，最耐用（推荐） |
| `fixed` | 一个自己的地址，按 plus 分号给每账号 |
| `mailtm` | 公共 API，盒子约 7 天 |
| `tempmail_lol` | 公共 API，随机域名 |

```yaml
# config.yaml
mail:
  provider: imap
  domain: your-domain.com
  domains: [your-domain.com, your-domain2.com]
  imap_host: imap.your-domain.com
  imap_user: catchall@your-domain.com
  imap_password: "********"
  verify_timeout: 180
```

环境变量：`WAVESPEED_IMAP_*` / `NOVITA_IMAP_*`。

---

## 5. 防封策略（`anti_ban`）

命令行用 JSON 覆盖，不传就用默认值（默认偏"慢但稳"）：

```bash
node src/provider-cli.mjs register --provider novita -n 20 -c 2 \
  --anti-ban '{"domain_rpm":2,"ip_concurrency":1,"jitter_ms":[2000,6000],"domain_health_floor":0.4}'
```

| 参数 | 默认 | 作用 |
| --- | --- | --- |
| `domain_rpm` / `domain_rph` | 3 / 40 | 单域名每分钟 / 每小时建号上限 |
| `domain_health_floor` | 0.25 | 健康分低于此值暂停该域名 |
| **`ip_concurrency`** | 1 | **同一出口 IP 同时只跑 1 个**（最有效） |
| `proxy_rotate` | true | 每账号轮换代理 |
| `jitter_ms` | [1200, 4200] | 全局抖动，避免固定节奏 |
| `geo_align` | true | 时区/语言跟随代理出口国家 |
| `fresh_profile` / `isolate_profile` | true | 每账号独立浏览器上下文 / profile 目录 |
| `clear_storage_between` | true（Novita） | 进页面前清 localStorage |
| `reuse_session` | true | 复用 cookie jar，减少重复登录 |
| `backoff` | 见下 | 按失败类型退避（秒） |

### 失败分类与处置

| kind | 含义 | 处置 |
| --- | --- | --- |
| `email_taken` | 地址已注册 | 换地址（零退避） |
| `email_rejected` | 地址被拒 | 换域名 + 降权该域名 |
| `password_rejected` | 密码不合规 | 换更强密码 |
| `blocked` | Cloudflare / WAF | 换引擎 + 退避 20–40s |
| `captcha_required` | Turnstile 拒绝 | 换引擎 + 退避 15–30s |
| `verification_pending` | 没收到验证信 | 短退避 5–10s |
| `key_not_obtainable` | 拿不到 key 明文 | 换引擎或换账号（零退避） |
| `oauth_unavailable` | OAuth 走不通 | 换账号（零退避） |
| `verify_required` | 需实名（Novita 403） | 换账号（零退避） |
| `quota_exhausted` | 余额/voucher（Novita 402） | 换账号（零退避） |
| `rate_limited` | 429 | 退避 20–45s + 换代理 |

**"零退避"是一个明确信号**：这类失败重试同一个目标只会更糟，直接换身份。

---

## 6. 独立号池联动

物理隔离约定（全部由 `provider.id` 派生，与 allmodels / moonai / poe / deepai 完全不重叠）：

```
data/<provider>-pool.json         号池，schema = dcg.pool/<provider>@1，0600，原子写
data/<provider>-register.lock     注册互斥锁（跨网关 / MCP / CLI）
data/<provider>-register-job.json job 状态（崩溃后可恢复）
data/<provider>-reg/              worker 的 sqlite 与 jsonl
tools/<provider>-reg/             Python worker（-m <provider>_reg）
```

由此得到三条保证：

1. **同时跑**：wavespeed 的锁不会挡 novita 的注册；
2. **不串号**：读号池时校验 `provider` 字段，别人的文件一律不读；
3. **可合并**：`importAccount()` 是唯一写入口，按 `apiKey` → `email` 去重，
   幂等地只发一把网关 Key。

号池条目（`maskAccount` 脱敏后对外）：

```json
{
  "id": "uuid",
  "email": "a@example.com",
  "apiKeyPreview": "sk_live_…f3a9",
  "keySource": "accesskey-ui",
  "expireTime": "90d",
  "engine": "browser",
  "status": "active",
  "healthy": true,
  "cooldownUntil": 0,
  "requests": 0,
  "gatewayKey": { "id": "uuid", "label": "novita:a@example.com", "prefix": "sk-dcg-AbCdEfGh" }
}
```

---

## 7. 反代出 Key

路由（挂在网关同一个 HTTP server 上，不另起进程）：

| 路由 | 用途 |
| --- | --- |
| `GET  /p/<provider>/v1/models` | 模型列表（自动换号，坏 Key 会被跳过） |
| `POST /p/<provider>/v1/chat/completions` | OpenAI 兼容对话（SSE 透传） |
| `POST /p/<provider>/v1/completions` | 旧版补全 |
| `POST /p/<provider>/v1/messages` | Anthropic 兼容 |
| `POST /p/<provider>/v1/images/generations` | OpenAI 风格图像 |
| `POST /p/<provider>/v1/async/<task-path>` | 异步任务提交（走 task 基址） |
| `GET  /p/<provider>/v1/async/result?id=…` | 异步任务轮询 |

鉴权两段：

1. **客户端侧**：网关自己的 `sk-dcg-…`（和 `/v1/*` 同一把尺子，计费/限额共用）；
2. **上游侧**：号池里轮询出来的面板 Key（`Authorization: Bearer <panel key>`）。

换号逻辑：401/402/403/429/5xx 一律换下一个账号（最多 3 个），失败账号回报号池
并进入冷却（429 冷却 60s，其它 30s）。

### 异步任务的路径差异（容易踩）

```
POST /p/wavespeed/v1/async/wavespeed-ai/z-image/turbo
     -> POST https://api.wavespeed.ai/api/v3/wavespeed-ai/z-image/turbo
GET  /p/wavespeed/v1/async/result?id=task-1
     -> GET  https://api.wavespeed.ai/api/v3/predictions/task-1/result

POST /p/novita/v1/async/txt2img
     -> POST https://api.novita.ai/v3/async/txt2img
GET  /p/novita/v1/async/result?id=nv-1
     -> GET  https://api.novita.ai/v3/async/task-result?task_id=nv-1
```

提交固定 `attempts=1`：**提交即计费，重试会产生重复任务**。

---

## 8. 在编程工具里调用

三种入口共用同一份 JSON Schema 与 handler（`src/provider-tools.mjs`）：

| 入口 | 适合 | 传输 |
| --- | --- | --- |
| `src/provider-mcp.mjs` | Claude Code / Cursor / Codex / VS Code Copilot | stdio JSON-RPC 2.0 |
| `POST /api/admin/providers/:id/*` | Open WebUI 插件、n8n、Dify | HTTP + 管理 token |
| `src/provider-cli.mjs` | 人 / shell / CI | CLI |

### 工具清单

| 工具 | 作用 |
| --- | --- |
| `provider_list` | 列出所有 provider 与号池概况 |
| `provider_status` | 某个 provider 的 job 进度、失败分类、最近日志 |
| `provider_register` | 批量注册（含 antiBan / keyPolicy） |
| `provider_register_stop` | 强杀当前注册 |
| `provider_pool` | 列账号（Key 脱敏） |
| `provider_export` | 导出账号 + 面板 Key + 网关 Key |
| `provider_import` | 外部账号并入号池（幂等） |
| `provider_mark_health` | 回报一次调用结果 |
| `provider_remove` | 移除账号 |
| `provider_probe` | 拿号池 Key 打上游 `/models` 自检 |
| `provider_relay_info` | 给出反代 base_url 与 Key 前缀 |
| `provider_antiban` | 查看当前生效的防封默认值 |

### Claude Code

项目已带 [`.mcp.json`](../.mcp.json)，在其中启动即自动发现 `providers` 服务器：

```json
{ "mcpServers": { "providers": { "command": "node", "args": ["src/provider-mcp.mjs"] } } }
```

或全局注册：

```bash
claude mcp add providers -- node /绝对路径/deepaiapi/src/provider-mcp.mjs
claude mcp list
```

### Cursor / VS Code / Codex

```json
{ "mcpServers": { "providers": { "command": "node", "args": ["src/provider-mcp.mjs"] } } }
```

```toml
# ~/.codex/config.toml
[mcp_servers.providers]
command = "node"
args = ["D:/qwq/项目/deepaiapi/src/provider-mcp.mjs"]
```

### 手测

```bash
node src/provider-mcp.mjs --print-tools
printf '%s\n' \
 '{"jsonrpc":"2.0","id":1,"method":"initialize","params":{"protocolVersion":"2025-06-18","capabilities":{},"clientInfo":{"name":"cli","version":"0"}}}' \
 '{"jsonrpc":"2.0","method":"notifications/initialized"}' \
 '{"jsonrpc":"2.0","id":2,"method":"tools/call","params":{"name":"provider_list","arguments":{}}}' \
 | node src/provider-mcp.mjs
```

---

## 9. 桌面端 / APK 逆向

WaveSpeed 官方发布了 **WaveSpeed Desktop**（Windows / macOS / Linux）与 **Android APK**
（`wavespeed.ai/desktop` 与 GitHub Releases，APK 签名 SHA-256 已公示）。
桌面端只需要用户"粘 API key"，说明它**不携带账号体系** —— 逆向它的价值不是注册，
而是把官方客户端当成**协议文档**：恢复真实端点、header 契约、轮询策略。

分析器：`tools/wavespeed-reg/wavespeed_reg/re.py`（纯静态，不执行样本、不联网）

```bash
cd tools/wavespeed-reg

# 自动在 Downloads / Desktop / TEMP 里找安装包
python -m wavespeed_reg.re --detect

# 指定安装包或已解包目录
python -m wavespeed_reg.re --path "C:/Downloads/WaveSpeed-Setup.exe"
python -m wavespeed_reg.re --path ./app.asar.unpacked --out ./out/re

# 只要端点清单
python -m wavespeed_reg.re --path ./app.apk --only endpoints
```

支持三类输入：

| 输入 | 处理 |
| --- | --- |
| Electron 安装包 | 定位 `resources/app.asar`，解析 header + 逐文件读取后扫描 |
| 已解包目录 | 直接扫 `.js/.mjs/.cjs/.json/.html` |
| `.apk` | zip 读取 `classes*.dex` 与 assets，扫字符串/URL；附带提取包名 |

输出：`out/re/<name>.json`（结构化）+ `out/re/<name>.md`（人读报告），
其中 `simulate_endpoints()` 会给出**可直接粘进 config.yaml 的端点配置**和
"worker 该怎么改"的结论（例如发现建 key 写接口 → 可以补一条协议腿）。

已用合成样本验证：asar 与 apk 都能正确抽出端点、过滤噪声域名
（`w3.org` 等无关 URL）、并在报告里落盘。

---

## 10. 离线验证（模拟运行环境）

`tests/mock-provider.mjs` 是一个假上游，同时扮演两家的面板与推理端点：

```bash
node tests/mock-provider.mjs --port 8899
# 预置 demo@example.com，打印可用的面板 Key
curl -H "Authorization: Bearer <key>" http://127.0.0.1:8899/v1/models
```

端到端（完全离线，覆盖"入池 → 反代 → 换号 → 导出"）：

```bash
node tests/providers-wavespeed-novita-e2e.mjs
```

单元测试：

```bash
node --test tests/providers-wavespeed-novita.test.mjs tests/novita-pool.test.mjs
python tools/wavespeed-reg/tests/test_offline.py    # 18 项
python tools/novita-reg/tests/test_offline.py       # 21 项
```

或一次跑完：

```bash
npm run test:providers
```

---

## 11. 与现有模块的关系

| 模块 | 是否受影响 |
| --- | --- |
| `src/allmodels-*` | 不动。`allmodels-pool.json` 与前缀 `/api/admin/allmodels/*` 完全不变 |
| `src/moonai-*` / `src/zo-adapter.mjs` | 不动。反代按 `provider.relay.kind` 分派，走各自专用翻译层 |
| `src/store.mjs` 的网关 Key | 复用。新 provider 的 Key 用同一张 `keys` 表与同一套限额 |
| `/v1/*`（DeepAI 主链路） | 不动。新路由在 `/p/<provider>/v1/*` |

唯一被改动的共用文件是 `src/provider-catalog.mjs`（新增 provider 条目）与
`src/provider-relay.mjs`（新增异步任务提交分支）；两者都是**追加**，不改既有 provider 行为。

---

## 12. 已知限制

- **WaveSpeed 的 OAuth 是硬门槛**：没有可用登录态就无法建号。这是站点事实，不是实现缺陷。
- **WaveSpeed 没有建 key 接口**：明文只能从 UI 读一次；读完立刻入库。
- **Novita 有实名门槛**：部分模型 403 `Model requires identity verification`，只能换账号。
- **Novita voucher 风控**：同设备多账号会被撤销赠金 —— `isolate_profile` 就是为此存在。
- **Turnstile**：两家都需要 token（WaveSpeed 未公开 sitekey，运行时从页面挖）。
  `--captcha auto` 先不带 token 试，被拒再按配置解题。
- **生成类 POST 不重试**：这是刻意的，重复提交会产生重复计费任务。
