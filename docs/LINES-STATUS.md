# 各线真实可用性（自动生成）

> 生成于 2026-11-26T14:39:48.665Z　命令：`node scripts/verify-lines.mjs --write`
> 判定：models 200/401/403=活；注册与发 Key 端点 404 或回 HTML = 不存在。

| 线 | models | 注册/发码 | 发 Key | 验证码 | 结论 |
| --- | --- | --- | --- | --- | --- |
| Agnes AI（agnes） | 401 ok | exists(200) / exists(200) | exists(401) | 需要 | usable-with-captcha |
| Cerebras（cerebras） | 403 ok | exists(400) | exists(400) | 无 | usable |
| OpenCode Zen（opencode-zen） | 200 ok | — | missing(404) / missing(404) | 无 | relay-ready |
| Requesty（requesty） | 200 ok | missing(404) | missing(404) / missing(404) | 无 | relay-ready |
| UnoRouter（unorouter） | 200 ok | — | missing(404) / missing(403) | 无 | relay-ready |
| Pollinations（pollinations） | 200 ok | — | — | 无 | relay-ready |
| Gitee AI（giteeai） | 200 ok | — | — | 无 | relay-ready |
| JEV AI（jevai） | 401 ok | exists(200) / exists(400) | missing(404) | 无 | signup-ok-keys-unknown |
| Clever AI（cleverai） | 404 suspect | — | — | 无 | models-broken |
| Gab AI（gabai） | 200 ok | — | — | 无 | relay-ready |
| ProxyScrape（proxyscrape） | — | exists(429) | — | 无 | signup-ok-keys-unknown |
| AllModels（allmodels） | — | — | — | 无 | not-checked |
| Poe（poe） | — | — | — | 无 | not-checked |
| Providers（providers） | — | — | — | 无 | not-checked |
| DeepAI（deepai） | — | — | — | 无 | not-checked |

## 证据

### Agnes AI（agnes）— usable-with-captcha
- models `https://api.agnes-ai.cn/v1/models` → 401 · 活着，需要 Key（正常）
- `GET https://platform-backend.agnes-ai.com/api/verification?email=probe@example.com&purpose=register` → 200 · HTTP 200 + JSON（端点存在），需验证码 · `{"code":400,"message":"Captcha verification is required","data":null}`
- `POST https://platform-backend.agnes-ai.com/api/user/register` → 200 · HTTP 200 + JSON（端点存在） · `{"code":400,"message":"Too many registration attempts from this email domain. Please try again later","data":null}`
- `POST https://platform-backend.agnes-ai.com/api/token` → 401 · HTTP 401 + JSON（端点存在） · `{"code":401,"message":"Not logged in or invalid token","data":null}`
- 备注：有验证码门槛

### Cerebras（cerebras）— usable
- models `https://api.cerebras.ai/v1/models` → 403 · 活着，需要 Key（正常）
- `GET https://cloud.cerebras.ai/api/auth/ok` → 400 · HTTP 400 + JSON（端点存在） · `"Bad request."`
- `GET https://cloud.cerebras.ai/api/auth/api-keys` → 400 · HTTP 400 + JSON（端点存在） · `"Bad request."`

### OpenCode Zen（opencode-zen）— relay-ready
- models `https://opencode.ai/zen/v1/models` → 200 · 匿名可列模型
- `GET https://opencode.ai/zen/api/keys` → 404 · HTTP 404（端点不存在） · `<!DOCTYPE html><html lang="en" dir="ltr" data-locale="en"><head><meta charset="utf-8"> <!--                             `
- `GET https://opencode.ai/api/keys` → 404 · HTTP 404（端点不存在） · `<!DOCTYPE html><html lang="en" dir="ltr" data-locale="en"><head><meta charset="utf-8"> <!--                             `
- 备注：有登记端点不存在，需要修 site.py / 描述

### Requesty（requesty）— relay-ready
- models `https://router.requesty.ai/v1/models` → 200 · 匿名可列模型
- `GET https://app.requesty.ai/api/auth/get-session` → 404 · HTTP 404（端点不存在） · `<!DOCTYPE html><html data-dpl-id="dpl_AZ14JYHKwihLYRkdisSiaLFp5CBs" lang="en" class="dark"><head><meta charSet="utf-8"/>`
- `GET https://app.requesty.ai/api/api-keys` → 404 · HTTP 404（端点不存在） · `<!DOCTYPE html><html data-dpl-id="dpl_AZ14JYHKwihLYRkdisSiaLFp5CBs" lang="en" class="dark"><head><meta charSet="utf-8"/>`
- `GET https://app.requesty.ai/api/keys` → 404 · HTTP 404（端点不存在） · `<!DOCTYPE html><html data-dpl-id="dpl_AZ14JYHKwihLYRkdisSiaLFp5CBs" lang="en" class="dark"><head><meta charSet="utf-8"/>`
- 备注：有登记端点不存在，需要修 site.py / 描述

### UnoRouter（unorouter）— relay-ready
- models `https://api.unorouter.com/v1/models` → 200 · 匿名可列模型
- `GET https://unorouter.com/api/token` → 404 · HTTP 404（端点不存在） · `NOT_FOUND`
- `GET https://api.unorouter.com/api/token` → 403 · HTTP 403 回了 HTML · `<!DOCTYPE html><html lang="en-US"><head><title>Just a moment...</title><meta http-equiv="Content-Type" content="text/htm`
- 备注：有登记端点不存在，需要修 site.py / 描述

### Pollinations（pollinations）— relay-ready
- models `https://gen.pollinations.ai/v1/models` → 200 · 匿名可列模型

### Gitee AI（giteeai）— relay-ready
- models `https://ai.gitee.com/v1/models` → 200 · 匿名可列模型

### JEV AI（jevai）— signup-ok-keys-unknown
- models `https://jev-ai.org/api/v1/models` → 401 · 活着，需要 Key（正常）
- `GET https://jev-ai.org/api/auth/ok` → 200 · HTTP 200 + JSON（端点存在） · `{"ok":true}`
- `POST https://jev-ai.org/api/auth/sign-up/email` → 400 · HTTP 400 + JSON（端点存在） · `{"code":"EMAIL_DOMAIN_NOT_ALLOWED","message":"Temporary or placeholder email addresses cannot be used. Please sign up wi`
- `GET https://jev-ai.org/api/user/api-keys` → 404 · HTTP 404（端点不存在） · `<!DOCTYPE html><html lang="en" dir="ltr" class="__variable_8eaea4 __variable_4dc837"><head><meta charSet="utf-8"/><meta `
- 备注：有登记端点不存在，需要修 site.py / 描述

### Clever AI（cleverai）— models-broken
- models `https://api.cleverai.app/appserver/api/v1/models` → 404 · HTTP 404

### Gab AI（gabai）— relay-ready
- models `https://gab.ai/v1/models` → 200 · 匿名可列模型

### ProxyScrape（proxyscrape）— signup-ok-keys-unknown
- `POST https://dashboard.proxyscrape.com/v2/v4/account/auth/register` → 429 · HTTP 429 + JSON（端点存在） · `{"type":"https://developers.cloudflare.com/support/troubleshooting/http-status-codes/cloudflare-1xxx-errors/error-1015/"`

### AllModels（allmodels）— not-checked

### Poe（poe）— not-checked

### Providers（providers）— not-checked

### DeepAI（deepai）— not-checked

