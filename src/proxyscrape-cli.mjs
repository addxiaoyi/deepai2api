#!/usr/bin/env node
// proxyscrape CLI：注册 → 代理池 → 分发给其它注册机。
//
//   node src/proxyscrape-cli.mjs register -n 3 --mail-provider imap --mail-domain example.com
//   node src/proxyscrape-cli.mjs proxies                 # 一行一条，可直接喂 --proxy
//   node src/proxyscrape-cli.mjs apply --line cleverai   # 只给某条线写代理文件
//   node src/proxyscrape-cli.mjs export --format proxies --out data/exports/ps.txt
import { existsSync, mkdirSync, readFileSync, writeFileSync } from "node:fs";
import { dirname, join, resolve } from "node:path";
import { fileURLToPath } from "node:url";

import { loadConfig } from "./config.mjs";
import { ProxyscrapePool, maskAccount, maskProxy, parseEndpoint } from "./proxyscrape-pool.mjs";
import { ProxyscrapeRegistrations } from "./proxyscrape-register.mjs";
import { PROXY_LINES, applyProxies, lineProxyFile, proxyShareStatus } from "./proxy-share.mjs";
import {
  DEFAULT_FORMAT as DEFAULT_PROXY_FORMAT,
  FORMATS as PROXY_FORMATS,
  ShareStore as ProxyShareStore,
  exportFileName as proxyExportFileName,
  maskShare as maskProxyShare,
  renderProxies,
} from "./proxy-export.mjs";
import { probeProxies } from "./proxy-check.mjs";

const here = dirname(fileURLToPath(import.meta.url));
const repoRoot = resolve(here, "..");

const USAGE = `proxyscrape-cli — 试用注册 + 代理池 + 出口分发

  register [options]        批量注册并领试用（产物 = 账号 + 代理）
    -n, --count <n>         数量（默认 1）
    -c, --concurrency <n>   并发（默认 1，试用按出口发，别贪）
        --mail-provider <p> imap | fixed | mailtm | tempmail_lol
        --mail-domain <d>   邮箱域名（可重复）
        --captcha <p>       browser | capsolver | manual | none
        --captcha-key <k>   capsolver clientKey（manual 时是 token）
        --engine <e>        auto | protocol | browser
        --proxy <p>         注册用出口，可重复
        --no-verify         跳过邮箱验证
        --no-trial          只注册，不领试用
        --export <fmt>      proxies | jsonl | csv
        --json              NDJSON 事件

  pool                     账号池（脱敏）
  proxies [--limit n]      打印可用代理，一行一条
  export [--format f]      导出 proxies | jsonl | csv | pool
  import <file.jsonl>      外部账号 + 代理入池
  apply [--line <line>]    把代理分发给注册机（写 data/proxies/<line>.txt）
  share                    看每条线现在分到几条

  export-format [--format f] [--alive] [--out file]
                           导出给别的设备用：
                             url | raw | at | json | csv
                             sub2api | sub2api-txt | cpa
                             clash | singbox | surge | env
  check [--timeout ms]     探测存活（TCP），把死代理标进冷却
  subscription add --name <n> [--format f]   生成订阅令牌（手机/路由器用）
  subscription list        列出订阅链接
  subscription revoke <t>  撤销订阅
`;

/** 订阅链接的 host：优先 PROXYSCRAPE_PUBLIC_BASE，其次网关公开地址，最后本机。 */
function publicBase(config) {
  const explicit = process.env.PROXYSCRAPE_PUBLIC_BASE || process.env.DEEPAI_PUBLIC_BASE || config.publicBase;
  return String(explicit || `http://127.0.0.1:${config.port}`).replace(/\/+$/, "");
}

function poolRows(pool) {
  return pool.usable().map((proxy) => ({
    scheme: proxy.scheme,
    host: proxy.host,
    port: proxy.port,
    username: proxy.username,
    password: proxy.password,
    endpoint: proxy.endpoint,
    status: proxy.status,
    cooldownUntil: proxy.cooldownUntil ?? 0,
  }));
}

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
  const pool = ProxyscrapePool.at(config.dataDir, {
    baseUrl: process.env.PROXYSCRAPE_BASE_URL || "https://dashboard.proxyscrape.com",
    log: (level, message) => process.stderr.write(`[${level}] ${message}\n`),
  });
  return { config, pool };
}

