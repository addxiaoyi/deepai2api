# Turnstile 求解器（`tools/captcha-solver`）

注册页有人机验证的线，统一靠这个本地服务过 —— 不用给每条线各装一套浏览器逻辑，
也不用买 capsolver。

来源：vendored 自 [Theyka/Turnstile-Solver](https://github.com/Theyka/Turnstile-Solver)
（**CC BY-NC 4.0，仅限非商业用途**，LICENSE 原样保留在目录里）。

## 协议（Quart 服务）

| 方法 | 路径 | 说明 |
| --- | --- | --- |
| `GET` | `/turnstile?url=&sitekey=[&action=&cdata=]` | 建任务，`202 {"task_id": "…"}` |
| `GET` | `/result?id=<task_id>` | `200 {"value": "<token>"}` / `{"value":"CAPTCHA_NOT_READY"}` / `422 {"value":"CAPTCHA_FAIL"}` |
| `GET` | `/` | 文档页（探活用） |

## 启动

```bash
pip install -r tools/captcha-solver/requirements.txt   # quart + patchright + camoufox
python tools/captcha-solver/main.py --headless
# 默认监听 http://127.0.0.1:5000
```

## 网关侧接入

```bash
# 探活
curl -s -H "Authorization: Bearer $TOKEN" localhost:8787/api/admin/captcha/status

# 解一枚 token
curl -s -X POST -H "Authorization: Bearer $TOKEN" -H 'Content-Type: application/json' \
  -d '{"url":"https://dashboard.proxyscrape.com/v2/sign-up","sitekey":"0x4AAAAAAAFWUVCKyusT9T8r"}' \
  localhost:8787/api/admin/captcha/solve
```

`src/captcha.mjs` 是客户端（`solveTurnstile` / `solverStatus`），带超时、轮询、错误码
（`solver_unavailable` / `solver_failed` / `solver_timeout`）。

## 已经接上的线

| 线 | 怎么用 |
| --- | --- |
| ProxyScrape | `--captcha solver --captcha-key http://127.0.0.1:5000`（或环境变量 `PROXYSCRAPE_CAPTCHA_SOLVER_URL`） |
| 其它线 | 在各自 worker 里调 `solver`（Turnstile sitekey + 页面 URL），逻辑同 `from_solver` |

```bash
cd tools/proxyscrape-reg
python -m proxyscrape_reg run -n 3 --captcha solver --captcha-key http://127.0.0.1:5000
```

## 注意

- 这个服务会开真实浏览器（patchright / camoufox），单机并发别超过 `--thread`（默认按 CPU 调），
  否则内存先炸。
- 它解的是 **Turnstile widget**；带 `action` / `cdata` 的站点要把这两个参数一起传。
- 版本升级：直接替换 `tools/captcha-solver` 下的文件，网关侧协议不变。
