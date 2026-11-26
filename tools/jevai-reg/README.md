# jevai-reg — jev-ai.org 注册机 + Key 池

注册 jev-ai.org（Better Auth），拿 `sk-glm5-…` Key，落库成号池。

## 站点事实

| 项 | 值 |
| --- | --- |
| 站点 | `https://jev-ai.org`（Next.js + Better Auth） |
| 认证 | `POST /api/auth/sign-up/email`、`/api/auth/sign-in/email`、`GET /api/auth/get-session` |
| 健康 | `GET /api/auth/ok` → `{"ok":true}` |
| 模型 API | `https://jev-ai.org/api/v1`，`Authorization: Bearer sk-glm5-…` |
| 已知端点 | `GET /api/v1/models`、`POST /api/v1/systemone` |
| Key 页面 | `https://jev-ai.org/settings/apikeys` |
| 邮箱限制 | 一次性/占位域名直接拒（`EMAIL_DOMAIN_NOT_ALLOWED`） |

## 安装 / 用法

```bash
pip install -r requirements.txt

python -m jevai_reg doctor                 # 探针：认证面 + 模型 API 的错误形状 + 端点缓存
python -m jevai_reg run -n 3 -c 1 --mail-provider imap --mail-domain example.com
python -m jevai_reg list                   # 账号（脱敏）
python -m jevai_reg keys                   # 一行一把 sk-glm5-…
python -m jevai_reg export --format keys --out out/keys.txt
python -m jevai_reg push --push-url http://127.0.0.1:8787 --push-token $TOKEN
```

## 两处"站点没告诉我们"的地方

1. **建 Key 的端点**：`/settings/apikeys` 是客户端渲染，接口名不在首屏 bundle。
   worker 用候选探测 + 命中缓存（`<output_dir>/key-endpoints.json`）：
   逐个试常见路径，谁的响应里出现 `sk-glm5-…` 就用谁。失败报 `no_api_key`。
2. **是否有验证门槛**：注册成功即可登录；未验证账号能否建 Key 取决于站点策略，
   所以默认走邮件验证（`verify_email: true`），失败也继续尝试建 Key。

## 事件流（`--json`）

```
{"type":"start","count":…,"mail_provider":…,"verify_email":…}
{"type":"attempt","index":0,"email":"…"}
{"type":"account","account":{"email":"…","apiKey":"sk-glm5-…",…}}
{"type":"failed","kind":"email_rejected|email_taken|no_api_key|rate_limited|transport|…"}
{"type":"done","summary":{…}}
```

## 失败分类

`email_rejected`、`email_taken`、`password_rejected`、`rate_limited`、`blocked`、
`captcha_required`、`verification_pending`、`no_api_key`、`transport`
—— 与其它 worker 同一套词表，网关侧不用做特殊映射。

## 测试

```bash
python -m pytest tests -q     # 离线，MockTransport，不联网
```
