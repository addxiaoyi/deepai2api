# 免费模型渠道调研：哪些适合反代 + 注册机

> 目的：给出「下一个该做哪条线」的决策依据，而不是一份链接清单。
> 机器可读版本在 [`src/line-candidates.mjs`](../src/line-candidates.mjs)，
> 命令：`node src/lines-cli.mjs list | show <id> | plan <id> | probe <id>`。

## 0. 结论先行

> ✅ **状态：前五条 + 兜底线已全部接入**（`dcg.pool/agnes@1` … `dcg.pool/pollinations@1`），
> 见 [`docs/AGNES-POOL.md`](AGNES-POOL.md) 等六份文档与 `src/openai-lines.mjs`。

**能靠注册机批量拿、且值得反代的，只有 5 条**（其余要么要卡/手机号，要么额度小到摊不平）：

| 优先级 | 线 | 理由 | 门槛 |
| --- | --- | --- | --- |
| ① | **Agnes AI**（`api.agnes-ai.cn`） | 2026-06 起**无限期免费、不限 token**、约 20 RPM、OpenAI 全兼容（stream+tools） | 纯邮箱，无手机号、无卡 |
| ② | **Cerebras Cloud** | 30 RPM / **14,400 RPD** / 60k TPM，OpenAI 全兼容，竞争远小于 Groq | 邮箱 + 验证信 |
| ③ | **OpenCode Zen** | 30 RPM / 500 RPD，`-free` 后缀模型不限额度，模型新（MiMo、Nemotron） | 邮箱 + 验证信 |
| ④ | **Requesty** | 12 个免费模型共享 200 RPD，聚合网关自带上游兜底 | 邮箱 + 验证信 |
| ⑤ | **UnoRouter** | 190+ 个 `:free` 模型，还支持 Anthropic 形状；单模型 1 RPM，靠**模型轮换**摊吞吐 | 邮箱 / Discord / GitHub |

再补两条特殊位：

- **Pollinations AI**：**根本不用注册**，按 IP 10 RPM / 500 RPD 直连。适合当"号池空了也能跑"的兜底线（`signup.methods=["none"]`）。
- **LLM7.io**：拿免费 token 只要邮箱、无验证信，30 RPM，改造成本最低，适合先练手。

**明确不值得做的**：Kluster（绑卡）、蚂蚁百灵（绑支付宝实名）、Runcrate / GitHub Models（GitHub 号需满 30 天或封号连锁）、Cloudflare（自家协议 + 1 万 token/天）、Hugging Face（$0.10/月）、Synap（**全网共享上限**，多号不加额度，做号池无意义）。

## 1. 完整登记表

分数 = `0.40×automation + 0.35×relay + 0.25×quota − penalty`（penalty 是热门度：越热封得越快，号池寿命越短）。

| 分数 | 线 | 类别 | 免费额度 | 注册门槛 | OpenAI | 结论 |
| --- | --- | --- | --- | --- | --- | --- |
| 4.8 | Agnes AI | cn | 20 RPM / — RPD | email + 邮箱验证 | ✅ | build |
| 4.05 | UnoRouter | aggregator | 1 RPM / — RPD | email/discord/github + 邮箱验证 | ✅ | build |
| 3.9 | AnyAPI | aggregator | 20 RPM / 200 RPD | email/google + 邮箱验证 | ✅ | watch |
| 3.9 | CrowLLM | aggregator | — RPM / — RPD | email/google + 邮箱验证 | ✅ | build |
| 3.9 | OpenCode Zen | cn | 30 RPM / 500 RPD | email/github + 邮箱验证 | ✅ | build |
| 3.9 | Requesty | aggregator | — RPM / 200 RPD | email/google/github + 邮箱验证 | ✅ | build |
| 3.85 | Cerebras Cloud | inference | 30 RPM / 14400 RPD | email/google + 邮箱验证 | ✅ | build |
| 3.8 | NVIDIA NIM | inference | 40 RPM / — RPD | email/google + 邮箱验证 | ✅ | build |
| 3.75 | Pollinations AI | aggregator | 10 RPM / 500 RPD | none/email | ✅ | build |
| 3.65 | Groq Cloud | inference | 30 RPM / 14400 RPD | email/google/github + 邮箱验证 | ✅ | build |
| 3.65 | 幻城网安 | cn | — RPM / — RPD | email + 邮箱验证 | ✅ | watch |
| 3.6 | LLM7.io | inference | 30 RPM / — RPD | email | ✅ | build |
| 3.55 | Api.Airforce | aggregator | — RPM / — RPD | email/discord | ✅ | watch |
| 3.4 | Mistral La Plateforme | firstparty | 60 RPM / — RPD（1B tokens/月） | email/google/github + 邮箱验证 + 手机号 | ✅ | build |
| 3.35 | Google AI Studio（Gemini） | firstparty | 15 RPM / 1500 RPD | google | ✅ | build |
| 3.35 | 魔搭 ModelScope | cn | 签到 250 积分/日 | email/phone + 手机号 | ✅ | build |
| 3.3 | OpenRouter（`:free`） | aggregator | 20 RPM / 200 RPD | email/google/github + 邮箱验证 | ✅ | build |
| 3.15 | NovAI | cn | 5 RPM / 10 RPD | email + 邮箱验证 | ✅ | skip |
| 3.15 | Synap | inference | 10 RPM / 10 RPD（全网共享） | email/google + 邮箱验证 | ✅ | skip |
| 3.1 | 商汤日日新 | cn | 公测免费（积分制） | phone + 手机号 | ✅ | watch |
| 3.05 | 智谱 GLM | firstparty | Flash 永久免费不限 token + 2000 万 | email/phone + 手机号 | ✅ | build |
| 2.95 | GitHub Models | inference | 15 RPM / 150 RPD | github | ✅ | watch |
| 2.85 | Cohere | firstparty | 20 RPM / 33 RPD | email/google + 邮箱验证 | ⚠️ | watch |
| 2.8 | Runcrate | inference | 每周额度 | github（号需 ≥30 天） | ✅ | skip |
| 2.7 | Hugging Face | inference | $0.10/月 | email/github/google | ✅ | skip |
| 2.5 | 硅基流动 | cn | 小模型 + ASR 免费 | email/phone + 手机号 | ✅ | watch |
| 2.45 | 蚂蚁百灵 | cn | 50 万 token/天 | email + 支付宝 | ✅ | skip |
| 2.45 | Kluster AI | inference | 未公开 | email/google + 绑卡 | ✅ | skip |
| 1.95 | Cloudflare Workers AI | inference | 1 万 neurons/天 | email + Turnstile | ⚠️ | skip |

