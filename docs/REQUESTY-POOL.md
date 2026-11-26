# Requesty 号池（`dcg.pool/requesty@1`）

- 上游基址：`https://router.requesty.ai/v1`
- Key 前缀：`sk-`
- 注册门槛：邮箱 + 验证信
- 免费额度：12 个免费模型共享 200 RPD
- worker：`tools/requesty-reg`（`-m requesty_reg`）
- 池文件：`$DEEPAI_GATEWAY_DATA/requesty-pool.json`
- 反代：`/requesty/v1/*`
- 接入导出：`data/keys/requesty.txt`（一行一把）、`data/keys/requesty.json`（含 `openai_base`）

## 为什么是这条线

Requesty：12 个免费模型共享 200 RPD；邮箱 + 验证信。

## 用法

```bash
# worker（站点探测；端点不对就改 site.py）
cd tools/requesty-reg && python -m requesty_reg doctor
python -m requesty_reg run -n 3 -c 1 --mail-provider imap --mail-domain example.com

# 网关侧
node src/requesty-cli.mjs register -n 3 --mail-domain example.com
node src/requesty-cli.mjs keys
node src/requesty-cli.mjs apply          # 写 data/keys/requesty.{txt,json}
```

## 接口

```
GET    /api/admin/requesty/status
POST   /api/admin/requesty/register            {count, concurrency, mailProvider, mailDomains[], proxies[]}
POST   /api/admin/requesty/register/stop
GET    /api/admin/requesty/pool?limit=200
POST   /api/admin/requesty/pool/import         {email, apiKey}
POST   /api/admin/requesty/pool/export?format=keys|jsonl|csv|pool[&download=1]
DELETE /api/admin/requesty/pool/{id}
GET    /api/admin/requesty/keys
POST   /api/admin/requesty/apply
GET    /api/admin/requesty/connect
GET    /requesty/v1/models                     （需要 sk-dcg-… 网关 Key）
POST   /requesty/v1/chat/completions
```

## 端点可信度

注册与发 Key 的路径来自公开文档/清单，**第一次跑先用 `doctor` 校验**；
不对就改 `tools/requesty-reg/requesty_reg/site.py` 里的 `signup_path` / `key_candidates`，
逻辑不用动（全部逻辑在 `tools/linekit`）。

## 与其它线的关系

六条 OpenAI 兼容线共用 `src/openai-lines.mjs` 一套实现：注册控制器、号池、反代都只有一份代码，
每条线只是一份 `defineLine(...)` 描述。这也是为什么这六条能一次接进来。

## 验证

```bash
node scripts/check-loop.mjs
node --test tests/openai-lines.test.mjs tests/lines-routes.test.mjs
python -m pytest tools/requesty-reg/tests -q
```
