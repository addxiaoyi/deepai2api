#!/usr/bin/env node
// jev-ai.org CLI：注册 → Key 池 → 导出 → 自动接入。
//
//   node src/jevai-cli.mjs register -n 5 --mail-provider imap --mail-domain example.com
//   node src/jevai-cli.mjs keys                     # 一行一把 sk-glm5-…
//   node src/jevai-cli.mjs apply                    # 写 data/keys/jevai.{txt,json}
//   node src/jevai-cli.mjs test-key                 # 用池里的 Key 打 /api/v1/models
import { existsSync, readFileSync } from "node:fs";
import { dirname, join, resolve } from "node:path";
import { fileURLToPath } from "node:url";

import { loadConfig } from "./config.mjs";
import { JevaiPool, importAccount, maskAccount } from "./jevai-pool.mjs";
import { JevaiRegistrations } from "./jevai-register.mjs";
import { JevaiRelay } from "./jevai-relay.mjs";
import { Store } from "./store.mjs";

const here = dirname(fileURLToPath(import.meta.url));
const repoRoot = resolve(here, "..");

const USAGE = `jevai-cli — jev-ai.org 注册机 + Key 池

  register [options]        批量注册并建 Key（产物 = sk-glm5-… Key）
    -n, --count <n>         数量（默认 1）
    -c, --concurrency <n>   并发（默认 1）
        --mail-provider <p> imap | fixed | mailtm | tempmail_lol
        --mail-domain <d>   邮箱域名（可重复；一次性邮箱会被拒）
        --key-name <n>      Key 的显示名
        --proxy <p>         注册出口，可重复
        --no-verify         跳过邮箱验证
        --no-checkin        跳过每日签到
        --export <fmt>      结束后导出 keys | jsonl | csv
        --json              NDJSON 事件

  pool                     号池（脱敏）
  keys                     打印池里的 Key（一行一把）
  export [--format f]      导出 keys | jsonl | csv | pool
  import <file.jsonl>      外部账号 + Key 入池（补发网关 Key）
  apply                    写 data/keys/jevai.{txt,json}（其它工具直接接入）
  checkin                  给池里每个账号签到（免费额度的主要来源）
    --all-today            连今天签过的也重试（默认跳过）
    --limit <n>            只签前 n 个
    --pause <秒>           账号间隔（默认 3）
  test-key [--key k]       用池里的 Key 打上游 /api/v1/models
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
  const pool = JevaiPool.at(config.dataDir, {
    baseUrl: process.env.JEVAIS_BASE_URL || "https://jev-ai.org",
    apiBase: process.env.JEVAIS_API_BASE || "https://jev-ai.org/api/v1",
    log: (level, message) => process.stderr.write(`[${level}] ${message}\n`),
  });
  return { config, store, pool };
}

async function cmdRegister(args) {
  const { config, store, pool } = makeContext();
  const registrations = new JevaiRegistrations({
    dataDir: config.dataDir,
    port: config.port,
    token: config.token ?? null,
    pool,
    store,
    log: (level, message) => process.stderr.write(`[${level}] ${message}\n`),
    onAccount: ({ account, gatewayKey }) => {
      if (!args.json) process.stdout.write(`  + ${account.email}  gw=${gatewayKey?.prefix ?? "-"}\n`);
    },
  });

  const snapshot = registrations.start({
    count: Number(args.count ?? args.n ?? 1),
    concurrency: Number(args.concurrency ?? args.c ?? 1),
    mailProvider: args["mail-provider"],
    mailDomains: multi(args["mail-domain"]),
    keyName: args["key-name"],
    proxies: multi(args.proxy),
    verifyEmail: args["no-verify"] !== true,
    checkin: args["no-checkin"] !== true,
    export: args.export,
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
    keys: finished.job?.keys ?? pool.keys().length,
    apply: finished.job?.apply ?? null,
    pool: pool.stats(),
  };
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

function cmdKeys() {
  const { pool } = makeContext();
  const rows = pool.keys();
  process.stdout.write(rows.length ? rows.join("\n") + "\n" : "");
  return rows.length ? 0 : 1;
}

function cmdExport(args) {
  const { config, pool } = makeContext();
  const format = String(args.format ?? "keys");
  if (!["keys", "jsonl", "csv", "pool"].includes(format)) {
    process.stderr.write(`unknown format: ${format}\n`);
    return 2;
  }
  const stamp = new Date().toISOString().replace(/[:.]/g, "-");
  const ext = format === "csv" ? "csv" : format === "jsonl" ? "jsonl" : format === "pool" ? "json" : "txt";
  const target = resolve(args.out ?? join(config.dataDir, "exports", `jevai-${stamp}.${ext}`));
  const result = pool.exportTo(target, { format });
  process.stdout.write(JSON.stringify(result, null, 2) + "\n");
  return 0;
}

function cmdImport(args) {
  const { config, store, pool } = makeContext();
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
    if (!source.email || !(source.apiKey ?? source.api_key)) continue;
    const result = importAccount({ pool, store, record: source, mintKey: args["no-mint-key"] !== true });
    if (result.created) created++;
    else updated++;
  }
  const registrations = new JevaiRegistrations({
    dataDir: config.dataDir,
    port: config.port,
    token: config.token ?? null,
    pool,
    store,
    log: () => {},
  });
  const applied = registrations.apply();
  process.stdout.write(JSON.stringify({ imported: created, updated, apply: applied, pool: pool.stats() }, null, 2) + "\n");
  return 0;
}

function cmdApply() {
  const { config, store, pool } = makeContext();
  const registrations = new JevaiRegistrations({
    dataDir: config.dataDir,
    port: config.port,
    token: config.token ?? null,
    pool,
    store,
    log: () => {},
  });
  const result = registrations.apply();
  process.stdout.write(JSON.stringify({ ok: true, ...result, pool: pool.stats() }, null, 2) + "\n");
  return result.keys > 0 ? 0 : 1;
}

async function cmdCheckin(args) {
  const { config, pool } = makeContext();
  const registrations = new JevaiRegistrations({
    dataDir: config.dataDir,
    port: config.port,
    token: config.token ?? null,
    pool,
    store: null,
    log: (level, message) => process.stderr.write(`[${level}] ${message}\n`),
  });
  const snapshot = registrations.checkin({
    onlyToday: args["all-today"] !== true,
    limit: Number(args.limit ?? 0),
    pause: Number(args.pause ?? 3),
    proxies: multi(args.proxy),
  });
  process.stderr.write(`job ${snapshot.job.id} started: checkin ${snapshot.job.requested} account(s)\n`);
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
    checked: finished.job?.checked ?? 0,
    gained: finished.job?.gained ?? 0,
    failed: finished.job?.failed ?? 0,
    details: finished.job?.summary?.results ?? null,
  };
  process.stdout.write(JSON.stringify(summary, null, 2) + "\n");
  return summary.checked > 0 ? 0 : 1;
}

async function cmdTestKey(args) {
  const { pool } = makeContext();
  const key = String(args.key ?? "") || pool.keys()[0] || "";
  if (!key) {
    process.stdout.write(JSON.stringify({ ok: false, error: "no key in pool" }) + "\n");
    return 1;
  }
  const relay = new JevaiRelay({ pool });
  const fakeReq = { method: "GET" };
  try {
    const { response, account } = await relay.forward(fakeReq, "/api/v1/models", {
      headers: { accept: "application/json" },
    });
    const text = (await response.text()).slice(0, 300);
    process.stdout.write(JSON.stringify({ ok: response.ok, status: response.status, account: account.email, body: text }, null, 2) + "\n");
    return response.ok ? 0 : 1;
  } catch (error) {
    process.stdout.write(JSON.stringify({ ok: false, error: error.message }) + "\n");
    return 1;
  }
}

async function main() {
  const args = parseArgs(process.argv.slice(2));
  const command = args._[0];
  if (!command || command === "help" || command === "-h") {
    process.stdout.write(USAGE);
    return 0;
  }
  const toolDir = process.env.JEVAIS_REGISTER_DIR || join(repoRoot, "tools", "jevai-reg");
  if (command === "register" && !existsSync(join(toolDir, "jevai_reg", "cli.py"))) {
    process.stderr.write(`找不到注册工具：${toolDir}\n`);
    return 2;
  }
  switch (command) {
    case "register":
      return cmdRegister(args);
    case "pool":
      return cmdPool();
    case "keys":
      return cmdKeys();
    case "export":
      return cmdExport(args);
    case "import":
      return cmdImport(args);
    case "apply":
      return cmdApply();
    case "checkin":
      return cmdCheckin(args);
    case "test-key":
      return cmdTestKey(args);
    default:
      process.stderr.write(`unknown command: ${command}\n\n${USAGE}`);
      return 2;
  }
}

process.exitCode = await main();
