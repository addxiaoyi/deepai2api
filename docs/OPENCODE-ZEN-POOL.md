# OpenCode Zen 号池（`dcg.pool/opencode-zen@1`）

- 上游基址：`https://opencode.ai/zen/v1`
- Key 前缀：`sk-`　免费后缀：`-free`
- 注册门槛：邮箱 + 验证信
- 免费额度：30 RPM / 500 RPD，-free 模型不限额度
- worker：`tools/opencode-zen-reg`（`-m opencode_zen_reg`）
- 池文件：`$DEEPAI_GATEWAY_DATA/opencode-zen-pool.json`
- 反代：`/opencode-zen/v1/*`
- 接入导出：`data/keys/opencode-zen.txt`（一行一把）、`data/keys/opencode-zen.json`（含 `openai_base`）

## 为什么是这条线

OpenCode Zen：30 RPM / 500 RPD，-free 模型不限额度；邮箱 + 验证信。

## 用法

```bash
# worker（站点探测；端点不对就改 site.py）
cd tools/opencode-zen-reg && python -m opencode_zen_reg doctor
python -m opencode_zen_reg run -n 3 -c 1 --mail-provider imap --mail-domain example.com

# 网关侧
node src/opencode-zen-cli.mjs register -n 3 --mail-domain example.com
node src/opencode-zen-cli.mjs keys
node src/opencode-zen-cli.mjs apply          # 写 data/keys/opencode-zen.{txt,json}
```

## 接口

```
GET    /api/admin/opencode-zen/status
POST   /api/admin/opencode-zen/register            {count, concurrency, mailProvider, mailDomains[], proxies[]}
POST   /api/admin/opencode-zen/register/stop
GET    /api/admin/opencode-zen/pool?limit=200
POST   /api/admin/opencode-zen/pool/import         {email, apiKey}
POST   /api/admin/opencode-zen/pool/export?format=keys|jsonl|csv|pool[&download=1]
DELETE /api/admin/opencode-zen/pool/{id}
GET    /api/admin/opencode-zen/keys
POST   /api/admin/opencode-zen/apply
GET    /api/admin/opencode-zen/connect
GET    /opencode-zen/v1/models                     （需要 sk-dcg-… 网关 Key）
POST   /opencode-zen/v1/chat/completions
```

## 端点可信度

注册与发 Key 的路径来自公开文档/清单，**第一次跑先用 `doctor` 校验**；
不对就改 `tools/opencode-zen-reg/opencode_zen_reg/site.py` 里的 `signup_path` / `key_candidates`，
逻辑不用动（全部逻辑在 `tools/linekit`）。

## 与其它线的关系

六条 OpenAI 兼容线共用 `src/openai-lines.mjs` 一套实现：注册控制器、号池、反代都只有一份代码，
每条线只是一份 `defineLine(...)` 描述。这也是为什么这六条能一次接进来。

## 验证

```bash
node scripts/check-loop.mjs
node --test tests/openai-lines.test.mjs tests/lines-routes.test.mjs
python -m pytest tools/opencode-zen-reg/tests -q
```
