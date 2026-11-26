// 候选线上游登记表 —— 「下一个该做哪条线」的决策依据。
//
// 每条记录只写**能验证的事实**（来自官方文档 / 实测清单，附 evidence 链接），
// 再加上三个主观但可解释的评分维度：
//
//   automation  0-5  能不能用注册机跑：5=邮箱+无验证码+接口发 Key，1=要绑卡/实名，0=邀请制
//   relay       0-5  反代契合度：5=OpenAI 全兼容（stream+tools），3=兼容但缺 tools，1=自家协议
//   quota       0-5  免费额度大小：5=永久不限 / 1B tokens 级，4=千级 RPD，3=200 RPD，2=10-50 RPD，1=象征性
//
// score = 0.40*automation + 0.35*relay + 0.25*quota - penalty
// penalty 是"热门度惩罚"：越热的站，批量注册被封得越快，号池寿命越短。
import { writeFileSync, mkdirSync } from "node:fs";
import { dirname, resolve } from "node:path";

export const DIMENSIONS = ["automation", "relay", "quota"];
export const WEIGHTS = { automation: 0.4, relay: 0.35, quota: 0.25 };

/** @typedef {'firstparty'|'inference'|'aggregator'|'cn'} Category */

export const CANDIDATES = [
  /* ---------------------------------------------------------- 一线大厂自营 */
  {
    id: "gemini",
    name: "Google AI Studio（Gemini）",
    category: "firstparty",
    baseUrl: "https://generativelanguage.googleapis.com/v1beta/openai/",
    modelsUrl: "/models",
    keyPrefix: "AIza",
    free: { models: ["gemini-2.5-pro", "gemini-2.0-flash"], rpm: 15, rpd: 1500, note: "Flash 15 RPM/1500 RPD；2.5 Pro 2 RPM/50 RPD" },
    signup: { methods: ["google"], emailVerification: false, captcha: "google", card: false, phone: false, regionBlock: ["EU", "UK", "CH"] },
    api: { openaiCompat: "full", stream: true, tools: true, anthropic: false },
    automation: 3,
    relay: 5,
    quota: 4,
    penalty: 0.6,
    verdict: "build",
    reason:
      "额度最大的免费档（1500 RPD），OpenAI 兼容完整。门槛是 Google 账号：号池要养 Google 号，且同一浏览器指纹/出口大量开 Key 会触发 abuse 审核。",
    evidence: ["https://ai.google.dev/gemini-api/docs/rate-limits", "https://ai.google.dev/gemini-api/docs/available-regions"],
  },
  {
    id: "mistral",
    name: "Mistral La Plateforme",
    category: "firstparty",
    baseUrl: "https://api.mistral.ai/v1",
    modelsUrl: "/models",
    free: { models: ["mistral-small-latest", "mistral-large-latest"], rpm: 60, rpd: 0, note: "免费档按月 token 计：1B tokens/月" },
    signup: { methods: ["email", "google", "github"], emailVerification: true, captcha: "none", card: false, phone: true },
    api: { openaiCompat: "full", stream: true, tools: true, anthropic: false },
    automation: 2,
    relay: 5,
    quota: 5,
    penalty: 0.4,
    verdict: "build",
    reason: "1B tokens/月是免费档里最肥的一个，但注册通常要手机验证；一旦能过，单号价值极高。",
    evidence: ["https://docs.mistral.ai/deployment/laplateforme/tier/"],
  },
  {
    id: "zhipu",
    name: "智谱 GLM 开放平台",
    category: "firstparty",
    baseUrl: "https://open.bigmodel.cn/api/paas/v4",
    modelsUrl: "/models",
    free: { models: ["glm-4-flash", "glm-4.7-flash", "glm-4.6v-flash", "cogview-3-flash"], rpm: 0, rpd: 0, note: "Flash 系列永久免费不限 token，限并发；注册另送 2000 万 token" },
    signup: { methods: ["email", "phone"], emailVerification: true, captcha: "slide", card: false, phone: true },
    api: { openaiCompat: "full", stream: true, tools: true, anthropic: false },
    automation: 2,
    relay: 5,
    quota: 4,
    penalty: 0.5,
    verdict: "build",
    reason: "Flash 永久免费 + 能用 tools，反代价值高；门槛是手机号（国内号容易），适合走手机号批量的号池。",
    evidence: ["https://open.bigmodel.cn/pricing", "https://docs.bigmodel.cn/"],
  },
  {
    id: "cohere",
    name: "Cohere",
    category: "firstparty",
    baseUrl: "https://api.cohere.ai/compatibility/v1",
    modelsUrl: "/models",
    free: { models: ["command-a", "command-r-plus"], rpm: 20, rpd: 33, note: "1000 次/月试用额度" },
    signup: { methods: ["email", "google"], emailVerification: true, captcha: "none", card: false, phone: false },
    api: { openaiCompat: "partial", stream: true, tools: true, anthropic: false },
    automation: 4,
    relay: 3,
    quota: 2,
    penalty: 0.3,
    verdict: "watch",
    reason: "注册门槛低（邮箱即可），但免费额度太小、且是 partial 兼容，只适合当兜底。",
    evidence: ["https://docs.cohere.com/docs/rate-limits"],
  },

  /* ------------------------------------------------------------- 推理平台 */
  {
    id: "groq",
    name: "Groq Cloud",
    category: "inference",
    baseUrl: "https://api.groq.com/openai/v1",
    modelsUrl: "/models",
    free: { models: ["llama-3.3-70b-versatile", "qwen/qwq-32b", "moonshotai/kimi-k2"], rpm: 30, rpd: 14400, tpm: 6000, note: "30 RPM / 14400 RPD，LPU 极快" },
    signup: { methods: ["email", "google", "github"], emailVerification: true, captcha: "none", card: false, phone: false },
    api: { openaiCompat: "full", stream: true, tools: true, anthropic: false },
    automation: 4,
    relay: 5,
    quota: 4,
    penalty: 0.7,
    verdict: "build",
    reason: "免费额度最实用的一个（14400 RPD + 极快），邮箱注册即可发 Key。缺点是太热，号池要快、且必须一出口一号。",
    evidence: ["https://console.groq.com/docs/rate-limits"],
  },
  {
    id: "cerebras",
    name: "Cerebras Cloud",
    category: "inference",
    baseUrl: "https://api.cerebras.ai/v1",
    modelsUrl: "/models",
    free: { models: ["llama-3.3-70b", "qwen-3-235b-a22b"], rpm: 30, rpd: 14400, tpm: 60000, note: "30 RPM / 14.4k RPD / 60k TPM" },
    signup: { methods: ["email", "google"], emailVerification: true, captcha: "none", card: false, phone: false },
    api: { openaiCompat: "full", stream: true, tools: true, anthropic: false },
    automation: 4,
    relay: 5,
    quota: 4,
    penalty: 0.5,
    verdict: "build",
    reason: "和 Groq 同档额度但竞争小得多，OpenAI 全兼容，最适合当第一条「新线」。",
    evidence: ["https://inference-docs.cerebras.ai/support/rate-limits"],
  },
  {
    id: "openrouter",
    name: "OpenRouter（:free 档）",
    category: "aggregator",
    baseUrl: "https://openrouter.ai/api/v1",
    modelsUrl: "/models",
    free: { models: ["deepseek/deepseek-r1:free", "qwen/qwen3-coder:free"], rpm: 20, rpd: 200, note: "带 :free 后缀的模型免费；20 RPM / 200 RPD" },
    signup: { methods: ["email", "google", "github"], emailVerification: true, captcha: "none", card: false, phone: false },
    api: { openaiCompat: "full", stream: true, tools: true, anthropic: true },
    automation: 4,
    relay: 5,
    quota: 3,
    penalty: 0.8,
    verdict: "build",
    reason: "一个 Key 打通 30+ 免费模型、还能出 Claude/OpenAI 形状，反代价值很高。但风控最严（同 IP 多号必封），必须真代理池 + 一出口一号。",
    evidence: ["https://openrouter.ai/docs/api-reference/limits"],
  },
  {
    id: "nvidia",
    name: "NVIDIA NIM",
    category: "inference",
    baseUrl: "https://integrate.api.nvidia.com/v1",
    modelsUrl: "/models",
    free: { models: ["meta/llama-3.3-70b-instruct", "qwen/qwen3-235b-a22b"], rpm: 40, rpd: 0, note: "40 RPM，credit 制会回补" },
    signup: { methods: ["email", "google"], emailVerification: true, captcha: "none", card: false, phone: false },
    api: { openaiCompat: "full", stream: true, tools: true, anthropic: false },
    automation: 4,
    relay: 5,
    quota: 3,
    penalty: 0.3,
    verdict: "build",
    reason: "邮箱注册拿 1000 credit、40 RPM，竞争低。缺点是用量到顶会静默降级，适合做备用池。",
    evidence: ["https://build.nvidia.com/", "https://docs.api.nvidia.com/nim/reference/llm-apis"],
  },
  {
    id: "github-models",
    name: "GitHub Models",
    category: "inference",
    baseUrl: "https://models.inference.ai.azure.com",
    modelsUrl: "/models",
    free: { models: ["gpt-4o", "deepseek-r1", "llama-3.3-70b"], rpm: 15, rpd: 150, note: "按模型档 10-15 RPM / 50-150 RPD" },
    signup: { methods: ["github"], emailVerification: false, captcha: "github", card: false, phone: false },
    api: { openaiCompat: "full", stream: true, tools: true, anthropic: false },
    automation: 3,
    relay: 5,
    quota: 2,
    penalty: 0.5,
    verdict: "watch",
    reason: "能白嫖 GPT-4o，但要 GitHub 账号 + PAT，且 GitHub 对批量小号极其敏感（封 GitHub 号连锁封 Key）。",
    evidence: ["https://docs.github.com/en/github-models"],
  },
  {
    id: "hf",
    name: "Hugging Face Serverless",
    category: "inference",
    baseUrl: "https://api-inference.huggingface.co/v1",
    modelsUrl: "/models",
    free: { models: ["meta-llama/Llama-3.3-70B-Instruct"], rpm: 0, rpd: 0, note: "$0.10/月 credit 自动回补" },
    signup: { methods: ["email", "github", "google"], emailVerification: true, captcha: "none", card: false, phone: false },
    api: { openaiCompat: "full", stream: true, tools: false, anthropic: false },
    automation: 4,
    relay: 3,
    quota: 1,
    penalty: 0.2,
    verdict: "skip",
    reason: "$0.10/月太少，共享队列冷启动慢，做号池的收益抵不上维护成本。",
    evidence: ["https://huggingface.co/docs/inference-providers/pricing"],
  },
  {
    id: "cloudflare",
    name: "Cloudflare Workers AI",
    category: "inference",
    baseUrl: "https://api.cloudflare.com/client/v4/accounts/{account_id}/ai/v1",
    modelsUrl: "/models",
    free: { models: ["@cf/meta/llama-3.3-70b-instruct-fp8-fast"], rpm: 0, rpd: 0, note: "10000 neurons/天（≈1 万输出 token）" },
    signup: { methods: ["email"], emailVerification: true, captcha: "turnstile", card: false, phone: false },
    api: { openaiCompat: "partial", stream: true, tools: false, anthropic: false },
    automation: 3,
    relay: 2,
    quota: 1,
    penalty: 0.2,
    verdict: "skip",
    reason: "要 account_id + Turnstile，且自家协议、神经元额度太小；投入产出比差。",
    evidence: ["https://developers.cloudflare.com/workers-ai/platform/pricing/"],
  },
  {
    id: "kluster",
    name: "Kluster AI",
    category: "inference",
    baseUrl: "https://api.kluster.ai/v1",
    modelsUrl: "/models",
    free: { models: ["deepseek-ai/DeepSeek-R1", "Qwen/Qwen3-235B-A22B"], rpm: 0, rpd: 0, note: "限流未公开" },
    signup: { methods: ["email", "google"], emailVerification: true, captcha: "none", card: true, phone: false },
    api: { openaiCompat: "full", stream: true, tools: true, anthropic: false },
    automation: 1,
    relay: 5,
    quota: 2,
    penalty: 0.2,
    verdict: "skip",
    reason: "要绑卡才能用免费额度，注册机拿不到卡；放弃。",
    evidence: ["https://docs.kluster.ai/get-started/rate-limits/"],
  },
  {
    id: "requesty",
    name: "Requesty",
    category: "aggregator",
    baseUrl: "https://router.requesty.ai/v1",
    modelsUrl: "/models",
    free: { models: ["nvidia/nemotron-3-ultra", "gemma-4-31b"], rpm: 0, rpd: 200, note: "12 个免费模型共享 200 RPD" },
    signup: { methods: ["email", "google", "github"], emailVerification: true, captcha: "none", card: false, phone: false },
    api: { openaiCompat: "full", stream: true, tools: true, anthropic: false },
    automation: 4,
    relay: 5,
    quota: 3,
    penalty: 0.2,
    verdict: "build",
    reason: "200 RPD 免费、OpenAI 全兼容、竞争小；聚合网关本身替我们兜住了上游波动。",
    evidence: ["https://www.requesty.ai/free-models"],
  },
  {
    id: "unorouter",
    name: "UnoRouter",
    category: "aggregator",
    baseUrl: "https://api.unorouter.com/v1",
    modelsUrl: "/models",
    free: { models: ["deepseek-v4-flash:free", "glm-5.2:free"], rpm: 1, rpd: 0, note: "190+ 个 :free 模型，单模型 1 RPM，靠轮换模型提吞吐" },
    signup: { methods: ["email", "discord", "github"], emailVerification: true, captcha: "none", card: false, phone: false },
    api: { openaiCompat: "full", stream: true, tools: true, anthropic: true },
    automation: 4,
    relay: 5,
    quota: 4,
    penalty: 0.3,
    verdict: "build",
    reason: "190+ 免费模型 + 支持 Anthropic 形状，模型轮换能把 1 RPM 摊成可用的总吞吐；连接不稳需要重试策略。",
    evidence: ["https://unorouter.com/token"],
  },
  {
    id: "crowllm",
    name: "CrowLLM",
    category: "aggregator",
    baseUrl: "https://crowllm.com/v1",
    modelsUrl: "/models",
    free: { models: ["grok-4.7:free", "glm-5.3-flash-thinking:free"], rpm: 0, rpd: 0, note: "31 个 :free 模型价格为 0；/api/pricing 匿名可读" },
    signup: { methods: ["email", "google"], emailVerification: true, captcha: "none", card: false, phone: false },
    api: { openaiCompat: "full", stream: true, tools: true, anthropic: true },
    automation: 4,
    relay: 5,
    quota: 3,
    penalty: 0.2,
    verdict: "build",
    reason: "免费货架里有 Grok 4.7 这种别处拿不到的模型，且 pricing/models 匿名可读 —— 探测脚本能在注册前先确认免费集还在。",
    evidence: ["https://crowllm.com/api/pricing"],
  },
  {
    id: "pollinations",
    name: "Pollinations AI",
    category: "aggregator",
    baseUrl: "https://gen.pollinations.ai/v1",
    modelsUrl: "/models",
    free: { models: ["openai", "openai-large", "gemini"], rpm: 10, rpd: 500, note: "匿名可用（10 RPM / 500 RPD），带 Key 提权" },
    signup: { methods: ["none", "email"], emailVerification: false, captcha: "none", card: false, phone: false },
    api: { openaiCompat: "full", stream: true, tools: false, anthropic: false },
    automation: 5,
    relay: 4,
    quota: 3,
    penalty: 0.4,
    verdict: "build",
    reason: "**不用注册**就能直接反代（按 IP 限速），等于零成本基线；要提权再上邮箱号池。适合当「没号时也能跑」的兜底线。",
    evidence: ["https://gen.pollinations.ai/", "https://github.com/pollinations/pollinations"],
  },
  {
    id: "llm7",
    name: "LLM7.io",
    category: "inference",
    baseUrl: "https://api.llm7.io/v1",
    modelsUrl: "/models",
    free: { models: ["deepseek-r1", "gpt-4o-mini"], rpm: 30, rpd: 0, note: "免费 token 把 15 RPM 提到 30 RPM" },
    signup: { methods: ["email"], emailVerification: false, captcha: "none", card: false, phone: false },
    api: { openaiCompat: "full", stream: true, tools: false, anthropic: false },
    automation: 5,
    relay: 3,
    quota: 3,
    penalty: 0.2,
    verdict: "build",
    reason: "拿免费 token 只需邮箱、几乎零门槛，适合立刻接一条低成本线。",
    evidence: ["https://token.llm7.io"],
  },
  {
    id: "anyapi",
    name: "AnyAPI",
    category: "aggregator",
    baseUrl: "https://api.anyapi.ai/v1",
    modelsUrl: "/models",
    free: { models: ["llama-3.3-70b", "qwen3-coder", "gpt-oss-120b"], rpm: 20, rpd: 200, note: "20 RPM / 200 RPD" },
    signup: { methods: ["email", "google"], emailVerification: true, captcha: "none", card: false, phone: false },
    api: { openaiCompat: "full", stream: true, tools: true, anthropic: false },
    automation: 4,
    relay: 5,
    quota: 3,
    penalty: 0.2,
    verdict: "watch",
    reason: "额度与 OpenRouter 同档但更小众；先在 Requesty/CrowLLM 上验证模型，再决定要不要铺。",
    evidence: ["https://anyapi.ai"],
  },
  {
    id: "airforce",
    name: "Api.Airforce",
    category: "aggregator",
    baseUrl: "https://api.airforce/v1",
    modelsUrl: "/models",
    free: { models: ["gpt-oss-120b", "glm-4.7-flash", "gpt-4o-mini"], rpm: 0, rpd: 0, note: "13 个免费模型，限流未公开" },
    signup: { methods: ["email", "discord"], emailVerification: false, captcha: "none", card: false, phone: false },
    api: { openaiCompat: "full", stream: true, tools: true, anthropic: true },
    automation: 4,
    relay: 5,
    quota: 2,
    penalty: 0.3,
    verdict: "watch",
    reason: "免费模型里有 GPT-4o-mini，但站点稳定性口碑一般，适合当第二条备用而不是主供。",
    evidence: ["https://api.airforce"],
  },

  /* --------------------------------------------------------- 国内（免绑卡） */
  {
    id: "agnes",
    name: "Agnes AI",
    category: "cn",
    baseUrl: "https://api.agnes-ai.cn/v1",
    modelsUrl: "/models",
    free: { models: ["agnes-3.0-flash", "agnes-2.5-flash"], rpm: 20, rpd: 0, note: "2026-06 起无限期免费，约 20 RPM，1M 上下文" },
    signup: { methods: ["email"], emailVerification: true, captcha: "none", card: false, phone: false },
    api: { openaiCompat: "full", stream: true, tools: true, anthropic: false },
    automation: 5,
    relay: 5,
    quota: 5,
    penalty: 0.2,
    verdict: "build",
    reason: "**纯邮箱注册 + 无手机号 + 免费不限 token（约 20 RPM）+ OpenAI 全兼容** —— 这是当前最适合做注册机的一条，优先级最高。",
    evidence: ["https://platform.agnes-ai.cn/"],
  },
  {
    id: "modelscope",
    name: "魔搭 ModelScope（推理 API）",
    category: "cn",
    baseUrl: "https://api-inference.modelscope.cn/v1",
    modelsUrl: "/models",
    free: { models: ["Qwen/Qwen3-235B-A22B", "deepseek-ai/DeepSeek-V3"], rpm: 0, rpd: 0, note: "每日签到 250 积分（24h 有效），按积分扣" },
    signup: { methods: ["email", "phone"], emailVerification: true, captcha: "slide", card: false, phone: true },
    api: { openaiCompat: "full", stream: true, tools: true, anthropic: false },
    automation: 2,
    relay: 5,
    quota: 4,
    penalty: 0.2,
    verdict: "build",
    reason: "额度稳（签到制）、模型全、OpenAI 兼容；但注册要手机号且签到给的是积分不是 token，worker 要额外做「签到→算积分」。",
    evidence: ["https://modelscope.cn/docs/model-service/API-Inference/intro"],
  },
  {
    id: "hcnsec",
    name: "幻城网安",
    category: "cn",
    baseUrl: "https://api.hcnsec.cn/v1",
    modelsUrl: "/models",
    free: { models: ["Qwen3.8-Flash-Next", "MiMo-V2.6-Flash", "space-bunny-free"], rpm: 0, rpd: 0, note: "每日领取额度（实测 1-500 浮动）" },
    signup: { methods: ["email"], emailVerification: true, captcha: "none", card: false, phone: false },
    api: { openaiCompat: "full", stream: true, tools: false, anthropic: false },
    automation: 4,
    relay: 4,
    quota: 3,
    penalty: 0.1,
    verdict: "watch",
    reason: "有永久免费模型 + 每日额度，邮箱即可；但额度浮动大，适合当补位池而非主供。",
    evidence: ["https://api.hcnsec.cn/sign-up"],
  },
  {
    id: "sensenova",
    name: "商汤日日新 SenseNova",
    category: "cn",
    baseUrl: "https://token.sensenova.cn/v1",
    modelsUrl: "/models",
    free: { models: ["sensenova-6.8-flash-lite"], rpm: 0, rpd: 0, note: "Token Plan 公测期免费，5h/周积分制" },
    signup: { methods: ["phone"], emailVerification: false, captcha: "slide", card: false, phone: true },
    api: { openaiCompat: "full", stream: true, tools: true, anthropic: false },
    automation: 2,
    relay: 4,
    quota: 4,
    penalty: 0.1,
    verdict: "watch",
    reason: "公测免费额度大，但只支持手机号注册且容易断流；等它转正再评估。",
    evidence: ["https://platform.sensenova.cn/"],
  },
  {
    id: "siliconflow",
    name: "硅基流动 SiliconFlow",
    category: "cn",
    baseUrl: "https://api.siliconflow.cn/v1",
    modelsUrl: "/models",
    free: { models: ["THUDM/GLM-Z1-9B-0414", "FunAudioLLM/SenseVoiceSmall"], rpm: 0, rpd: 0, note: "10B 以下一批模型免费（含 ASR）" },
    signup: { methods: ["email", "phone"], emailVerification: true, captcha: "slide", card: false, phone: true },
    api: { openaiCompat: "full", stream: true, tools: true, anthropic: false },
    automation: 2,
    relay: 4,
    quota: 2,
    penalty: 0.2,
    verdict: "watch",
    reason: "免费的都是小模型，主力模型要钱；只有 ASR 那块免费额度真正有用。",
    evidence: ["https://cloud.siliconflow.cn/models"],
  },
  {
    id: "opencode-zen",
    name: "OpenCode Zen",
    category: "cn",
    baseUrl: "https://opencode.ai/zen/v1",
    modelsUrl: "/models",
    free: { models: ["mimo-v2.5-free", "nemotron-3-ultra-free"], rpm: 30, rpd: 500, note: "-free 后缀模型免费，整站 30 RPM / 500 RPD" },
    signup: { methods: ["email", "github"], emailVerification: true, captcha: "none", card: false, phone: false },
    api: { openaiCompat: "full", stream: true, tools: true, anthropic: false },
    automation: 4,
    relay: 5,
    quota: 3,
    penalty: 0.2,
    verdict: "build",
    reason: "500 RPD 免费 + OpenAI 兼容 + 邮箱注册，和 Requesty 同一档但模型更新更快。",
    evidence: ["https://opencode.ai/zen/v1/models"],
  },
  {
    id: "antling",
    name: "蚂蚁百灵",
    category: "cn",
    baseUrl: "https://api.ant-ling.com/v1",
    modelsUrl: "/models",
    free: { models: ["Ling-3.0-flash-VL", "Ring-2.6-1T"], rpm: 0, rpd: 0, note: "每天 50 万 token，次日 2 点刷新" },
    signup: { methods: ["email"], emailVerification: true, captcha: "none", card: false, phone: false, alipay: true },
    api: { openaiCompat: "full", stream: true, tools: true, anthropic: false },
    automation: 1,
    relay: 4,
    quota: 3,
    penalty: 0.1,
    verdict: "skip",
    reason: "必须绑支付宝实名才能建 Key，注册机过不去；单号 50 万 token/天也不算大。",
    evidence: ["https://chat.ant-ling.com/open"],
  },
  {
    id: "novai",
    name: "NovAI（aiapi-pro）",
    category: "cn",
    baseUrl: "https://aiapi-pro.com/v1",
    modelsUrl: "/models",
    free: { models: ["glm-4.7-flash", "glm-4.6v-flash", "cogview3-flash"], rpm: 5, rpd: 10, note: "5 个免费模型共享 5 RPM / 10 RPD；另送 $2" },
    signup: { methods: ["email"], emailVerification: true, captcha: "none", card: false, phone: false },
    api: { openaiCompat: "full", stream: true, tools: false, anthropic: false },
    automation: 4,
    relay: 4,
    quota: 1,
    penalty: 0.1,
    verdict: "skip",
    reason: "10 RPD 太少，除非号池极便宜否则摊不平。",
    evidence: ["https://aiapi-pro.com/free"],
  },
  /* --------------------------------------- 需 GitHub 账号/周额度的两例外 */
  {
    id: "runcrate",
    name: "Runcrate",
    category: "inference",
    baseUrl: "https://api.runcrate.ai/v1",
    modelsUrl: "/models",
    free: { models: ["deepseek-v4.1-flash", "glm-5.3", "qwen3.8-flash"], rpm: 0, rpd: 0, note: "每 workspace 每周重置，额度看面板" },
    signup: { methods: ["github"], emailVerification: false, captcha: "github", card: false, phone: false, note: "GitHub 账号需 ≥30 天" },
    api: { openaiCompat: "full", stream: true, tools: true, anthropic: false },
    automation: 1,
    relay: 5,
    quota: 3,
    penalty: 0.1,
    verdict: "skip",
    reason: "要求 GitHub 账号满 30 天，等于把注册机变成「养号机」，成本远超收益。",
    evidence: ["https://www.runcrate.ai/inference/free"],
  },
  {
    id: "synap",
    name: "Synap",
    category: "inference",
    baseUrl: "https://synap402.com/v1",
    modelsUrl: "/models",
    free: { models: ["qwen3-coder-30b"], rpm: 10, rpd: 10, note: "5 万 token/天、10 RPD，且全网 30 万 token/天共享上限" },
    signup: { methods: ["email", "google"], emailVerification: true, captcha: "none", card: false, phone: false },
    api: { openaiCompat: "full", stream: true, tools: true, anthropic: false },
    automation: 4,
    relay: 4,
    quota: 1,
    penalty: 0.1,
    verdict: "skip",
    reason: "全网共享上限意味着号池叠不上去（多号不加额度），做注册机没意义。",
    evidence: ["https://synap402.com/api/account/free-tier"],
  },
];

