#!/usr/bin/env node
// MoonAI 命令行：注册 / 号池 / 导出 / 导入 / 自检 / 生成编程工具配置。
//
//   node src/moonai-cli.mjs doctor
//   node src/moonai-cli.mjs register -n 5 -c 2 --engine auto --mail-provider imap \
//        --mail-domains example.com --push
//   node src/moonai-cli.mjs pool
//   node src/moonai-cli.mjs export --format csv
//   node src/moonai-cli.mjs import out/accounts.jsonl
//   node src/moonai-cli.mjs probe
//   node src/moonai-cli.mjs config --tool cline
//
// 与网关共用同一份号池文件（data/moonai-pool.json）与同一个注册锁，
// 所以"网关在跑批量注册"时 CLI 会被锁挡住，而不是两个进程互相覆盖。
import { existsSync } from "node:fs";
import { resolve } from "node:path";

import { loadConfig } from "./config.mjs";
import { Store } from "./store.mjs";
import { createProviderRuntime } from "./providers.mjs";
import { probeMoonai } from "./moonai-adapter.mjs";

const argv = process.argv.slice(2);
const command = argv[0] ?? "doctor";
const flags = parseFlags(argv.slice(1));

function parseFlags(args) {
  const out = { _: [] };
  for (let index = 0; index < args.length; index++) {
    const token = args[index];
    if (!token.startsWith("--")) {
      out._.push(token);
      continue;
    }
    const key = token.slice(2);
    const next = args[index + 1];
    if (next && !next.startsWith("--")) {
      out[key] = next;
      index++;
    } else {
      out[key] = true;
    }
  }
  return out;
}

const num = (value, fallback) => {
  const parsed = Number(value);
  return Number.isFinite(parsed) ? parsed : fallback;
};

const config = loadConfig();
const store = Store.at(config.dataDir);
const runtime = createProviderRuntime({
  dataDir: config.dataDir,
  port: config.port,
  token: config.token,
  store,
  config,
  log: (level, message) => {
    if (level === "warn" || level === "error") process.stderr.write(`[${level}] ${message}\n`);
  },
});
const { provider, pool, registrations } = runtime.requireProvider("moonai");

const json = (value) => process.stdout.write(JSON.stringify(value, null, 2) + "\n");

async function cmdDoctor() {
  const checks = [];
  const push = (name, ok, detail) => checks.push({ name, ok, detail });

  push("号池文件", true, pool.path);
  push("号池 schema", pool.toJSON().schema === "dcg.pool/moonai@1", pool.toJSON().schema);
  push("worker 目录", registrations.toolPresent, registrations.toolDir);
  push("python", true, registrations.python);
  push("上游 apiBase", Boolean(runtime.endpoints("moonai").apiBase), runtime.endpoints("moonai").apiBase);
  const accounts = pool.stats();
  push("号池账号", accounts.total > 0, `${accounts.total} 个（可用 ${pool.healthy().length}）`);
  push("网关 Key", accounts.keys > 0, `${accounts.keys} 个已绑定`);
  const withToken = pool.accounts.filter((a) => a.accessToken).length;
  push("会话令牌", withToken > 0, `${withToken} 个账号带 access_token`);

  for (const row of checks) {
    process.stdout.write(`[${row.ok ? "OK " : "FAIL"}] ${row.name.padEnd(14)} ${row.detail}\n`);
  }
  const failed = checks.filter((row) => !row.ok).length;
  process.stdout.write(`\n${checks.length - failed}/${checks.length} 项通过\n`);
  return failed ? 1 : 0;
}