## 2. 重要判断：不是每条都要"新建一条线"

本仓库已经有 **`providers` 线 + `provider-catalog`**（`/p/<provider>/v1/*`）。所以分两种落法：

| 落法 | 适用 | 工作量 |
| --- | --- | --- |
| **A. 加进 `providers` 线**（只填 catalog + key 来源） | 纯 OpenAI 兼容、无自家签名的付费网关/聚合站（Requesty、UnoRouter、AnyAPI、CrowLLM、OpenCode Zen、Agents 类） | 半天，无需新 worker |
| **B. 独立一条线**（worker + pool + relay + panel + loop 登记） | 有自己的注册流程/发 Key 接口/会话体系（Agnes、Cerebras、Groq、Gemini、UnoRouter 的 Better-Auth 站） | 1-2 天（参考 `jevai` / `cleverai`） |

`node src/lines-cli.mjs plan <id>` 会直接吐出 B 方案的文件清单 + 7 步 checklist。

## 3. 接入顺序（✅ 已按此顺序落地）

```
第一批（邮箱就能跑，先铺量）
  1. agnes        —— 无限免费，先做，价值最高
  2. cerebras     —— 14.4k RPD，反代最稳
  3. opencode-zen —— 500 RPD，模型新
  4. requesty     —— 200 RPD，聚合兜底
  5. llm7         —— 30 RPM，练手 + 便宜

第二批（有手机号/第三方账号成本，看号源价格）
  6. zhipu        —— Flash 永久免费，但每号要手机号
  7. modelscope   —— 签到制，worker 要加"签到→积分"
  8. gemini       —— 额度最大，但要养 Google 号

第三批（只做兜底）
  9. pollinations —— 免注册，IP 限速，当最后一道保险
  10. unorouter   —— 1 RPM/模型，靠轮换；连接不稳，重试要厚
```

## 4. 每条线要做对的三件事

1. **一出口一号**（硬性）：OpenRouter / Groq / Cerebras 这类同 IP 多号必封。号池文件里的 `proxy` 字段要强制记录，注册与反代走同一出口。
2. **模型名漂移**：聚合站的 `:free` 后缀模型随时下架。反代层要能 404 时**自动换同族模型**，而不是把错误抛给下游（`jevai-relay` 已经做了 `upstream_endpoint_missing` 的映射，新线照抄）。
3. **额度账**：`RPD > 1000` 的线才值得长期跑（Cerebras/Groq 级）；`10-50 RPD` 的线只能当补充，且**必须按号轮询**，否则一个号被打满就整批 429。

## 5. 怎么验证一条线还活着

```bash
# 打上游 /models，看是 401（正常，要求鉴权）还是 404（base 写错了）
node src/lines-cli.mjs probe cerebras

# 看这条线要建哪些文件、走哪些步骤
node src/lines-cli.mjs plan agnes

# 只看"邮箱就能跑"的
node src/lines-cli.mjs list --only email
```

## 6. 数据来源与时效

| 来源 | 用途 |
| --- | --- |
| [awesome-free-llm-apis](https://github.com/amardeeplakshkar/awesome-free-llm-apis)（2026-03 校验） | 永久免费档、RPM/RPD、OpenAI 兼容、速度档 |
| [free-llm-api-cn](https://github.com/HughPython/free-llm-api-cn)（2026-10 更新） | 国内站点：免绑卡、签到制、积分制、手机号要求 |
| 各站官方 docs | base URL、模型名、限流页（每条 `evidence` 里都有直链） |

限流与政策变化很快，"免费"随时可能收回 —— 所以每条都带 `evidence`，并且 `probe` 能随时重测，
不要把这份表当承诺，当成**待验证假设**。
