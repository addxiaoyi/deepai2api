#!/usr/bin/env node
// Import `deepai-reg` accounts into this repo's gateway account pool.
//
//   node tools/deepai-reg/scripts/import-to-gateway.mjs out/accounts.jsonl
//   node tools/deepai-reg/scripts/import-to-gateway.mjs out/accounts.jsonl --data ./data
//
// Reads JSONL rows shaped {apiKey, username, isPro, ...} and upserts them through
// the gateway's own Store, so the file format stays authoritative in one place.
import { readFileSync } from "node:fs";
import { dirname, resolve } from "node:path";
import { fileURLToPath, pathToFileURL } from "node:url";

const here = dirname(fileURLToPath(import.meta.url));
const repoRoot = resolve(here, "../../..");

const args = process.argv.slice(2);
const file = args.find((a) => !a.startsWith("--")) ?? resolve(repoRoot, "tools/deepai-reg/out/accounts.jsonl");
const dataFlag = args.indexOf("--data");
const dataDir = dataFlag >= 0 ? resolve(args[dataFlag + 1]) : resolve(repoRoot, "data");

const { Store } = await import(pathToFileURL(resolve(repoRoot, "src/store.mjs")).href);

const lines = readFileSync(file, "utf8")
  .split(/\r?\n/)
  .map((line) => line.trim())
  .filter(Boolean);

const store = Store.at(dataDir);
let added = 0;
let skipped = 0;

for (const line of lines) {
  let row;
  try {
    row = JSON.parse(line);
  } catch {
    skipped += 1;
    continue;
  }
  const apiKey = typeof row.apiKey === "string" ? row.apiKey.trim() : "";
  if (!apiKey) {
    skipped += 1;
    continue;
  }
  if (store.accounts.some((a) => a.apiKey === apiKey)) {
    skipped += 1;
    continue;
  }
  store.upsertAccount({
    apiKey,
    username: row.username || row.email || "deepai",
    isPro: row.isPro === true,
    connect: null,
  });
  added += 1;
}

store.saveNow?.();
console.log(
  JSON.stringify(
    { file, data: dataDir, added, skipped, accounts: store.accounts.length },
    null,
    2,
  ),
);
