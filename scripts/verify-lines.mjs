#!/usr/bin/env node
// 真实可用性巡检：对每条线打**真的 HTTP**，不 mock。
//
//   node scripts/verify-lines.mjs                  # 全部线
//   node scripts/verify-lines.mjs --only agnes     # 只查一条
//   node scripts/verify-lines.mjs --json           # 机器可读
//   node scripts/verify-lines.mjs --write          # 顺手更新 docs/LINES-STATUS.md
//
// 判定口径（这是重点，避免"看起来 200 就算能用"）：
//
//   models  上游 `/v1/models`：200=匿名可列（少数站），401/403=活着但要 Key（正常）
//   signup  注册/发码端点：**404/HTML = 端点不存在**；4xx + JSON + 业务码 = 端点真实存在
//   keys    发 Key 端点：401/400/405 = 存在（要登录/要参数）；404/HTML = 路径不对
//   captcha 响应里出现 captcha/turnstile/ticket/randstr 字样 → 该线有验证码门槛
//
// 每个结论都带证据（状态码 + 响应片段），写进 docs/LINES-STATUS.md。
import { mkdirSync, writeFileSync } from "node:fs";
import { dirname, resolve } from "node:path";

const UA = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36";
const TIMEOUT = 15000;

/** 每条线要打的点：站内事实（来自各线 site.py / 描述）。 */
export const CHECKS = {
  agnes: {
    name: "Agnes AI",
    models: "https://api.agnes-ai.cn/v1/models",
    chat: "https://api.agnes-ai.cn/v1/chat/completions",
    signup: [
      ["GET", "https://platform-backend.agnes-ai.com/api/verification?email=probe@example.com&purpose=register"],
      ["POST", "https://platform-backend.agnes-ai.com/api/user/register", { email: "probe@example.com", password: "Xk9#mQ2vLp7!aB", password_confirm: "Xk9#mQ2vLp7!aB", code: "000000" }],
    ],
    keys: [["POST", "https://platform-backend.agnes-ai.com/api/token", { name: "x", api_key_profile: "default" }]],
  },
  cerebras: {
    name: "Cerebras",
    models: "https://api.cerebras.ai/v1/models",
    chat: "https://api.cerebras.ai/v1/chat/completions",
    signup: [["GET", "https://cloud.cerebras.ai/api/auth/ok"]],
    keys: [["GET", "https://cloud.cerebras.ai/api/auth/api-keys"]],
  },
  "opencode-zen": {
    name: "OpenCode Zen",
    models: "https://opencode.ai/zen/v1/models",
    chat: "https://opencode.ai/zen/v1/chat/completions",
    // 匿名就能列模型（实测 200）；发 Key 的接口只在登录后可见，暂无公开路径
    signup: [],
    keys: [
      ["GET", "https://opencode.ai/zen/api/keys"],
      ["GET", "https://opencode.ai/api/keys"],
    ],
  },
  requesty: {
    name: "Requesty",
    models: "https://router.requesty.ai/v1/models",
    chat: "https://router.requesty.ai/v1/chat/completions",
    signup: [
      ["GET", "https://app.requesty.ai/api/auth/get-session"],
    ],
    keys: [
      ["GET", "https://app.requesty.ai/api/api-keys"],
      ["GET", "https://app.requesty.ai/api/keys"],
    ],
  },
  unorouter: {
    name: "UnoRouter",
    models: "https://api.unorouter.com/v1/models",
    chat: "https://api.unorouter.com/v1/chat/completions",
    signup: [],
    // 实测：POST /api/token/ → 401 AUTH_UNAUTHORIZED（端点存在，需登录）
    keys: [
      ["POST", "https://api.unorouter.com/api/token/", { name: "dcg-probe" }],
    ],
  },
  pollinations: {
    name: "Pollinations",
    models: "https://gen.pollinations.ai/v1/models",
    chat: "https://gen.pollinations.ai/v1/chat/completions",
    signup: [],
    keys: [],
  },
  giteeai: {
    name: "Gitee AI",
    models: "https://ai.gitee.com/v1/models",
    images: "https://ai.gitee.com/v1/images/generations",
    signup: [],
    keys: [],
  },
  jevai: {
    name: "JEV AI",
    models: "https://jev-ai.org/api/v1/models",
    chat: "https://jev-ai.org/api/v1/chat/completions",
    signup: [
      ["GET", "https://jev-ai.org/api/auth/ok"],
      ["POST", "https://jev-ai.org/api/auth/sign-up/email", { email: "probe@example.com", password: "Xk9#mQ2vLp7!aB", name: "probe" }],
    ],
    keys: [["GET", "https://jev-ai.org/api/user/api-keys"]],
  },
  cleverai: {
    name: "Clever AI",
    models: "https://api.cleverai.app/appserver/api/v1/models",
    signup: [],
    keys: [],
  },
  gabai: {
    name: "Gab AI",
    models: "https://gab.ai/v1/models",
    chat: "https://gab.ai/v1/chat/completions",
    signup: [],
    keys: [],
  },
  proxyscrape: {
    name: "ProxyScrape",
    models: "",
    signup: [
      ["POST", "https://dashboard.proxyscrape.com/v2/v4/account/auth/register", { email: "probe@example.com", password: "Xk9#mQ2vLp7!aB" }],
    ],
    keys: [],
  },
  "allmodels": { name: "AllModels", models: "", signup: [], keys: [] },
  poe: { name: "Poe", models: "", signup: [], keys: [] },
  providers: { name: "Providers", models: "", signup: [], keys: [] },
  deepai: { name: "DeepAI", models: "", signup: [], keys: [] },
};

