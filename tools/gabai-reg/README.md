# gabai-reg（gab.ai 批量注册机）

两条腿：协议 + 指纹浏览器（Camoufox / Playwright / 外部 CDP）。

- 协议：直接调用 `POST https://gab.ai/json/auth/signup/email`，再用 bearer token 调 `POST /json/api-keys` 领 `gab_…` key。
- 指纹浏览器：协议被 CF/Turnstile 拦住时，开 `/signup` 走完表单，然后在同一登录态里 `fetch('/json/api-keys', {method:'POST'})` 领 key。

> 重要：`gab_…` developer key 需要 Plus 才能调用 `/v1/chat/completions`；没有 Plus 的账号仍可注册并入池，但会被标记 `api_blocked=plus_required`。

## 快速开始

```powershell
cd tools\gabai-reg
python -m pip install -r requirements.txt
python -m gabai_reg doctor

# 跑 5 个，协议优先，遇到 bot gate 自动切浏览器
python -m gabai_reg run -n 5 -c 2 --engine auto --json
```

输出：

- `out/accounts.sqlite3`：主库
- `out/accounts.jsonl` / `out/accounts.csv`
- `out/screenshots/`：浏览器路径截图

## 主要参数

- `--engine auto|protocol|browser`
- `--mail-provider tempmail_lol|mailtm|imap|fixed`
- `--proxy` 多次或逗号分隔
- `--browser-stack auto|camoufox|playwright`
- `--cdp http://127.0.0.1:PORT` 接管外部指纹浏览器

