#!/usr/bin/env node
// Clever AI Hub 号池 CLI：批量注册 / 出 Key / 导出 / 试聊。
//
//   node src/cleverai-cli.mjs register -n 10 -c 1 --mail-provider imap
//   node src/cleverai-cli.mjs pool
//   node src/cleverai-cli.mjs export --format csv
//   node src/cleverai-cli.mjs import accounts.jsonl
//   node src/cleverai-cli.mjs chat --model openai-gpt-6-luna
//
// 真正的注册逻辑在 tools/cleverai-reg（Python）；这里只管号池文件、网关 Key 和导出。
import { existsSync, readFileSync } from "node:fs";
import { dirname, join, resolve } from "node:path";
import { fileURLToPath } from "node:url";

import { loadConfig } from "./config.mjs";
import { CleveraiPool, importAccount, maskAccount } from "./cleverai-pool.mjs";
import { CleveraiRegistrations } from "./cleverai-register.mjs";
import { CleveraiRelay } from "./cleverai-relay.mjs";
import { Store } from "./store.mjs";

const here = dirname(fileURLToPath(import.meta.url));
const repoRoot = resolve(here, "..");

const USAGE = `cleverai-cli — Clever AI Hub 号池：批量注册 / 出 Key / 导出

  register [options]        批量注册（并发），每个账号自动建网关 key 并写号池
    -n, --count <n>         数量（默认 1）
    -c, --concurrency <n>   并发（默认 1，防封建议保持 1）
        --mail-provider <p> imap | fixed | mailtm | tempmail_lol
        --mail-domain <d>   邮箱域名（可重复）
        --appcheck <p>      browser | capsolver | manual
        --captcha-key <k>   capsolver clientKey
        --appcheck-token   直接给一枚现成 App Check JWT
        --engine <e>        auto | protocol | browser
        --browser-stack <s> auto | camoufox | patchright | playwright
        --headful           有头浏览器（调试）
        --cdp <url>         接管外部指纹浏览器
        --proxy <p>         代理，可重复
        --no-verify         跳过邮箱验证
        --no-warmup         跳过注册后预热对话
        --export <fmt>      结束后导出 jsonl | csv | pool
        --json              以 NDJSON 打印 worker 事件

  pool                     列出号池账号（脱敏）
  export [--format <fmt>]  导出账号 + Key（jsonl | csv | pool）
  import <file.jsonl>      把外部账号导入号池并补发网关 Key
  keys                     列出号池账号已绑定的网关 key
  chat [options]           用号池账号跑一次对话，验证还能不能用
`;

function parseArgs(argv) {
  const out = { _: [] };
  for (let i = 0; i < argv.length; i++) {
    const arg = argv[i];
    if (!arg.startsWith("-")) {
      out._.push(arg);
      continue;
    }
    const name = arg.replace(/^-+/, "");
    if (name.includes("=")) {
      const [k, v] = name.split("=", 2);
      out[k] = v;
      continue;
    }
    const next = argv[i + 1];
    if (next !== undefined && !next.startsWith("-")) {
      out[name] = next;
      i++;
    } else {
      out[name] = true;
    }
  }
  return out;
}

const multi = (value) => (value === undefined ? [] : Array.isArray(value) ? value : [value]);

function makeContext() {
  const config = loadConfig([]);
  const store = Store.at(config.dataDir);
  const pool = CleveraiPool.at(config.dataDir, {
    baseUrl: process.env.CLEVERAI_BASE_URL || "https://cleverai.app",
    apiBase: process.env.CLEVERAI_API_BASE || "https://api.cleverai.app",
    engine: process.env.CLEVERAI_CHAT_ENGINE || "openai-gpt-6-luna",
    log: (level, message) => process.stderr.write(`[${level}] ${message}\n`),
  });
  return { config, store, pool };
}

async function cmdRegister(args) {
  const { config, store, pool } = makeContext();
  const token = config.token ?? process.env.DEEPAI_GATEWAY_TOKEN ?? null;
  const registrations = new CleveraiRegistrations({
    dataDir: config.dataDir,
    port: config.port,
    token,
    pool,
    store,
    log: (level, message) => process.stderr.write(`[${level}] ${message}\n`),
    onAccount: ({ account, gatewayKey }) => {
      if (!args.json) {
        process.stdout.write(`  + ${account.email}  gw=${gatewayKey?.prefix ?? "-"}\n`);
      }
    },
  });

  const snapshot = registrations.start({
    count: Number(args.count ?? args.n ?? 1),
    concurrency: Number(args.concurrency ?? args.c ?? 1),
    mailProvider: args["mail-provider"],
    mailDomains: multi(args["mail-domain"]),
    appcheck: args.appcheck,
    captchaKey: args["captcha-key"],
    appcheckToken: args["appcheck-token"],
    engine: args.engine,
    browserStack: args["browser-stack"],
    headful: args.headful === true,
    cdp: args.cdp,
    chatEngine: args["chat-engine"],
    proxies: multi(args.proxy),
    verifyEmail: args["no-verify"] !== true,
    warmup: args["no-warmup"] !== true,
    mintKeys: args["no-mint-key"] !== true,
    perProxyConcurrency: Number(args["per-proxy"] ?? 1),
    perDomainBurst: Number(args["domain-burst"] ?? 5),
    push: false,
  });
  process.stderr.write(`job ${snapshot.job.id} started: ${snapshot.job.requested} × ${snapshot.job.concurrency}\n`);

  const finished = await new Promise((done) => {
    const timer = setInterval(() => {
      const status = registrations.status();
      if (!status.running) {
        clearInterval(timer);
        done(status);
      }
    }, 1000);
  });

  const summary = {
    status: finished.job?.status,
    created: finished.job?.created ?? 0,
    failed: finished.job?.failed ?? 0,
    pool: pool.stats(),
  };
  if (args.export) summary.export = registrations.export({ format: String(args.export) });
  process.stdout.write(JSON.stringify(summary, null, 2) + "\n");
  return summary.created > 0 ? 0 : 1;
}

