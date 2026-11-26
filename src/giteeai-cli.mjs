#!/usr/bin/env node
// Gitee AI（模力方舟） CLI：校验令牌 → 入池 → 导出 → 接入。
//
// 与其它线不同：Gitee AI 令牌必须在 gitee.com 个人设置里创建，没有机器注册接口，
// 所以这里是 "tokens 文件 → 校验 → 入池"，注册那一步外包给人工/Gitee 账号。
import { existsSync, readFileSync } from "node:fs";
import { dirname, join, resolve } from "node:path";
import { fileURLToPath } from "node:url";

import { loadConfig } from "./config.mjs";
import { Store } from "./store.mjs";
import { LINE, at as openPool, mask } from "./giteeai-pool.mjs";
import { createRegistrations } from "./giteeai-register.mjs";
import { importAccount } from "./openai-lines.mjs";

const here = dirname(fileURLToPath(import.meta.url));

const USAGE = `giteeai-cli — Gitee AI（模力方舟） 令牌池

  import <tokens.txt>    一行一个 Gitee 令牌：校验（GET /v1/models）后入池
  verify <token>         只校验一枚
  pool                   号池（脱敏）
  keys                   打印令牌
  export [--format keys|jsonl|csv] [--out f]
  apply                  写 data/keys/giteeai.{txt,json}
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

function ctx() {
  const config = loadConfig([]);
  const store = Store.at(config.dataDir);
  const pool = openPool(config.dataDir, { log: (l, m) => process.stderr.write(`[${l}] ${m}\n`) });
  return { config, store, pool };
}

async function verify(token) {
  const url = `${LINE.apiBase.replace(/\/+$/, "")}/models`;
  const response = await fetch(url, { headers: { Authorization: `Bearer ${token}` } });
  const text = await response.text().catch(() => "");
  return { ok: response.ok, status: response.status, body: text.slice(0, 200) };
}

async function cmdImport(args) {
  const file = args._[1];
  if (!file || !existsSync(file)) {
    process.stderr.write(`file not found: ${file ?? "(missing)"}\n`);
    return 2;
  }
  const { config, store, pool } = ctx();
  const tokens = readFileSync(file, "utf8")
    .split(/\r?\n/)
    .map((t) => t.trim())
    .filter((t) => t && !t.startsWith("#"));
  let created = 0;
  let failed = 0;
  for (const [index, token] of tokens.entries()) {
    const check = await verify(token);
    if (!check.ok) {
      failed++;
      process.stderr.write(`  ✗ token#${index + 1} HTTP ${check.status}\n`);
      continue;
    }
    const result = importAccount({
      line: LINE,
      pool,
      store,
      record: { email: `giteeai-token-${index + 1}@local`, apiKey: token, engine: "token-import" },
    });
    if (result.created) created++;
    process.stdout.write(`  ✓ token#${index + 1} 入池${result.gatewayKey ? ` gw=${result.gatewayKey.prefix}` : ""}\n`);
  }
  const reg = createRegistrations({ dataDir: config.dataDir, pool, store, log: () => {} });
  const applied = reg.apply();
  process.stdout.write(JSON.stringify({ ok: created > 0, created, failed, apply: applied, pool: pool.stats() }, null, 2) + "\n");
  return created > 0 ? 0 : 1;
}

async function cmdVerify(args) {
  const token = args._[1];
  if (!token) { process.stderr.write("要一枚令牌\n"); return 2; }
  const result = await verify(token);
  process.stdout.write(JSON.stringify(result, null, 2) + "\n");
  return result.ok ? 0 : 1;
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
  const ext = format === "csv" ? "csv" : format === "jsonl" ? "jsonl" : "txt";
  const stamp = new Date().toISOString().replace(/[:.]/g, "-");
  const target = resolve(args.out ?? join(config.dataDir, "exports", `giteeai-${stamp}.${ext}`));
  process.stdout.write(JSON.stringify(pool.exportTo(target, { format }), null, 2) + "\n");
  return 0;
}

function cmdApply() {
  const { config, store, pool } = ctx();
  const reg = createRegistrations({ dataDir: config.dataDir, pool, store, log: () => {} });
  const applied = reg.apply();
  process.stdout.write(JSON.stringify({ ok: true, ...applied, pool: pool.stats() }, null, 2) + "\n");
  return applied.keys > 0 ? 0 : 1;
}

const args = parseArgs(process.argv.slice(2));
const command = args._[0];
if (!command || command === "help" || command === "-h") {
  process.stdout.write(USAGE);
  process.exitCode = 0;
} else if (command === "import") process.exitCode = await cmdImport(args);
else if (command === "verify") process.exitCode = await cmdVerify(args);
else if (command === "pool") process.exitCode = cmdPool();
else if (command === "keys") process.exitCode = cmdKeys();
else if (command === "export") process.exitCode = cmdExport(args);
else if (command === "apply") process.exitCode = cmdApply();
else {
  process.stderr.write(`unknown command: ${command}\n\n${USAGE}`);
  process.exitCode = 2;
}