/** 评分：0-5，扣掉热门度惩罚后仍可能为负（越负越别做）。 */
export function scoreLine(candidate) {
  const { automation = 0, relay = 0, quota = 0, penalty = 0 } = candidate;
  const base =
    WEIGHTS.automation * automation + WEIGHTS.relay * relay + WEIGHTS.quota * quota;
  return Math.round((base - penalty) * 100) / 100;
}

/** 按分数降序，同分时 automation 高的优先（好做比好用更重要，因为做不了就等于零）。 */
export function rankCandidates(rows = CANDIDATES) {
  return rows
    .map((row) => ({ ...row, score: scoreLine(row) }))
    .sort((a, b) => b.score - a.score || b.automation - a.automation || a.id.localeCompare(b.id));
}

export const byId = (id) => CANDIDATES.find((row) => row.id === id) ?? null;

/** 只要邮箱就能跑、且反代可用（5 或 4）的线：注册机的首选池。 */
export function emailOnlyPicks(rows = CANDIDATES) {
  return rankCandidates(rows).filter(
    (row) => row.signup.card === false && row.signup.phone === false && row.automation >= 4 && row.relay >= 4,
  );
}

/**
 * 需要「额外资产」的线：绑卡 / 手机号 / 支付宝实名，或者**没有邮箱入口**
 * （只能靠 Google、GitHub 这类第三方账号）。单独列出来评估成本。
 *
 * 注意：`methods` 里顺带支持 github 不算门槛 —— 只要有 email 这条路就是可自动化的。
 */
