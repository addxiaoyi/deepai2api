# UnoRouter 号池（`dcg.pool/unorouter@1`）

- 上游基址：`https://api.unorouter.com/v1`
- Key 前缀：`sk-`　免费后缀：`:free`
- 注册门槛：邮箱 / Discord / GitHub
- 免费额度：190+ :free 模型，单模型 1 RPM，靠模型轮换
- worker：`tools/unorouter-reg`（`-m unorouter_reg`）
- 池文件：`$DEEPAI_GATEWAY_DATA/unorouter-pool.json`
- 反代：`/unorouter/v1/*`（也支持 Anthropic `/v1/messages`）
- 接入导出：`data/keys/unorouter.txt`（一行一把）、`data/keys/unorouter.json`（含 `openai_base`）

## 为什么是这条线

UnoRouter：190+ :free 模型，单模型 1 RPM，靠模型轮换；邮箱 / Discord / GitHub。

## 用法

```bash
# worker（站点探测；端点不对就改 site.py）
cd tools/unorouter-reg && python -m unorouter_reg doctor
python -m unorouter_reg run -n 3 -c 1 --mail-provider imap --mail-domain example.com

# 网关侧
node src/unorouter-cli.mjs register -n 3 --mail-domain example.com
node src/unorouter-cli.mjs keys
node src/unorouter-cli.mjs apply          # 写 data/keys/unorouter.{txt,json}
```

## 接口

```
GET    /api/admin/unorouter/status
POST   /api/admin/unorouter/register            {count, concurrency, mailProvider, mailDomains[], proxies[]}
POST   /api/admin/unorouter/register/stop
GET    /api/admin/unorouter/pool?limit=200
POST   /api/admin/unorouter/pool/import         {email, apiKey}
POST   /api/admin/unorouter/pool/export?format=keys|jsonl|csv|pool[&download=1]
DELETE /api/admin/unorouter/pool/{id}
GET    /api/admin/unorouter/keys
POST   /api/admin/unorouter/apply
GET    /api/admin/unorouter/connect
GET    /unorouter/v1/models                     （需要 sk-dcg-… 网关 Key）
POST   /unorouter/v1/chat/completions
```

## 端点可信度

注册与发 Key 的路径来自公开文档/清单，**第一次跑先用 `doctor` 校验**；
不对就改 `tools/unorouter-reg/unorouter_reg/site.py` 里的 `signup_path` / `key_candidates`，
逻辑不用动（全部逻辑在 `tools/linekit`）。

## 与其它线的关系

六条 OpenAI 兼容线共用 `src/openai-lines.mjs` 一套实现：注册控制器、号池、反代都只有一份代码，
每条线只是一份 `defineLine(...)` 描述。这也是为什么这六条能一次接进来。

## 验证

```bash
node scripts/check-loop.mjs
node --test tests/openai-lines.test.mjs tests/lines-routes.test.mjs
python -m pytest tools/unorouter-reg/tests -q
```
