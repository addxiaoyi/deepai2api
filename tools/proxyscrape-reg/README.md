# proxyscrape-reg — 试用注册机 + 代理池

`proxyscrape.com` 的 premium 免费试用注册机。产物不是模型通道，而是**代理**：
每个账号领一份试用，试用给出 `host:port:user:pass`，导出来喂给其它注册机当出口。

## 协议（从 dashboard 的 Next.js bundle 还原）

站点是 `dashboard.proxyscrape.com`（`/v2/...`），注册和登录都要 **Cloudflare Turnstile**：

| 步骤 | 请求 | 说明 |
| --- | --- | --- |
| 1 | `POST /v2/v4/account/auth/register` `{email,password,cf_turnstile_token}` | 缺 token 直接 `400 "No Cloudflare Turnstile token provided."` |
| 2 | `POST /v2/v4/account/auth/login` `{service:"email",email,password,cf_turnstile_token}` | 拿 `data.tokens.access` |
| 3 | `GET /v2/v4/account/premium/trial-eligibility` | `{claimed: bool}` |
| 4 | `POST /v2/v4/account/premium/claim-trial` | 领试用；重复领 → `400 "Trial already claimed"` |
| 5 | `GET /v2/v4/account/accounts-summary` | 各产品/服务 + `proxy_credentials` |
| 6 | `GET /v2/services/{product}/proxy-setup/{id}` | 代理接入信息（文本或 JSON） |
| 7 | `GET /v2/v4/account/api-keys` | 面板 API key |

要点：

- Turnstile sitekey `0x4AAAAAAAFWUVCKyusT9T8r`，字段名是 `cf_turnstile_token`
  （不是 `turnstileToken`）。
- 代理凭据**不在固定字段**：`services.<product>.proxy_credentials` 里有
  `host` / `ports[]` / `username` / `password`，不同产品形状不一样。所以 `protocol.py`
  用**深扫描**（`extract_proxies`）把任何 `{host|ip|server}` + `{port|ports}` 组合挖出来，
  再把 `proxy-setup` 的文本（`host:port:user:pass`）一起解析。
- 试用按账号发；同一出口领太多次会被拒（`no_trial`）。

## 安装

```bash
cd tools/proxyscrape-reg
pip install -r requirements.txt
# 浏览器腿（Turnstile 走真实页面时）：
pip install "camoufox[geoip]"
```

## 用法

```bash
# 体检：站点可达 + 注册端点的 Turnstile 门还在不在
python -m proxyscrape_reg doctor

# 注册 3 个并领试用（默认串行，试用按出口发）
python -m proxyscrape_reg run -n 3 -c 1 \
    --mail-provider imap --mail-domain example.com

# 只用现成 token（调试）
python -m proxyscrape_reg run -n 1 --captcha manual --captcha-key "$TS_TOKEN"

# 看池子 / 只打代理 / 导出
python -m proxyscrape_reg list
python -m proxyscrape_reg proxies
python -m proxyscrape_reg export --format proxies --out out/proxies.txt

# 回推网关（会同时写进账号池与共享代理池）
python -m proxyscrape_reg push --push-url http://127.0.0.1:8787 --push-token $TOKEN
```

## 双引擎

| `--engine` | 行为 |
| --- | --- |
| `protocol` | 纯 HTTP：解 Turnstile → 注册 → 登录 → 领试用 → 读凭据 |
| `browser` | Camoufox 打开 `/v2/sign-up` 填表提交（Turnstile 由页面自己过），再走协议领试用 |
| `auto` | 先协议；被 `turnstile_required` / `blocked` 挡下就换浏览器 |

## Turnstile 三种拿法

| `--captcha` | 说明 |
| --- | --- |
| `browser` | Camoufox 在注册页读 `cf-turnstile-response` 隐藏域（默认） |
| `capsolver` | `AntiTurnstileTask(ProxyLess)`，带 sitekey + 页面 URL |
| `manual` | `--captcha-key` 直接给一枚 token |
| `none` | 不发 token；只有站点临时关掉校验时才走得通 |

## 配置

`config.example.yaml` 是完整字段；环境变量：

```
PROXYSCRAPE_BASE_URL=https://dashboard.proxyscrape.com
PROXYSCRAPE_CAPTCHA_PROVIDER=browser
PROXYSCRAPE_CAPTCHA_KEY=...
PROXYSCRAPE_MAIL_DOMAIN=example.com
PROXYSCRAPE_IMAP_HOST / _USER / _PASSWORD
PROXYSCRAPE_GATEWAY_URL / _GATEWAY_TOKEN
PROXYSCRAPE_PYTHON / PROXYSCRAPE_REGISTER_DIR
```

## 事件流（`--json`）

```
{"type":"start", ...}      配置（含 claim_trial）
{"type":"attempt", ...}    一个邮箱一次尝试
{"type":"account", ...}    成功，带 email / trialClaimed / proxies[]
{"type":"pushed", ...}     网关已入池
{"type":"failed", ...}     最终失败（kind：turnstile_required / no_trial / no_credentials / ...）
{"type":"done","summary":{...}}
```

## 测试

```bash
python -m pytest tests -q      # 离线，不联网
```
