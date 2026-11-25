# Blackbox 桌面端 / CLI 逆向记录与"运行环境模拟"

> 本文是本仓库里对 `blackbox.ai` 客户端侧的逆向笔记，以及把它接到本网关的
> **可复现运行环境**做法。纯离线分析产物在 `.tmpdata/bb/`（不入库），
> 结论都落在 `src/provider-catalog.mjs` 的 `blackbox.cli` 与
> `tools/blackbox-reg/blackbox_reg/clienv.py`。

---

## 1. 有什么可下载

| 形态 | 获取方式 | 备注 |
| --- | --- | --- |
| CLI v2（Node.js） | `https://blackbox.ai/install.ps1` / `install.sh` | 安装脚本 ~24 KB，纯文本，可读 |
| npm 包 | `@blackbox_ai/blackbox-cli@1.0.2`（`dist/` 1.9 MB）+ `@blackbox_ai/blackbox-cli-core@1.0.2` | 无 native 依赖，`dist/**/*.js` 直接读 |
| VS Code 扩展 | marketplace `blackboxapp.blackbox`（`.vsix`） | 里面是 webview + 与网页同一套后端 |
| 桌面端 | 未见独立 Electron 安装包 | 现有产品是"网页 + 扩展 + CLI" |

`install.ps1` 的真实下载逻辑（第 98–152 行）：

```powershell
$EXTENSION_SERVICE_URL = "https://releases.blackbox.ai"
$PLATFORM = "windows-x64"
$RELEASE_API_URL = "$EXTENSION_SERVICE_URL/api/v0/latest?product=blackbox-cli-v2&platform=$PLATFORM"
# → { url, version }，然后 Invoke-WebRequest 下载 zip，解到 $HOME\.blackbox-cli-v2
```

> 2026-11 实测该 `api/v0/latest` 对本机返回 404（可能按 UA/地区分流），
> 所以**别依赖自动安装**：`npm i -g @blackbox_ai/blackbox-cli` 或直接用解压后的
> `dist/index.js` 更可靠。

---

## 2. 逆向结论（对着 dist 代码读出来的）

### 2.1 配置与凭据

```
$HOME/.blackboxcli/
    settings.json          全局设置（security.auth.*）
    oauth_creds.json       设备码 OAuth 的令牌
    google_accounts.json   Google 登录档
    installation_id
```

`blackbox-cli-core/dist/src/config/storage.js`：`GEMINI_DIR = '.blackboxcli'`、
`getGlobalSettingsPath() = <home>/.blackboxcli/settings.json`、
`getOAuthCredsPath() = <home>/.blackboxcli/oauth_creds.json`。

### 2.2 provider 选择

`dist/src/config/settings.js` + `config/auth.js`：
`security.auth.selectedProvider` / `security.auth.selectedType`，
凭据写在 `security.auth.<provider>.{apiKey,baseUrl,model}`。
`AuthType` 字面量（`core/dist/src/core/contentGenerator.js`）：

```js
AuthType.USE_BLACKBOX_API = "blackbox-api"
AuthType.BLACKBOX_OAUTH  = "blackbox-oauth"
AuthType.USE_OPENAI      = "openai"
```

### 2.3 端点

| 用途 | 地址 | 证据 |
| --- | --- | --- |
| 默认 OpenAI baseUrl | `https://api.blackbox.ai/v1` | `commands/configure/ConfigureUI.js:19`、`AuthDialog.js:18` |
| 模型元信息 | `https://api.blackbox.ai/model/info` → 回退 `/models` | `core/dist/src/config/blackboxModels.js:226,253` |
| 加密推理 | `${BLACKBOX_ENCRYPTION_BASE_URL || 'https://encrypt.blackboxai.app'}/message_stream` | `core/dist/src/encrypt/config.js:29`、`streaming-client.js:116` |
| Cloud MCP | `https://cloud.blackbox.ai/api/mcp` | `commands/mcp/add.js:10` |
| 设备码 OAuth | `https://chat.blackboxcli.ai/api/v1/oauth2/{device/code,token}`，client_id `f0304373b74a44d2b584a3fb70ca9e56`，scope `openid profile email model.completion`，PKCE S256 | `core/dist/src/blackbox/blackboxOAuth2.js:15-21` |
| 发布的 release API | `https://releases.blackbox.ai/api/v0/latest?product=blackbox-cli-v2&platform=…` | `install.ps1:123` |
| shell 下载脚本 | `https://shell.blackbox.ai/api/scripts/blackbox-cli-v2/download.ps1` | `cli/utils/installationInfo.js` |

⚠️ `chat.blackboxcli.ai` 现在 NXDOMAIN，设备码登录这条路已死；
`api.blackbox.ai` 对所有路径回 `404`（Google Frontend）。**能用的推理入口是
`https://enterprise.blackbox.ai`**（`/chat/completions` 与 `/v1/chat/completions`
都回 `{"error":{"message":"blackbox.Error: Invalid API key","type":"auth_error"}}`）。