async function cmdRegister() {
  const started = registrations.start({
    count: num(flags.n ?? flags.count, 1),
    concurrency: num(flags.c ?? flags.concurrency, 2),
    engine: flags.engine ?? "auto",
    browserStack: flags["browser-stack"] ?? "auto",
    mailProvider: flags["mail-provider"] ?? "imap",
    mailDomains: toList(flags["mail-domains"]),
    email: flags.email ?? "",
    captcha: flags.captcha ?? "none",
    captchaKey: flags["captcha-key"] ?? "",
    keysPerAccount: num(flags["keys-per-account"], 1),
    proxies: toList(flags.proxy),
    cdp: flags.cdp ?? "",
    headful: flags.headful === true,
    humanize: num(flags.humanize, 0),
    export: flags.export ?? "",
    mintKeys: flags["no-mint"] !== true,
    push: flags.push === true,
    verifyEmail: flags["no-verify"] !== true,
    antiBan: parseJson(flags["anti-ban"]),
    keyPolicy: parseJson(flags["key-policy"]),
  });
  if (flags.json) return json(started), 0;
  process.stdout.write(`已启动：${started.job?.id}（provider=${provider.id}）\n`);
  process.stdout.write("进度看：node src/moonai-cli.mjs status --follow\n");
  return 0;
}

async function cmdStatus() {
  const job = registrations.job;
  if (!job) return process.stdout.write("还没有跑过注册。\n"), 0;
  if (flags.json) return json(job), 0;
  process.stdout.write(`job ${job.id}  ${job.status}  ${job.created ?? 0}/${job.requested ?? "?"}\n`);
  for (const line of (job.lines ?? []).slice(-(num(flags.lines, 30)))) {
    process.stdout.write(`  ${line.at.slice(11, 19)} ${line.level.padEnd(5)} ${line.text}\n`);
  }
  return 0;
}

async function cmdStop() {
  const stopped = registrations.stop();
  json({ ok: true, stopped: stopped.stopped });
  return 0;
}

async function cmdPool() {
  pool.reload();
  if (flags.json) return json({ stats: pool.stats(), accounts: pool.accounts.map(runtime.maskAccount) }), 0;
  const stats = pool.stats();
  process.stdout.write(`schema ${stats.schema}\n`);
  process.stdout.write(`账号 ${stats.total}（active ${stats.active} / cooling ${stats.cooling} / unhealthy ${stats.unhealthy}）  Key ${stats.keys}\n\n`);
  for (const account of pool.accounts) {
    const key = account.apiKey ? `${account.apiKey.slice(0, 8)}…` : "-";
    const token = account.accessToken ? `token:${new Date(account.accessTokenExpiresAt || 0).toISOString().slice(0, 16)}` : "no-token";
    process.stdout.write(
      `${account.email.padEnd(34)} ${String(account.status).padEnd(8)} ${key.padEnd(14)} ${token.padEnd(22)} ${account.gatewayKey?.prefix ?? "-"}\n`,
    );
  }
  return 0;
}

async function cmdExport() {
  const format = flags.format ?? "jsonl";
  const result = registrations.export({ format, file: flags.file ?? "" });
  json(result);
  return 0;
}

async function cmdImport() {
  const file = flags._[0] ?? flags.file;
  if (!file) {
    process.stderr.write("用法：moonai-cli import <accounts.jsonl>\n");
    return 2;
  }
  const { readFileSync } = await import("node:fs");
  const rows = readFileSync(resolve(file), "utf8")
    .split(/\r?\n/)
    .map((line) => line.trim())
    .filter(Boolean)
    .map((line) => {
      try {
        return JSON.parse(line);
      } catch {
        return null;
      }
    })
    .filter(Boolean);
  let created = 0;
  let merged = 0;
  for (const row of rows) {
    const result = runtime.importAccount({ pool, store, record: { ...row, provider: provider.id }, mintKey: flags["no-mint"] !== true });
    result.created ? created++ : merged++;
  }
  json({ file, rows: rows.length, created, merged, pool: pool.stats() });
  return 0;
}

async function cmdProbe() {
  if (!pool.healthy().length) {
    process.stderr.write("号池里没有可用账号。\n");
    return 1;
  }
  const account = pool.pick({});
  const result = await probeMoonai({ provider, account, model: flags.model ?? provider.defaultModel });
  json({ email: account.email, model: flags.model ?? provider.defaultModel, ...result });
  return result.ok ? 0 : 1;
}

