# Pollinations 号池（`dcg.pool/pollinations@1`）

- 上游基址：`https://gen.pollinations.ai/v1`
- Key 前缀：`（匿名，不需要 Key）`
- 注册门槛：**不需要注册**（匿名线）
- 免费额度：免注册，按 IP 限速 10 RPM / 500 RPD
- worker：`tools/pollinations-reg`（`-m pollinations_reg`）
- 池文件：`$DEEPAI_GATEWAY_DATA/pollinations-pool.json`
- 反代：`/pollinations/v1/*`
- 接入导出：`data/keys/pollinations.txt`（一行一把）、`data/keys/pollinations.json`（含 `openai_base`）

## 为什么是这条线

Pollinations：免注册，按 IP 限速 10 RPM / 500 RPD；**不需要注册**（匿名线）。

## 用法

```bash
# worker（站点探测；端点不对就改 site.py）
cd tools/pollinations-reg && python -m pollinations_reg doctor
python -m pollinations_reg run -n 3 -c 1 --mail-provider imap --mail-domain example.com

# 网关侧
node src/pollinations-cli.mjs register -n 3 --mail-domain example.com
node src/pollinations-cli.mjs keys
node src/pollinations-cli.mjs apply          # 写 data/keys/pollinations.{txt,json}
```

## 接口

```
GET    /api/admin/pollinations/status
POST   /api/admin/pollinations/register            {count, concurrency, mailProvider, mailDomains[], proxies[]}
POST   /api/admin/pollinations/register/stop
GET    /api/admin/pollinations/pool?limit=200
POST   /api/admin/pollinations/pool/import         {email, apiKey}
POST   /api/admin/pollinations/pool/export?format=keys|jsonl|csv|pool[&download=1]
DELETE /api/admin/pollinations/pool/{id}
GET    /api/admin/pollinations/keys
POST   /api/admin/pollinations/apply
GET    /api/admin/pollinations/connect
GET    /pollinations/v1/models                     （需要 sk-dcg-… 网关 Key）
POST   /pollinations/v1/chat/completions
```

## 端点可信度

注册与发 Key 的路径来自公开文档/清单，**第一次跑先用 `doctor` 校验**；
不对就改 `tools/pollinations-reg/pollinations_reg/site.py` 里的 `signup_path` / `key_candidates`，
逻辑不用动（全部逻辑在 `tools/linekit`）。

## 与其它线的关系

六条 OpenAI 兼容线共用 `src/openai-lines.mjs` 一套实现：注册控制器、号池、反代都只有一份代码，
每条线只是一份 `defineLine(...)` 描述。这也是为什么这六条能一次接进来。

## 验证

```bash
node scripts/check-loop.mjs
node --test tests/openai-lines.test.mjs tests/lines-routes.test.mjs
python -m pytest tools/pollinations-reg/tests -q
```
