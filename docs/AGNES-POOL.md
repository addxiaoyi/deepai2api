# Agnes AI 号池（`dcg.pool/agnes@1`）

免费额度最好的一条线：**无限期免费、不限 token、约 20 RPM、OpenAI 全兼容**，注册只要邮箱。

- 控制台后端：`https://platform-backend.agnes-ai.com`（`/api/*`）
- 模型 API：`https://api.agnes-ai.cn/v1`
- Key 前缀：`sk-`
- 注册形态：**邮箱验证码**（不是 Better Auth）
- worker：`tools/agnes-reg`（`-m agnes_reg`）
- 池文件：`$DEEPAI_GATEWAY_DATA/agnes-pool.json`
- 反代：`/agnes/v1/*`
- 接入导出：`data/keys/agnes.{txt,txt.json}`

## 1. 上游协议（来自真实注册脚本核对）

| 步骤 | 请求 | 说明 |
| --- | --- | --- |
| 1 发码 | `GET /api/verification?email=<e>&purpose=register` | `{"code":200}`，6 位验证码发到邮箱 |
| 2 收码 | 邮箱 | 从正文取 6 位数字（`\b\d{6}\b`） |
| 3 注册 | `POST /api/user/register` `{email,password,password_confirm,code}` | `{"code":200,"data":{"access_token":…,"user":{"display_id","username","email"}}}` |
| 4 建 Key | `POST /api/token`，`Authorization: Bearer <access_token>`，`{name, api_key_profile:"default"}` | `{"code":200,"data":{"key":"sk-…"}}` |

要点：

- **发码要腾讯云验证码**（2026-11 实测）：不带票据时
  `GET /api/verification` 回 `{"code":400,"message":"Captcha verification is required"}`。
  前端用的是腾讯云验证码（`NEXT_PUBLIC_TENCENT_CAPTCHA_REGION=cn`），
  票据就是 query 里的 `ticket` + `randstr`，所以真实发码 URL 是：
  `/api/verification?email=…&purpose=register&ticket=…&randstr=…`
- 响应统一是 `{code, message, data}`，**HTTP 200 也可能是业务失败**（`code != 200`）——
  worker 两种都判。
- **同域名注册有频率限制**：`too many registration from this domain`。所以 worker 会
  **按域名轮换**（`linekit.runner._next_domain`），一个域名被打满就换下一个。
- 建 Key 用 `api_key_profile: "default"`（脚本里就是写死的），worker 会带这个字段。
- 不需要手机号、不需要绑卡。

## 2. 用法

```bash
cd tools/agnes-reg
python -m agnes_reg doctor        # 探针：backend / 模型 API / 号池计数

# 验证码三种取法（--captcha）：
#   browser  用 Camoufox 打开 /login、点"发送验证码"，从网络请求里抓 ticket/randstr（默认 auto 会走这条）
#   manual   自己给 --ticket / --randstr
#   none     站点临时关掉校验时用
python -m agnes_reg run -n 5 -c 1 --mail-provider imap --mail-domain example.com --captcha browser
python -m agnes_reg run -n 1 --captcha manual --ticket <t> --randstr <r>

python -m agnes_reg keys
python -m agnes_reg export --format keys

# 网关侧等价
node src/agnes-cli.mjs register -n 5 --mail-domain example.com
node src/agnes-cli.mjs keys && node src/agnes-cli.mjs apply
```

邮箱要求：**能收信的自有域名**（一次性邮箱很多站会拒；Agnes 对同域名高频注册会限流，
所以备 2-3 个域名轮换更稳）。

## 3. 反代

```
GET  /agnes/v1/models
POST /agnes/v1/chat/completions
```

下游只看到 `sk-dcg-…`；401/403/429/5xx 会让该账号进冷却并换号（401/403 冷却 10 分钟，
429 冷却 1 分钟），400/404 不怪 Key。SSE 直通。

## 4. 接口

```
GET    /api/admin/agnes/status
POST   /api/admin/agnes/register            {count, concurrency, mailProvider, mailDomains[], proxies[]}
POST   /api/admin/agnes/register/stop
GET    /api/admin/agnes/pool?limit=200
POST   /api/admin/agnes/pool/import         {email, apiKey}
POST   /api/admin/agnes/pool/export?format=keys|jsonl|csv|pool[&download=1]
GET    /api/admin/agnes/keys
POST   /api/admin/agnes/apply
GET    /api/admin/agnes/connect
DELETE /api/admin/agnes/pool/{id}
```

## 5. 验证

```bash
python -m pytest tools/agnes-reg/tests -q     # 四步流程 + 域名限流 + 错误码，全离线
node scripts/check-loop.mjs
node --test tests/lines-routes.test.mjs
```
