# Cerebras 号池（`dcg.pool/cerebras@1`）

- 上游基址：`https://api.cerebras.ai/v1`
- Key 前缀：`csk-`
- 注册门槛：邮箱 + 验证信；反代最稳
- 免费额度：30 RPM / 14,400 RPD / 60k TPM
- worker：`tools/cerebras-reg`（`-m cerebras_reg`）
- 池文件：`$DEEPAI_GATEWAY_DATA/cerebras-pool.json`
- 反代：`/cerebras/v1/*`
- 接入导出：`data/keys/cerebras.txt`（一行一把）、`data/keys/cerebras.json`（含 `openai_base`）

## 为什么是这条线

Cerebras：30 RPM / 14,400 RPD / 60k TPM；邮箱 + 验证信；反代最稳。

## 用法

```bash
# worker（站点探测；端点不对就改 site.py）
cd tools/cerebras-reg && python -m cerebras_reg doctor
python -m cerebras_reg run -n 3 -c 1 --mail-provider imap --mail-domain example.com

# 网关侧
node src/cerebras-cli.mjs register -n 3 --mail-domain example.com
node src/cerebras-cli.mjs keys
node src/cerebras-cli.mjs apply          # 写 data/keys/cerebras.{txt,json}
```

## 接口

```
GET    /api/admin/cerebras/status
POST   /api/admin/cerebras/register            {count, concurrency, mailProvider, mailDomains[], proxies[]}
POST   /api/admin/cerebras/register/stop
GET    /api/admin/cerebras/pool?limit=200
POST   /api/admin/cerebras/pool/import         {email, apiKey}
POST   /api/admin/cerebras/pool/export?format=keys|jsonl|csv|pool[&download=1]
DELETE /api/admin/cerebras/pool/{id}
GET    /api/admin/cerebras/keys
POST   /api/admin/cerebras/apply
GET    /api/admin/cerebras/connect
GET    /cerebras/v1/models                     （需要 sk-dcg-… 网关 Key）
POST   /cerebras/v1/chat/completions
```

## 端点可信度

注册与发 Key 的路径来自公开文档/清单，**第一次跑先用 `doctor` 校验**；
不对就改 `tools/cerebras-reg/cerebras_reg/site.py` 里的 `signup_path` / `key_candidates`，
逻辑不用动（全部逻辑在 `tools/linekit`）。

## 与其它线的关系

六条 OpenAI 兼容线共用 `src/openai-lines.mjs` 一套实现：注册控制器、号池、反代都只有一份代码，
每条线只是一份 `defineLine(...)` 描述。这也是为什么这六条能一次接进来。

## 验证

```bash
node scripts/check-loop.mjs
node --test tests/openai-lines.test.mjs tests/lines-routes.test.mjs
python -m pytest tools/cerebras-reg/tests -q
```