export function gatedPicks(rows = CANDIDATES) {
  return rankCandidates(rows).filter(
    (row) =>
      row.signup.card === true ||
      row.signup.phone === true ||
      row.signup.alipay === true ||
      !(row.signup.methods || []).includes("email"),
  );
}

/**
 * 生成"新增一条线"的落地清单（对应 docs/LOOP.md 的 7 步 checklist）。
 * 这里只产出计划文本，不写文件。
 */
export function workerPlan(id) {
  const row = byId(id);
  if (!row) return null;
  const slug = row.id;
  const needMail = row.signup.emailVerification;
  const needPhone = row.signup.phone;
  const needCard = row.signup.card;
  return {
    id: slug,
    name: row.name,
    score: scoreLine(row),
    verdict: row.verdict,
    files: [
      `tools/${slug}-reg/${slug.replace(/-/g, "_")}_reg/{protocol.py,config.py,store.py,runner.py,cli.py,push.py}`,
      `tools/${slug}-reg/tests/test_offline.py`,
      `src/${slug}-pool.mjs`,
      `src/${slug}-register.mjs`,
      `src/${slug}-relay.mjs`,
      `src/${slug}-cli.mjs`,
      `docs/${slug.toUpperCase()}-POOL.md`,
      `tests/${slug}-pool.test.mjs`,
      `tests/${slug}-routes.test.mjs`,
    ],
    steps: [
      `登记：scripts/check-loop.mjs 的 LINES 加一条（schema=dcg.pool/${slug}@1、worker=tools/${slug}-reg）`,
      `协议：确认注册端点与发 Key 端点（base=${row.baseUrl}）`,
      needMail
        ? "邮箱：复用 imap catch-all（一次性域名多数站会拒）"
        : "邮箱：无需验证，直接注册",
      needPhone ? "手机号：这条线要手机号，先确认号源成本再决定" : "手机号：不需要",
      needCard ? "绑卡：需要卡，注册机过不去 —— 直接放弃" : "绑卡：不需要",
      `反代：${row.api.openaiCompat === "full" ? "直通 /v1（OpenAI 全兼容）" : "需要字段映射（partial）"}；stream=${row.api.stream} tools=${row.api.tools}`,
      "接口：/api/admin/<line>/{status,register,register/stop,pool,pool/import,pool/export}",
      `验证：node scripts/check-loop.mjs && npm run test:loop && python -m pytest tools/${slug}-reg/tests -q`,
    ],
    docs: row.evidence,
  };
}

