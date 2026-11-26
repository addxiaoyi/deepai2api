#!/usr/bin/env node
// Cerebras CLI：注册 / 号池 / 导出 / 接入。
import { existsSync, readFileSync } from "node:fs";
import { dirname, join, resolve } from "node:path";
import { fileURLToPath } from "node:url";

import { loadConfig } from "./config.mjs";
import { Store } from "./store.mjs";
import { LINE, at as openPool, mask } from "./cerebras-pool.mjs";
import { createRegistrations } from "./cerebras-register.mjs";

const here = dirname(fileURLToPath(import.meta.url));
const repoRoot = resolve(here, "..");

const USAGE = `cerebras-cli — Cerebras 注册机 + 号池

  register -n <n> [--mail-domain d] [--proxy p]   批量注册并发 Key
  pool                                           号池（脱敏）
  keys                                           打印 Key（一行一把）
  export [--format keys|jsonl|csv|pool] [--out f]
  import <file.jsonl>                            外部账号 + Key 入池
  apply                                          写 data/keys/cerebras.{txt,json}
`;

function parseArgs(argv) {
  const out = { _: [] };
  for (let i = 0; i < argv.length; i++) {
    const arg = argv[i];
    if (!arg.startsWith("-")) { out._.push(arg); continue; }
    const name = arg.replace(/^-+/, "");
    const next = argv[i + 1];
    if (next !== undefined && !next.startsWith("-")) { out[name] = next; i++; }
    else out[name] = true;
  }
  return out;
}

const multi = (v) => (v === undefined ? [] : Array.isArray(v) ? v : [v]);

function ctx() {
  const config = loadConfig([]);
  const store = Store.at(config.dataDir);
  const pool = openPool(config.dataDir, {
    log: (level, message) => process.stderr.write(`[${level}] ${message}\n`),
  });
  return { config, store, pool };
}

async function cmdRegister(args) {
  const { config, store, pool } = ctx();
  const reg = createRegistrations({
    dataDir: config.dataDir,
    pool,
    store,
    log: (level, message) => process.stderr.write(`[${level}] ${message}\n`),
    onAccount: ({ account }) => process.stdout.write(`  + ${account.email}\n`),
  });
  const snapshot = reg.start({
    count: Number(args.count ?? args.n ?? 1),
    concurrency: Number(args.concurrency ?? args.c ?? 1),
    mailProvider: args["mail-provider"],
    mailDomains: multi(args["mail-domain"]),
    proxies: multi(args.proxy),
    export: args.export,
  });
  process.stderr.write(`job ${snapshot.job.id} started\n`);
  const finished = await new Promise((done) => {
    const timer = setInterval(() => {
      const status = reg.status();
      if (!status.running) { clearInterval(timer); done(status); }
    }, 1000);
  });
  process.stdout.write(
    JSON.stringify({ status: finished.job?.status, created: finished.job?.created ?? 0, keys: pool.keys().length, apply: finished.job?.apply ?? null, pool: pool.stats() }, null, 2) + "\n",
  );
  return (finished.job?.created ?? 0) > 0 ? 0 : 1;
}

function cmdPool() {
  const { pool } = ctx();
  process.stdout.write(JSON.stringify({ line: LINE.id, stats: pool.stats(), accounts: pool.accounts.map(mask) }, null, 2) + "\n");
  return 0;
}

function cmdKeys() {
  const { pool } = ctx();
  const rows = pool.keys();
  process.stdout.write(rows.length ? rows.join("\n") + "\n" : "");
  return rows.length ? 0 : 1;
}

function cmdExport(args) {
  const { config, pool } = ctx();
  const format = String(args.format ?? "keys");
  const ext = format === "csv" ? "csv" : format === "jsonl" ? "jsonl" : format === "pool" ? "json" : "txt";
  const stamp = new Date().toISOString().replace(/[:.]/g, "-");
  const target = resolve(args.out ?? join(config.dataDir, "exports", `${LINE.id}-${stamp}.${ext}`));
  const result = pool.exportTo(target, { format });
  process.stdout.write(JSON.stringify(result, null, 2) + "\n");
  return 0;
}

function cmdImport(args) {
  const { config, store, pool } = ctx();
  const file = args._[1];
  if (!file || !existsSync(file)) { process.stderr.write(`file not found: ${file}\n`); return 2; }
  const text = readFileSync(file, "utf8").trim();
  const rows = text.startsWith("[") ? JSON.parse(text) : text.split(/\r?\n/).filter(Boolean).map((l) => JSON.parse(l));
  let created = 0;
  let updated = 0;
  for (const row of rows) {
    const source = row.account ?? row;
    if (!source.email) continue;
    const reg = createRegistrations({ dataDir: config.dataDir, pool, store, log: () => {} });
    const result = reg._accept(source);
    if (result.created) created++;
    else updated++;
  }
  const reg = createRegistrations({ dataDir: config.dataDir, pool, store, log: () => {} });
  const applied = reg.apply();
  process.stdout.write(JSON.stringify({ line: LINE.id, imported: created, updated, apply: applied, pool: pool.stats() }, null, 2) + "\n");
  return 0;
}

function cmdApply() {
  const { config, store, pool } = ctx();
  const reg = createRegistrations({ dataDir: config.dataDir, pool, store, log: () => {} });
  const applied = reg.apply();
  process.stdout.write(JSON.stringify({ ok: true, line: LINE.id, ...applied, pool: pool.stats() }, null, 2) + "\n");
  return applied.keys > 0 || LINE.anonymous ? 0 : 1;
}

async function main() {
  const args = parseArgs(process.argv.slice(2));
  const command = args._[0];
  if (!command || command === "help" || command === "-h") { process.stdout.write(USAGE); return 0; }
  const toolDir = process.env[`${LINE.envPrefix}REGISTER_DIR`] || join(repoRoot, LINE.toolDir);
  if (command === "register" && !existsSync(join(toolDir, LINE.pyModule, "cli.py"))) {
    process.stderr.write(`找不到注册工具：${toolDir}\n`);
    return 2;
  }
  switch (command) {
    case "register": return cmdRegister(args);
    case "pool": return cmdPool();
    case "keys": return cmdKeys();
    case "export": return cmdExport(args);
    case "import": return cmdImport(args);
    case "apply": return cmdApply();
    default:
      process.stderr.write(`unknown command: ${command}\n\n${USAGE}`);
      return 2;
  }
}

process.exitCode = await main();