const CAPTCHA_HINTS = ["captcha", "turnstile", "ticket", "randstr", "human verification", "verify you are human"];

async function hit(method, url, body, attempt = 0) {
  const started = Date.now();
  const controller = new AbortController();
  const timer = setTimeout(() => controller.abort(), TIMEOUT);
  try {
    const res = await fetch(url, {
      method,
      signal: controller.signal,
      headers: { "User-Agent": UA, "Content-Type": "application/json", Accept: "application/json" },
      ...(body ? { body: JSON.stringify(body) } : {}),
      redirect: "manual",
    });
    const text = (await res.text().catch(() => "")).slice(0, 400);
    return { status: res.status, ms: Date.now() - started, text };
  } catch (error) {
    // 网络抖动很常见（同一台机器前后两次结果可能不同），重试一次再判不可达
    if (attempt < 2) return hit(method, url, body, attempt + 1);
    return { status: 0, ms: Date.now() - started, error: error.message.slice(0, 120) };
  } finally {
    clearTimeout(timer);
  }
}

const isHtml = (text) => /^\s*<(!doctype|html)/i.test(text ?? "");
const hasCaptcha = (text) => CAPTCHA_HINTS.some((hint) => (text ?? "").toLowerCase().includes(hint));

/** models 判定 */
export function judgeModels(result) {
  if (result.status === 0) return { level: "unreachable", note: result.error ?? "网络不可达" };
  if (result.status === 200) return { level: "ok", note: "匿名可列模型" };
  if ([401, 403].includes(result.status)) return { level: "ok", note: "活着，需要 Key（正常）" };
  if (isHtml(result.text)) return { level: "wrong-path", note: `回了 HTML（HTTP ${result.status}），基址可能不对` };
  return { level: "suspect", note: `HTTP ${result.status}` };
}

/** chat/images 端点判定：401/403/400=存在（要 Key 或参数），404=不存在，200=匿名可用 */
export function judgeChat(result) {
  if (result.status === 0) return { level: "unreachable", note: result.error ?? "网络不可达" };
  if (result.status === 200) return { level: "anonymous", note: "匿名可用（无需 Key）" };
  if ([400, 401, 403, 422].includes(result.status)) return { level: "exists", note: `HTTP ${result.status}（端点存在，需 Key/参数）` };
  if (result.status === 404) return { level: "missing", note: "HTTP 404（端点不存在）" };
  return { level: "suspect", note: `HTTP ${result.status}` };
}

/** signup / keys 判定：404 或 HTML = 端点不存在 */
export function judgeEndpoint(result) {
  if (result.status === 0) return { level: "unreachable", note: result.error ?? "网络不可达" };
  if (result.status === 404 || result.status === 405) {
    return { level: "missing", note: `HTTP ${result.status}（端点不存在）` };
  }
  if (isHtml(result.text)) return { level: "missing", note: `HTTP ${result.status} 回了 HTML` };
  if (result.status < 500) {
    return { level: "exists", note: `HTTP ${result.status} + JSON（端点存在）${hasCaptcha(result.text) ? "，需验证码" : ""}` };
  }
  return { level: "error", note: `HTTP ${result.status}` };
}

async function verifyLine(id) {
  const spec = CHECKS[id];
  const out = { id, name: spec.name, models: null, signup: [], keys: [], captcha: false, verdict: "unknown", notes: [] };

  if (spec.models) {
    const result = await hit("GET", spec.models);
    out.models = { url: spec.models, ...result, judge: judgeModels(result) };
    if (hasCaptcha(result.text)) out.captcha = true;
  }
  for (const [field, body] of [["chat", { model: "x", messages: [{ role: "user", content: "hi" }] }], ["images", { model: "z-image-turbo", prompt: "hi" }]]) {
    const url = spec[field];
    if (!url) continue;
    const result = await hit("POST", url, body);
    out[field] = { url, ...result, judge: judgeChat(result) };
  }
  for (const [method, url, body] of spec.signup) {
    const result = await hit(method, url, body);
    const judge = judgeEndpoint(result);
    out.signup.push({ method, url, ...result, judge });
    if (hasCaptcha(result.text)) out.captcha = true;
  }
  for (const [method, url, body] of spec.keys) {
    const result = await hit(method, url, body);
    out.keys.push({ method, url, ...result, judge: judgeEndpoint(result) });
  }

  const modelsOk = out.models?.judge.level === "ok";
  out.chatOk = out.chat?.judge.level === "exists" || out.chat?.judge.level === "anonymous";
  out.imagesOk = out.images?.judge.level === "exists" || out.images?.judge.level === "anonymous";
  const relayOk = out.chatOk || out.imagesOk;
  const signupOk = out.signup.some((row) => row.judge.level === "exists");
  const keysOk = out.keys.some((row) => row.judge.level === "exists");
  const anyMissing = [...out.signup, ...out.keys].some((row) => row.judge.level === "missing");

  if (!out.models && !out.signup.length && !out.keys.length) out.verdict = "not-checked";
  else if (out.models?.judge.level === "unreachable" && !signupOk && !keysOk) out.verdict = "unreachable";
  else if (!modelsOk && !signupOk && !keysOk) out.verdict = out.models ? "models-broken" : "not-checked";
  else if (signupOk && keysOk) out.verdict = out.captcha ? "usable-with-captcha" : "usable";
  else if (relayOk && signupOk) out.verdict = out.captcha ? "usable-with-captcha" : "relay-ready";
  else if (relayOk) out.verdict = "relay-ready";
  else if (signupOk) out.verdict = "signup-ok-keys-unknown";
  else out.verdict = "models-only";
  if (anyMissing) out.notes.push("有登记端点不存在，需要修 site.py / 描述");
  if ([...out.signup, ...out.keys, out.models, out.chat, out.images].filter(Boolean).some((row) => row.judge.level === "unreachable")) {
    out.notes.push("有端点本轮网络不可达（不代表站点挂了）");
  }
  if (out.captcha) out.notes.push("有验证码门槛");
  return out;
}

