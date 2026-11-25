#!/usr/bin/env node
// Allmodels 号池 CLI: batch-register accounts, mint gateway keys, export the pool.
//
//   node src/allmodels-cli.mjs register -n 20 -c 4 --captcha capsolver --captcha-key KEY
//   node src/allmodels-cli.mjs register -n 10 --mail-provider mailtm --export jsonl
//   node src/allmodels-cli.mjs pool                 # what is in the pool right now
//   node src/allmodels-cli.mjs export --format csv  # accounts + keys in one file
//   node src/allmodels-cli.mjs import accounts.jsonl
//
// The heavy lifting lives in tools/allmodels-reg (Python); this binary owns the
// pool file, the gateway keys and the export shapes.
import { existsSync, readFileSync } from "node:fs";
import { dirname, join, resolve } from "node:path";
import { fileURLToPath } from "node:url";

import { loadConfig } from "./config.mjs";
import { AllmodelsPool, importAccount } from "./allmodels-pool.mjs";
import { AllmodelsRegistrations } from "./allmodels-register.mjs";
import { Store } from "./store.mjs";

const here = dirname(fileURLToPath(import.meta.url));
const repoRoot = resolve(here, "..");

const USAGE = `allmodels-cli — Allmodels 号池：批量注册 / 建 key / 导出

  register [options]        批量注册（并发），每个账号自动建网关 key 并写号池
    -n, --count <n>         数量（默认 1）
    -c, --concurrency <n>   并发（默认 2）
        --mail-provider <p> tempmail_lol | mailtm | imap | fixed
        --mail-domain <d>   指定邮箱域名（可重复）
        --captcha <p>       none | capsolver | 2captcha | browser | manual
        --captcha-key <k>   解题平台 key，或 manual 模式下的 token
        --base-url <url>    面板地址（默认 https://dash.allmodels.io）
        --keys-per-account  每个账号再额外建几个 key（默认 1）
        --proxy <p>         代理，可重复
        --no-verify         跳过邮箱验证轮询
        --no-mint-key       不自动创建网关 sk-dcg- key
        --export <fmt>      结束后导出 jsonl | csv | pool
        --json              以 NDJSON 打印 worker 事件

  pool                     列出号池账号（脱敏）
  export [--format <fmt>]  导出账号 + key（jsonl | csv | pool）
  import <file.jsonl>      把外部账号导入号池并补发网关 key
  keys                     列出号池账号已绑定的网关 key
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
  const pool = AllmodelsPool.at(config.dataDir, {
    baseUrl: process.env.ALLMODELS_BASE_URL || "https://dash.allmodels.io",
    apiBase: process.env.ALLMODELS_API_BASE || "https://api.allmodels.io/v1",
    log: (level, message) => process.stderr.write(`[${level}] ${message}\n`),
  });
  return { config, store, pool };
}

async function cmdRegister(args) {
  const { config, store, pool } = makeContext();
  const token = config.token ?? process.env.DEEPAI_GATEWAY_TOKEN ?? null;
  const registrations = new AllmodelsRegistrations({
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
    concurrency: Number(args.concurrency ?? args.c ?? 2),
    mailProvider: args["mail-provider"],
    mailDomains: multi(args["mail-domain"]),
    captcha: args.captcha,
    captchaKey: args["captcha-key"],
    baseUrl: args["base-url"],
    keysPerAccount: Number(args["keys-per-account"] ?? 1),
    proxies: multi(args.proxy),
    verifyEmail: args["no-verify"] !== true,
    mintKeys: args["no-mint-key"] !== true,
    push: false,
  });
  process.stderr.write(`job ${snapshot.job.id} started: ${snapshot.job.requested} × ${snapshot.job.concurrency}\n`);

  // The controller is child-process based; wait for it to settle.
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
  const rows = pool.accounts.map((account) => ({
    email: account.email,
    apiKey: account.apiKey ? `${account.apiKey.slice(0, 8)}…${account.apiKey.slice(-4)}` : "",
    keyPrefix: account.keyPrefix,
    gatewayKey: account.gatewayKey?.prefix ?? "",
    projectId: account.projectId,
    guardrailId: account.guardrailId,
    status: account.status,
    healthy: account.healthy !== false,
    requests: account.requests ?? 0,
    createdAt: account.createdAt,
  }));
  process.stdout.write(JSON.stringify({ stats: pool.stats(), accounts: rows }, null, 2) + "\n");
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
  const target = resolve(args.out ?? join("data", "exports", `allmodels-accounts-${stamp}.${ext}`));
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
    if (!row.email || !(row.apiKey ?? row.api_key)) continue;
    const result = importAccount({ pool, store, record: row, mintKey: args["no-mint-key"] !== true });
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
  }));
  process.stdout.write(JSON.stringify({ keys: rows }, null, 2) + "\n");
  return 0;
}

async function main() {
  const args = parseArgs(process.argv.slice(2));
  const command = args._[0];
  if (!command || command === "help" || command === "-h") {
    process.stdout.write(USAGE);
    return 0;
  }
  const toolDir = process.env.ALLMODELS_REGISTER_DIR || join(repoRoot, "tools", "allmodels-reg");
  if (command === "register" && !existsSync(join(toolDir, "allmodels_reg", "cli.py"))) {
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
    default:
      process.stderr.write(`unknown command: ${command}\n\n${USAGE}`);
      return 2;
  }
}

process.exitCode = await main();