async function cmdConfig() {
  const baseUrl = `http://${config.host === "0.0.0.0" ? "127.0.0.1" : config.host}:${config.port}/p/moonai/v1`;
  const key =
    pool.accounts.find((account) => account.gatewayKey?.plaintext)?.gatewayKey?.plaintext ??
    "sk-dcg-（先在网关创建一把 Key）";
  const tool = String(flags.tool ?? "all");
  const model = provider.defaultModel;

  const blocks = {
    openai: `# OpenAI Python SDK
from openai import OpenAI
client = OpenAI(base_url="${baseUrl}", api_key="${key}")
print(client.chat.completions.create(model="${model}", messages=[{"role":"user","content":"hi"}]).choices[0].message.content)`,

    node: `// OpenAI Node SDK
import OpenAI from "openai";
const client = new OpenAI({ baseURL: "${baseUrl}", apiKey: "${key}" });
const res = await client.chat.completions.create({ model: "${model}", messages: [{ role: "user", content: "hi" }] });`,

    curl: `curl -sS ${baseUrl}/chat/completions \\
  -H "Authorization: Bearer ${key}" -H "Content-Type: application/json" \\
  -d '{"model":"${model}","messages":[{"role":"user","content":"hi"}],"stream":true}'`,

    cline: `# Cline / Roo Code（VS Code 插件）
API Provider : OpenAI Compatible
Base URL     : ${baseUrl}
API Key      : ${key}
Model ID     : ${model}`,

    continue: `# Continue（config.yaml）
models:
  - name: moonai
    provider: openai
    model: ${model}
    apiBase: ${baseUrl}
    apiKey: ${key}`,

    "claude-code": `# Claude Code（Anthropic 协议走 /p/moonai/v1/messages）
export ANTHROPIC_BASE_URL=http://${config.host === "0.0.0.0" ? "127.0.0.1" : config.host}:${config.port}/p/moonai
export ANTHROPIC_AUTH_TOKEN=${key}
export ANTHROPIC_MODEL=${model}`,

    aider: `# aider
export OPENAI_API_BASE=${baseUrl}
export OPENAI_API_KEY=${key}
aider --model openai/${model}`,

    env: `OPENAI_BASE_URL=${baseUrl}
OPENAI_API_KEY=${key}
OPENAI_MODEL=${model}`,
  };

  if (tool === "all") {
    for (const [name, block] of Object.entries(blocks)) {
      process.stdout.write(`\n===== ${name} =====\n${block}\n`);
    }
    return 0;
  }
  if (!blocks[tool]) {
    process.stderr.write(`未知工具：${tool}（可用：${Object.keys(blocks).join(", ")}）\n`);
    return 2;
  }
  process.stdout.write(blocks[tool] + "\n");
  return 0;
}

function toList(value) {
  if (value === undefined) return [];
  return (Array.isArray(value) ? value : [value]).flatMap((item) => String(item).split(",")).map((s) => s.trim()).filter(Boolean);
}

function parseJson(value) {
  if (!value || value === true) return {};
  try {
    const parsed = JSON.parse(value);
    return parsed && typeof parsed === "object" ? parsed : {};
  } catch {
    return {};
  }
}

const handlers = {
  doctor: cmdDoctor,
  register: cmdRegister,
  status: cmdStatus,
  stop: cmdStop,
  pool: cmdPool,
  export: cmdExport,
  import: cmdImport,
  probe: cmdProbe,
  config: cmdConfig,
};

if (!handlers[command]) {
  process.stderr.write(`未知命令：${command}（可用：${Object.keys(handlers).join(", ")}）\n`);
  process.exit(2);
}

if (!existsSync(pool.path) && command === "doctor") {
  process.stderr.write(`提示：号池文件还不存在（${pool.path}），先跑一轮 register。\n`);
}

const code = await handlers[command]();
process.exit(typeof code === "number" ? code : 0);
