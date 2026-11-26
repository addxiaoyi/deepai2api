#!/usr/bin/env node
// 待配置项 CLI：哪些线还差外部条件，一眼看清。
//
//   node scripts/pending.mjs list           # 全部
//   node scripts/pending.mjs list --line agnes
//   node scripts/pending.mjs check          # 按当前环境变量判断（缺什么）
//   node scripts/pending.mjs check --json
//   node scripts/pending.mjs md             # 写 docs/PENDING-CONFIG.md
import { mkdirSync, writeFileSync } from "node:fs";
import { dirname, resolve } from "node:path";

import { PENDING, checkEnv, forLine, summary, toMarkdown } from "../src/pending-config.mjs";

const args = process.argv.slice(2);
const command = args[0] ?? "list";
const flag = (name, fallback = "") => (args.includes(name) ? args[args.indexOf(name) + 1] ?? fallback : fallback);
const asJson = args.includes("--json");

function mark(status) {
  return status === "solved" ? "✅" : status === "blocked" ? "⛔" : "🟡";
}

function cmdList() {
  const line = flag("--line");
  const rows = line ? forLine(line) : PENDING;
  if (!rows.length) {
    process.stderr.write(`没有匹配的待配置项（line=${line}）\n`);
    return 2;
  }
  if (asJson) {
    process.stdout.write(JSON.stringify({ items: rows, summary: summary() }, null, 2) + "\n");
    return 0;
  }
  const pad = (v, n) => String(v).padEnd(n);
  process.stdout.write(`${pad("", 2)} ${pad("id", 24)} ${pad("line", 14)} ${pad("kind", 11)} ${pad("status", 8)} title\n`);
  process.stdout.write("-".repeat(110) + "\n");
  for (const item of rows) {
    process.stdout.write(
      `${mark(item.status)} ${pad(item.id, 24)} ${pad(item.line, 14)} ${pad(item.kind, 11)} ${pad(item.status, 8)} ${item.title}\n`,
    );
  }
  const s = summary();
  process.stdout.write(`\n共 ${s.total} 项：open ${s.open} / blocked ${s.blocked} / solved ${s.solved}\n`);
  return 0;
}

function cmdCheck() {
  const line = flag("--line");
  const rows = checkEnv(process.env).filter((row) => !line || row.line === line || row.line === "all");
  if (asJson) {
    process.stdout.write(JSON.stringify({ checks: rows }, null, 2) + "\n");
    return 0;
  }
  for (const row of rows) {
    const icon = row.status === "solved" ? "✅" : row.ready ? "🟢" : "🟡";
    const missing = row.status === "solved" ? "" : row.missing.length ? `  缺：${row.missing.join(", ")}` : "";
    process.stdout.write(`${icon} ${row.id.padEnd(24)} ${row.line.padEnd(14)} ${row.status.padEnd(8)}${missing}\n`);
  }
  const ready = rows.filter((row) => row.ready).length;
  process.stdout.write(`\n${ready}/${rows.length} 项当前环境已就绪\n`);
  return ready === rows.length ? 0 : 1;
}

function cmdMd() {
  const target = resolve(flag("--out", "docs/PENDING-CONFIG.md"));
  mkdirSync(dirname(target), { recursive: true });
  writeFileSync(target, toMarkdown() + "\n", "utf8");
  process.stdout.write(`已写入 ${target}\n`);
  return 0;
}

switch (command) {
  case "list":
    process.exitCode = cmdList();
    break;
  case "check":
    process.exitCode = cmdCheck();
    break;
  case "md":
    process.exitCode = cmdMd();
    break;
  case "summary":
    process.stdout.write(JSON.stringify(summary(), null, 2) + "\n");
    break;
  default:
    process.stderr.write(`用法：node scripts/pending.mjs [list|check|md|summary] [--line <line>] [--json]\n`);
    process.exitCode = 2;
}
