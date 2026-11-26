# Gitee AI（模力方舟）号池（`dcg.pool/giteeai@1`）

把 Gitee AI 的访问令牌池化，并包成 **OpenAI Images 形状** 的反代面。

- 上游：`https://ai.gitee.com/v1`（`/images/generations`、`/models`）
- 默认模型：`z-image-turbo`（文生图），编辑用 `Qwen-Image-Edit`
- 令牌前缀：无统一前缀（Gitee 个人访问令牌，常见 32 位十六进制）
- worker：`tools/giteeai-reg`（`-m giteeai_reg`）
- 池文件：`$DEEPAI_GATEWAY_DATA/giteeai-pool.json`
- 反代：`/giteeai/v1/*`（`POST /giteeai/v1/images/generations`）
- 接入导出：`data/keys/giteeai.{txt,json}`

## 为什么没有"注册"这一步

Gitee 的访问令牌是在 **gitee.com → 设置 → 私人令牌** 里手动创建的（或走 OAuth 授权），
没有公开的机器注册/签发接口。所以这条线的自动化边界是：

| 环节 | 谁做 |
| --- | --- |
| 建 Gitee 账号 + 生成令牌 | 人工 / Gitee 账号池 |
| 令牌校验（只读 `/models`，不消耗生图额度） | `giteeai_reg run` |
| 入池 + 发网关 Key + 导出接入描述 | 网关（本仓库） |
| 出图 / 轮询 / 故障切换 / 额度冷却 | `/giteeai/v1/*` 反代 |

## 用法

```bash
# 1) 校验并入池（一行一个令牌，# 开头是注释）
node src/giteeai-cli.mjs import tokens.txt
node src/giteeai-cli.mjs verify <token>        # 单枚校验
node src/giteeai-cli.mjs keys                  # 打印池里的令牌
node src/giteeai-cli.mjs apply                 # 写 data/keys/giteeai.{txt,json}

# worker 直连（等价）
cd tools/giteeai-reg
python -m giteeai_reg run --tokens-file tokens.txt --json
python -m giteeai_reg doctor
```

## 反代

```bash
curl -X POST http://127.0.0.1:8787/giteeai/v1/images/generations \
  -H "Authorization: Bearer sk-dcg-…" \
  -H "Content-Type: application/json" \
  -d '{"model":"z-image-turbo","prompt":"a cat","n":1,"size":"1024x1024"}'
```

- 下游只看到网关的 `sk-dcg-…`；上游换 Gitee 令牌。
- `401/403` → 令牌标记失效并换下一个；`429/5xx` → 短冷却换令牌重试；`400/422`（参数/审核）
  直接把错误返回，不浪费别的令牌。
- 模型名不在上游时回 `model_not_found`，并提示先 `GET /giteeai/v1/models`。

## 与 giteeai2api 的关系

`sources/giteeai2api`（Go 版）把 Gitee 的图生接口适配成 OpenAI Images，自带管理台与令牌池。
本仓库把同一件事做成了**这条线**：区别是它嵌进统一网关（统一的 `/api/admin/*`、
统一的 `sk-dcg-` 鉴权、统一的号池与导出）。两者可以并存：

- 想单独部署一套带 UI 的图床网关 → 用 `giteeai2api`（`docker compose up`）。
- 想和别的模型线共用一个网关、一套 Key → 用这条线。

## 验证

```bash
node scripts/check-loop.mjs                       # 这条线在矩阵里
node --test tests/openai-lines.test.mjs tests/lines-routes.test.mjs
python -m pytest tools/giteeai-reg/tests -q
```