function toMarkdown(rows) {
  const head = [
    "# 各线真实可用性（自动生成）",
    "",
    `> 生成于 ${new Date().toISOString()}　命令：\`node scripts/verify-lines.mjs --write\``,
    "> 判定：models 200/401/403=活；注册与发 Key 端点 404 或回 HTML = 不存在。",
    "",
    "| 线 | models | 注册/发码 | 发 Key | 验证码 | 结论 |",
    "| --- | --- | --- | --- | --- | --- |",
  ];
  const body = rows.map((row) => {
    const models = row.models ? `${row.models.status} ${row.models.judge.level}` : "—";
    const signup = row.signup.length
      ? row.signup.map((r) => `${r.judge.level}(${r.status})`).join(" / ")
      : "—";
    const keys = row.keys.length ? row.keys.map((r) => `${r.judge.level}(${r.status})`).join(" / ") : "—";
    return `| ${row.name}（${row.id}） | ${models} | ${signup} | ${keys} | ${row.captcha ? "需要" : "无"} | ${row.verdict} |`;
  });
  const notes = ["", "## 证据", ""];
  for (const row of rows) {
    notes.push(`### ${row.name}（${row.id}）— ${row.verdict}`);
    if (row.models) notes.push(`- models \`${row.models.url}\` → ${row.models.status} · ${row.models.judge.note}`);
    for (const item of [row.chat, row.images].filter(Boolean)) {
      process.stdout.write(`     POST ${item.url} -> ${item.status} ${item.judge.level}\n`);
    }
    for (const item of [...row.signup, ...row.keys]) {
      notes.push(`- \`${item.method} ${item.url}\` → ${item.status} · ${item.judge.note} · \`${(item.text ?? "").slice(0, 120).replace(/\n/g, " ")}\``);
    }
    if (row.notes.length) notes.push(`- 备注：${row.notes.join("；")}`);
    notes.push("");
  }
  return [...head, ...body, ...notes].join("\n");
}

const args = process.argv.slice(2);
const only = args.includes("--only") ? args[args.indexOf("--only") + 1] : "";
const asJson = args.includes("--json");
const shouldWrite = args.includes("--write");

const ids = only ? [only] : Object.keys(CHECKS);
const rows = [];
for (const id of ids) {
  if (!CHECKS[id]) {
    process.stderr.write(`未知线：${id}\n`);
    process.exitCode = 2;
    continue;
  }
  const row = await verifyLine(id);
  rows.push(row);
  if (!asJson) {
    const mark = {
      usable: "✅",
      "usable-with-captcha": "🔐",
      "relay-ready": "🟢",
      "signup-ok-keys-unknown": "🟡",
      "models-only": "🟡",
      "keys-ok-signup-unknown": "🟡",
      "models-broken": "❌",
      unreachable: "📡",
      "not-checked": "·",
    }[row.verdict] ?? "·";
    process.stdout.write(`${mark} ${row.id.padEnd(14)} ${row.verdict.padEnd(24)} models=${row.models?.status ?? "-"}\n`);
    for (const item of [...row.signup, ...row.keys]) {
      process.stdout.write(`     ${item.method} ${item.url} -> ${item.status} ${item.judge.level}${item.judge.note.includes("验证码") ? " 🔐" : ""}\n`);
    }
  }
}

if (asJson) process.stdout.write(JSON.stringify({ at: new Date().toISOString(), lines: rows }, null, 2) + "\n");
if (shouldWrite) {
  const target = resolve("docs/LINES-STATUS.md");
  mkdirSync(dirname(target), { recursive: true });
  writeFileSync(target, toMarkdown(rows) + "\n", "utf8");
  if (!asJson) process.stdout.write(`\n已写入 ${target}\n`);
}