function cmdPool() {
  const { pool } = makeContext();
  process.stdout.write(
    JSON.stringify({ stats: pool.stats(), accounts: pool.accounts.map(maskAccount) }, null, 2) + "\n",
  );
  return 0;
}

function cmdExport(args) {
  const { pool } = makeContext();
  const format = String(args.format ?? "jsonl");
  if (!["jsonl", "csv", "pool"].includes(format)) {
    process.stderr.write(`unknown format: ${format}\n`);
    return 2;
  }
  const stamp = new Date().toISOString().replace(/[:.]/g, "-");
  const ext = format === "csv" ? "csv" : format === "pool" ? "json" : "jsonl";
  const target = resolve(args.out ?? join("data", "exports", `cleverai-accounts-${stamp}.${ext}`));
  const result = pool.exportTo(target, { format });
  process.stdout.write(JSON.stringify(result, null, 2) + "\n");
  return 0;
}

function cmdImport(args) {
  const { store, pool } = makeContext();
  const file = args._[1];
  if (!file || !existsSync(file)) {
    process.stderr.write(`file not found: ${file ?? "(missing)"}\n`);
    return 2;
  }
  const text = readFileSync(file, "utf8").trim();
  const rows = text.startsWith("[")
    ? JSON.parse(text)
    : text.split(/\r?\n/).filter(Boolean).map((line) => JSON.parse(line));
  let created = 0;
  let updated = 0;
  for (const row of rows) {
    const source = row.account ?? row;
    if (!source.email || !(source.refreshToken ?? source.refresh_token)) continue;
    const result = importAccount({ pool, store, record: source, mintKey: args["no-mint-key"] !== true });
    if (result.created) created++;
    else updated++;
  }
  process.stdout.write(JSON.stringify({ imported: created, updated, pool: pool.stats() }, null, 2) + "\n");
  return 0;
}

function cmdKeys() {
  const { pool } = makeContext();
  const rows = pool.accounts.map((account) => ({
    email: account.email,
    gatewayKeyId: account.gatewayKey?.id ?? "",
    gatewayKeyPrefix: account.gatewayKey?.prefix ?? "",
    label: account.gatewayKey?.label ?? "",
    engine: account.engine,
    wallet: account.wallet ?? {},
  }));
  process.stdout.write(JSON.stringify({ keys: rows }, null, 2) + "\n");
  return 0;
}

async function cmdChat(args) {
  const { config, pool } = makeContext();
  const relay = new CleveraiRelay({
    pool,
    dataDir: config.dataDir,
    log: (level, message) => process.stderr.write(`[${level}] ${message}\n`),
  });
  const prompt = String(args.prompt ?? "Say hello in one short sentence.");
  const result = await relay.complete({
    model: args.model ?? "",
    messages: [{ role: "user", content: prompt }],
  });
  process.stdout.write(JSON.stringify(result, null, 2) + "\n");
  return 0;
}

async function main() {
  const args = parseArgs(process.argv.slice(2));
  const command = args._[0];
  if (!command || command === "help" || command === "-h") {
    process.stdout.write(USAGE);
    return 0;
  }
  const toolDir = process.env.CLEVERAI_REGISTER_DIR || join(repoRoot, "tools", "cleverai-reg");
  if (command === "register" && !existsSync(join(toolDir, "cleverai_reg", "cli.py"))) {
    process.stderr.write(`找不到注册工具：${toolDir}\n`);
    return 2;
  }
  switch (command) {
    case "register":
      return cmdRegister(args);
    case "pool":
      return cmdPool();
    case "export":
      return cmdExport(args);
    case "import":
      return cmdImport(args);
    case "keys":
      return cmdKeys();
    case "chat":
      return cmdChat(args);
    default:
      process.stderr.write(`unknown command: ${command}\n\n${USAGE}`);
      return 2;
  }
}

process.exitCode = await main();