### 2.4 CLI 里的模型与工具

- 模型白名单由 `/model/info` 的 `data[].model_name` 给出，兜底是
  `blackboxai/encrypted`；
- 工具（shell / 文件读写 / 搜索）是 CLI 自己在本地执行的，**不是**上游工具调用；
  所以把它指向本网关时，工具链路只依赖模型是否按 OpenAI `tools` 协议回
  `tool_calls`——本网关的 `/p/blackbox/v1` 原样透传，不做改写。

---

## 3. 运行环境模拟（把它接到本网关）

`tools/blackbox-reg/blackbox_reg/clienv.py` 做三件事：

1. 在任意目录造一个隔离的 `HOME`，写入
   `.blackboxcli/settings.json`：

   ```json
   {
     "security": {
       "auth": {
         "selectedType": "blackbox-api",
         "selectedProvider": "blackbox",
         "blackbox": {
           "apiKey": "sk-dcg-…",
           "baseUrl": "http://127.0.0.1:8787/p/blackbox/v1",
           "model": "blackboxai/blackbox-pro"
         }
       }
     }
   }
   ```

2. 导出 `HOME` / `USERPROFILE` / `BLACKBOX_API_KEY` / `BLACKBOX_API_BASE_URL` /
   `BLACKBOX_API_MODEL`（双保险：settings 与 env 任一被读都能生效）；
3. 可选地在沙箱里跑一次
   `blackbox --model … --approval-mode yolo -p "<prompt>"`，把 stdout/stderr 抓回来。

### 用法

```powershell
# 网关先跑起来，并且号池里已经有号
node src\index.mjs --port 8787 --token <管理口令>

# 造环境（不跑）
python -m blackbox_reg cli-env `
  --gateway-url http://127.0.0.1:8787/p/blackbox/v1 `
  --gateway-key sk-dcg-… `
  --model blackboxai/blackbox-pro `
  --env-dir out\cli-env

# 造环境并直接跑一次（验证工具调用）
python -m blackbox_reg cli-env `
  --gateway-url http://127.0.0.1:8787/p/blackbox/v1 `
  --gateway-key sk-dcg-… `
  --env-dir out\cli-env --run `
  --prompt "Call the shell tool to print the current working directory, then answer in one line."
```

`prepare_env()` 的返回值里有完整的 `env` 字典与 `settings` 路径，脚本里可以直接
`subprocess` 拿去用；`--run` 只是把它跑一遍。

### 怎么判断"真的能调工具"

三个判据，缺一不可：

1. CLI 退出码 0；
2. stdout 里出现 shell 工具的执行痕迹（CLI 会打印命令与输出）；
3. 网关日志里出现 `POST /p/blackbox/v1/chat/completions`，且上游响应带
   `tool_calls`（`node tests/blackbox-e2e.mjs` 的第 4 项就是这个断言）。

---

## 4. 给其它编程工具的配置片段

| 工具 | Base URL | Key | 模型 |
| --- | --- | --- | --- |
| Cline / Roo / Continue（OpenAI 兼容） | `http://127.0.0.1:8787/p/blackbox/v1` | `sk-dcg-…` | `blackboxai/blackbox-pro` |
| Claude Code（Anthropic 兼容） | `http://127.0.0.1:8787/p/blackbox/v1` | `sk-dcg-…` | 同上 |
| OpenAI SDK | `base_url="http://127.0.0.1:8787/p/blackbox/v1"` | 同上 | 同上 |
| Blackbox CLI | `BLACKBOX_API_BASE_URL=http://127.0.0.1:8787/p/blackbox/v1` | 同上 | 同上 |
| MCP / n8n / Dify | 走 `/api/admin/providers/blackbox/*` 管理接口 | 管理口令 | — |

> Anthropic 客户端请把 base 指到 `…/p/blackbox/v1` 并把 `/messages` 拼在后面；
> 网关会把 Anthropic 请求翻成 OpenAI chat，回程再翻回来（含 SSE 事件序列）。

---

## 5. 未做 / 已知风险

- **Google 登录的设备验证**：如果 Google 要求短信/设备确认，`browser` 腿会停在
  密码页并落 DOM 快照（`out/screenshots/*.html`），需要人工过一次或用
  `--google-cookies` 复用已登录会话。
- **面板 Key 接口**没有官方文档，候选路径今天命中 `/api/keys`，明天站点改版可能失效；
  探测失败时用 `--keys-path` / `--keys-create-path` 手工指定，或只用浏览器腿。
- **免费档可能不给 Key**：官方文档写明 Agent API 需要 Pro；worker 在自检拿到 403
  且响应含 `needs Pro` 时会抛 `plan_required`，号池保留该号但标记不健康。
- `api.blackbox.ai` 已是空壳；如果哪天恢复，改 `BLACKBOX_API_BASE` 即可，不用改代码。