async function cmdRegister(args) {
  const { config, pool } = makeContext();
  const registrations = new ProxyscrapeRegistrations({
    dataDir: config.dataDir,
    port: config.port,
    token: config.token ?? null,
    pool,
    log: (level, message) => process.stderr.write(`[${level}] ${message}\n`),
    onAccount: ({ account, proxies }) => {
      if (!args.json) process.stdout.write(`  + ${account.email}（+${proxies} 条代理）\n`);
    },
  });

  const snapshot = registrations.start({
    count: Number(args.count ?? args.n ?? 1),
    concurrency: Number(args.concurrency ?? args.c ?? 1),
    mailProvider: args["mail-provider"],
    mailDomains: multi(args["mail-domain"]),
    captcha: args.captcha,
    captchaKey: args["captcha-key"],
    engine: args.engine,
    browserStack: args["browser-stack"],
    headful: args.headful === true,
    cdp: args.cdp,
    proxies: multi(args.proxy),
    verifyEmail: args["no-verify"] !== true,
    claimTrial: args["no-trial"] !== true,
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
    proxies: finished.job?.proxies ?? 0,
    failed: finished.job?.failed ?? 0,
    share: finished.job?.share ?? null,
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

function cmdProxies(args) {
  const { pool } = makeContext();
  const limit = Number(args.limit ?? 0);
  const rows = pool.usable().map((p) => p.endpoint);
  const out = limit > 0 ? rows.slice(0, limit) : rows;
  process.stdout.write(out.length ? out.join("\n") + "\n" : "");
  return out.length ? 0 : 1;
}

function cmdExport(args) {
  const { config, pool } = makeContext();
  const format = String(args.format ?? "proxies");
  if (!["proxies", "jsonl", "csv", "pool"].includes(format)) {
    process.stderr.write(`unknown format: ${format}\n`);
    return 2;
  }
  const stamp = new Date().toISOString().replace(/[:.]/g, "-");
  const ext = format === "csv" ? "csv" : format === "jsonl" ? "jsonl" : format === "pool" ? "json" : "txt";
  const target = resolve(args.out ?? join(config.dataDir, "exports", `proxyscrape-${stamp}.${ext}`));
  const result = pool.exportTo(target, { format });
  process.stdout.write(JSON.stringify(result, null, 2) + "\n");
  return 0;
}

function cmdImport(args) {
  const { pool } = makeContext();
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
  let proxies = 0;
  for (const row of rows) {
    if (!row.email) continue;
    const result = pool.add(row);
    proxies += result.proxies;
    if (result.created) created++;
    else updated++;
  }
  const share = applyProxies({ pool, dataDir: makeContext().config.dataDir });
  process.stdout.write(JSON.stringify({ imported: created, updated, proxies, share, pool: pool.stats() }, null, 2) + "\n");
  return 0;
}

function cmdApply(args) {
  const { config, pool } = makeContext();
  const line = args.line ? String(args.line) : "";
  const lines = line ? [line] : PROXY_LINES;
  const share = applyProxies({ pool, dataDir: config.dataDir, lines, limitPerLine: Number(args.limit ?? 0) });
  process.stdout.write(JSON.stringify(share, null, 2) + "\n");
  return share.total > 0 ? 0 : 1;
}

function cmdShare() {
  const { config } = makeContext();
  process.stdout.write(JSON.stringify(proxyShareStatus(config.dataDir), null, 2) + "\n");
  return 0;
}

/* ------------------------------------------------- 导出 / 探测 / 订阅 */

async function cmdExportFormat(args) {
  const { config, pool } = makeContext();
  const format = PROXY_FORMATS[String(args.format ?? DEFAULT_PROXY_FORMAT)]
    ? String(args.format ?? DEFAULT_PROXY_FORMAT)
    : DEFAULT_PROXY_FORMAT;
  let rows = poolRows(pool);
  if (args.alive === true) {
    const { alive } = await probeProxies(rows, { timeoutMs: Number(args.timeout ?? 4000), concurrency: 8 });
    rows = alive.map((item) => item.proxy);
  }
  if (args.limit) rows = rows.slice(0, Number(args.limit));
  const body = renderProxies(rows, format, { name: String(args.name ?? "proxyscrape") });
  if (args.out) {
    const target = resolve(String(args.out));
    mkdirSync(dirname(target), { recursive: true });
    writeFileSync(target, body, { mode: 0o600 });
    process.stdout.write(JSON.stringify({ ok: true, file: target, format, rows: rows.length }, null, 2) + "\n");
    return 0;
  }
  process.stdout.write(body);
  return rows.length ? 0 : 1;
}

async function cmdCheck(args) {
  const { pool } = makeContext();
  const rows = poolRows(pool);
  const { alive, dead } = await probeProxies(rows, {
    timeoutMs: Number(args.timeout ?? 4000),
    concurrency: Number(args.concurrency ?? 8),
  });
  for (const item of alive) pool.recordProxy(item.proxy.endpoint, { ok: true });
  for (const item of dead) pool.recordProxy(item.proxy.endpoint, { ok: false, error: item.error ?? "unreachable" });
  process.stdout.write(
    JSON.stringify(
      {
        total: rows.length,
        alive: alive.length,
        dead: dead.length,
        results: [...alive, ...dead].map((item) => ({
          endpoint: `${item.proxy.scheme}://${item.proxy.host}:${item.proxy.port}`,
          ok: item.ok,
          ms: item.ms,
          error: item.error ?? null,
        })),
      },
      null,
      2,
    ) + "\n",
  );
  return alive.length ? 0 : 1;
}

function cmdSubscription(args) {
  const { config } = makeContext();
  const store = new ProxyShareStore(config.dataDir);
  const action = args._[1] ?? "list";
  if (action === "add" || action === "mint") {
    const minted = store.mint({
      name: String(args.name ?? ""),
      format: String(args.format ?? DEFAULT_PROXY_FORMAT),
      note: String(args.note ?? ""),
    });
    process.stdout.write(
      JSON.stringify(
        {
          ok: true,
          reused: minted.reused,
          name: minted.name,
          format: minted.format,
          token: minted.token,
          url: `${publicBase(config)}/proxy/${minted.token}`,
        },
        null,
        2,
      ) + "\n",
    );
    return 0;
  }
  if (action === "revoke") {
    const token = args._[2] ?? String(args.token ?? "");
    const ok = store.revoke(token);
    process.stdout.write(JSON.stringify({ ok, token: ok ? `${token.slice(0, 6)}…` : null }, null, 2) + "\n");
    return ok ? 0 : 1;
  }
  const base = publicBase(config);
  process.stdout.write(
    JSON.stringify(
      {
        publicBase: base,
        shares: store.list().map((share) => ({
          ...maskProxyShare(share),
          url: share.revoked ? null : `${base}/proxy/${share.token}`,
        })),
      },
      null,
      2,
    ) + "\n",
  );
  return 0;
}
async function main() {
  const args = parseArgs(process.argv.slice(2));
  const command = args._[0];
  if (!command || command === "help" || command === "-h") {
    process.stdout.write(USAGE);
    return 0;
  }
  const toolDir = process.env.PROXYSCRAPE_REGISTER_DIR || join(repoRoot, "tools", "proxyscrape-reg");
  if (command === "register" && !existsSync(join(toolDir, "proxyscrape_reg", "cli.py"))) {
    process.stderr.write(`找不到注册工具：${toolDir}\n`);
    return 2;
  }
  switch (command) {
    case "register":
      return cmdRegister(args);
    case "pool":
      return cmdPool();
    case "proxies":
      return cmdProxies(args);
    case "export":
      return cmdExport(args);
    case "import":
      return cmdImport(args);
    case "apply":
      return cmdApply(args);
    case "share":
      return cmdShare();
    case "export-format":
      return cmdExportFormat(args);
    case "check":
      return cmdCheck(args);
    case "subscription":
      return cmdSubscription(args);
    default:
      process.stderr.write(`unknown command: ${command}\n\n${USAGE}`);
      return 2;
  }
}

process.exitCode = await main();