/** 导出成 markdown 表格（面板/文档都能用）。 */
export function toMarkdown(rows = CANDIDATES) {
  const ranked = rankCandidates(rows);
  const head = [
    "| 分数 | 线 | 类别 | 免费额度 | 注册门槛 | OpenAI | 结论 |",
    "| --- | --- | --- | --- | --- | --- | --- |",
  ];
  const body = ranked.map((row) => {
    const gate = [
      row.signup.methods.join("/"),
      row.signup.emailVerification ? "邮箱验证" : null,
      row.signup.phone ? "手机号" : null,
      row.signup.card ? "绑卡" : null,
      row.signup.alipay ? "支付宝" : null,
    ]
      .filter(Boolean)
      .join(" + ");
    const quota = `${row.free.rpm || "—"} RPM / ${row.free.rpd || "—"} RPD`;
    const compat = row.api.openaiCompat === "full" ? "✅" : row.api.openaiCompat === "partial" ? "⚠️" : "❌";
    return `| ${row.score} | ${row.name} | ${row.category} | ${quota} | ${gate} | ${compat} | ${row.verdict} |`;
  });
  return [...head, ...body].join("\n");
}

export function writeMarkdown(file) {
  const target = resolve(file);
  mkdirSync(dirname(target), { recursive: true });
  writeFileSync(target, toMarkdown() + "\n", "utf8");
  return { file: target, rows: CANDIDATES.length };
}
