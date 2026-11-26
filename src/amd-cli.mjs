#!/usr/bin/env node
// AMD 开发者云（GPU） CLI：cookie 入池 → 积分 → 起/停 GPU 实例 → 导出。
import { existsSync, readFileSync } from "node:fs";
import { dirname, join, resolve } from "node:path";
import { fileURLToPath } from "node:url";

import { loadConfig } from "./config.mjs";
import { Store } from "./store.mjs";
import { importAccount, maskAccount } from "./openai-lines.mjs";
import { LINE, at as openPool, instanceSummary } from "./amd-pool.mjs";

const here = dirname(fileURLToPath(import.meta.url));
const repoRoot = resolve(here, "..");

const USAGE = `amd-cli — AMD 开发者云（GPU）

  import <file>          每行 "email<TAB>cookie"（或只有 cookie）；校验后入池
  verify <cookie>        只校验一个 cookie
  pool                   号池 + 积分/实例概览
  export [--format keys|jsonl|csv] [--out f]
  apply                  写 data/keys/amd.{txt,json}
  python 侧             cd tools/amd-reg && python -m amd_reg launch|status|remove|points
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
  const pool = openPool(config.dataDir, { log: (l, m) => process.stderr.write(`[${l}] ${m}\\n`) });
  return { config, store, pool };
}

async function verify(cookie) {
  const response = await fetch(`${LINE.apiBase}/User/GetCurrentUserDataSummary`, {
    headers: { Cookie: cookie, Accept: "application/json" },
  });
  const text = await response.text().catch(() => "");
  let points = null;
  try {
    const payload = JSON.parse(text);
    const data = payload?.data ?? {};
    points = data.points ?? data.totalPoints ?? null;
  } catch {}
  return { ok: response.ok, status: response.status, points, body: text.slice(0, 160) };
}

async function cmdImport(args) {
  const file = args._[1];
  if (!file || !existsSync(file)) { process.stderr.write(`file not found: ${file ?? "(missing)"}\n`); return 2; }
  const { config, store, pool } = ctx();
  const rows = readFileSync(file, "utf8").split(/\r?\n/).map((l) => l.trim()).filter((l) => l && !l.startsWith("#"));
  let created = 0;
  let failed = 0;
  for (const [index, row] of rows.entries()) {
    const [maybeEmail, ...rest] = row.split("\t");
    const email = rest.length ? maybeEmail : `amd-${index + 1}@local`;
    const cookie = (rest.length ? rest.join("\t") : row).trim();
    const check = await verify(cookie);
    if (!check.ok) {
      failed++;
      process.stderr.write(`  ✗ ${email} HTTP ${check.status}\n`);
      continue;
    }
    const result = importAccount({
      line: LINE,
      pool,
      store,
      record: {
        email,
        apiKey: cookie,
        name: "AMD developer",
        credits: { points: check.points, hasInstance: null },
        engine: "cookie-import",
      },
    });
    if (result.created) created++;
    process.stdout.write(`  ✓ ${email} 入池（积分 ${check.points ?? "?"}）\n`);
  }
  process.stdout.write(JSON.stringify({ ok: created > 0, created, failed, instances: instanceSummary(pool) }, null, 2) + "\n");
  return created > 0 ? 0 : 1;
}

async function cmdVerify(args) {
  const cookie = args._[1];
  if (!cookie) { process.stderr.write("要一个 cookie\n"); return 2; }
  process.stdout.write(JSON.stringify(await verify(cookie), null, 2) + "\n");
  return 0;
}

function cmdPool() {
  const { pool } = ctx();
  process.stdout.write(
    JSON.stringify({ line: LINE.id, stats: pool.stats(), instances: instanceSummary(pool), accounts: pool.accounts.map(maskAccount) }, null, 2) + "\n",
  );
  return 0;
}

function cmdExport(args) {
  const { config, pool } = ctx();
  const format = String(args.format ?? "keys");
  const ext = format === "csv" ? "csv" : format === "jsonl" ? "jsonl" : "txt";
  const stamp = new Date().toISOString().replace(/[:.]/g, "-");
  const target = resolve(args.out ?? join(config.dataDir, "exports", `amd-${stamp}.${ext}`));
  process.stdout.write(JSON.stringify(pool.exportTo(target, { format }), null, 2) + "\n");
  return 0;
}

function cmdApply() {
  const { config, pool } = ctx();
  const keys = pool.exportKeys(resolve(config.dataDir, "keys", "amd.txt"));
  const connection = pool.exportConnection(resolve(config.dataDir, "keys", "amd.json"));
  process.stdout.write(JSON.stringify({ ok: true, keys: keys.rows, files: { txt: keys.file, json: connection.file } }, null, 2) + "\n");
  return 0;
}

const args = parseArgs(process.argv.slice(2));
const command = args._[0];
if (!command || command === "help" || command === "-h") { process.stdout.write(USAGE); process.exitCode = 0; }
else if (command === "import") process.exitCode = await cmdImport(args);
else if (command === "verify") process.exitCode = await cmdVerify(args);
else if (command === "pool") process.exitCode = cmdPool();
else if (command === "export") process.exitCode = cmdExport(args);
else if (command === "apply") process.exitCode = cmdApply();
else { process.stderr.write(`unknown command: ${command}\n\n${USAGE}`); process.exitCode = 2; }
