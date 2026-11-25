#!/usr/bin/env node
// Provider CLI：批量注册 / 查号池 / 导出 Key / 起反代说明。
//
//   node src/provider-cli.mjs list
//   node src/provider-cli.mjs status --provider novita
//   node src/provider-cli.mjs register --provider novita -n 5 -c 1 \
//        --mail-provider imap --mail-domain your-domain.com \
//        --captcha capsolver --captcha-key KEY
//   node src/provider-cli.mjs pool --provider wavespeed
//   node src/provider-cli.mjs export --provider novita --format csv
//   node src/provider-cli.mjs import --provider novita accounts.jsonl
//   node src/provider-cli.mjs relay --provider novita
//
// 重活都在 tools/<provider>-reg（Python）；这个二进制只管号池、网关 Key 和导出形状。
import { existsSync, readFileSync } from "node:fs";
import { resolve } from "node:path";

import { loadConfig } from "./config.mjs";
import { Store } from "./store.mjs";
import { PROVIDERS } from "./provider-catalog.mjs";
import { createProviderRuntime } from "./providers.mjs";
import { DEFAULT_ANTI_BAN } from "./provider-register.mjs";
import { createProviderToolRegistry } from "./provider-tools.mjs";

const USAGE = `provider-cli — 多 provider 号池：批量注册 / 建 key / 导出 / 反代接入

  list                      列出所有 provider 与号池概况
  status --provider <id>    看某个 provider 的 job 进度与失败分类
  register [options]        批量注册（每个账号自动建网关 key 并写号池）
    --provider <id>         必填，如 novita / wavespeed
    -n, --count <n>         数量（默认 1）
    -c, --concurrency <n>   并发（默认 2；同 IP 建议 1）
        --engine <e>        auto | protocol | browser
        --browser-stack <s> auto | camoufox | patchright | playwright
        --cdp <url>         接管外部指纹浏览器，如 http://127.0.0.1:50325
        --mail-provider <p> imap | domain | mailtm | tempmail_lol | fixed
        --mail-domain <d>   域名邮箱（可重复）
        --captcha <p>       auto | none | capsolver | 2captcha | browser | manual
        --captcha-key <k>   解题平台 key
        --keys-per-account  每个账号再额外建几个 key
        --anti-ban <json>   防封策略覆盖
        --key-policy <json> key 策略
        --proxy <p>         代理，可重复
        --no-verify         跳过邮箱验证轮询
        --session-file <f>  复用登录会话 cookies（blackbox 协议腿）
        --google-pool <f>   Google 身份池 JSONL（blackbox 浏览器腿）
        --keys-path <p>     覆盖面板 Key 列表接口（探测失败时用）
        --keys-create-path <p>  覆盖面板建 Key 接口
        --export <fmt>      结束后导出 jsonl | csv | pool
        --json              以 NDJSON 打印 worker 事件
  stop --provider <id>      停止当前注册
  pool --provider <id>      列出号池账号（脱敏）
  export --provider <id>    导出账号 + key（jsonl | csv | pool）
  import --provider <id> <file.jsonl>   导入外部账号并补发网关 key
  relay --provider <id>     打印 OpenAI 兼容接入地址与用法
  tools                     打印工具清单（MCP / OpenAI function 形状）
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

const parseJsonArg = (value, label) => {
  if (value === undefined || value === true) return undefined;
  try {
    return JSON.parse(String(value));
  } catch {
    process.stderr.write(`${label} 不是合法 JSON：${String(value).slice(0, 80)}\n`);
    return undefined;
  }
};

function makeRuntime({ only = null } = {}) {
  const config = loadConfig([]);
  const store = Store.at(config.dataDir);
  const runtime = createProviderRuntime({
    dataDir: config.dataDir,
    port: config.port,
    token: config.token ?? process.env.DEEPAI_GATEWAY_TOKEN ?? null,
    store,
    config,
    log: (level, message) => process.stderr.write(`[${level}] ${message}\n`),
    enabled: only,
  });
  const tools = createProviderToolRegistry({
    runtime,
    store,
    config,
    log: () => {},
    defaultAntiBan: DEFAULT_ANTI_BAN,
  });
  return { config, store, runtime, tools };
}

function pickProvider(runtime, args, { required = true } = {}) {
  const id = String(args.provider ?? "").trim().toLowerCase();
  if (!id) {
    if (!required) return null;
    process.stderr.write(`需要 --provider（可选：${[...runtime.pools.keys()].join(", ")}）\n`);
    return null;
  }
  try {
    return runtime.requireProvider(id);
  } catch (error) {
    process.stderr.write(`${error.message}\n`);
    return null;
  }
}

async function cmdList() {
  const { runtime } = makeRuntime();
  const rows = runtime.list().map((row) => ({
    provider: row.id,
    label: row.label,
    accounts: row.pool.total,
    healthy: row.pool.active,
    keys: row.pool.keys,
    running: row.running,
    worker: row.toolPresent ? "ready" : "missing",
    openaiBase: `/p/${row.id}/v1`,
    apiBase: row.apiBase,
  }));
  process.stdout.write(JSON.stringify({ providers: rows }, null, 2) + "\n");
  return 0;
}

async function cmdStatus(args) {
  const { runtime, tools } = makeRuntime();
  const found = pickProvider(runtime, args, { required: false });
  if (found) {
    const result = await tools.call("provider_status", { provider: found.provider.id });
    process.stdout.write(JSON.stringify(result, null, 2) + "\n");
    return 0;
  }
  const all = [];
  for (const provider of runtime.providers) {
    all.push(await tools.call("provider_status", { provider: provider.id }));
  }
  process.stdout.write(JSON.stringify({ providers: all }, null, 2) + "\n");
  return 0;
}

async function cmdRegister(args) {
  const { runtime, tools } = makeRuntime();
  const found = pickProvider(runtime, args);
  if (!found) return 2;
  const providerId = found.provider.id;

  const antiBan = parseJsonArg(args["anti-ban"], "--anti-ban");
  const keyPolicy = parseJsonArg(args["key-policy"], "--key-policy");

  const snapshot = await tools.call("provider_register", {
    provider: providerId,
    count: Number(args.count ?? args.n ?? 1),
    concurrency: Number(args.concurrency ?? args.c ?? 2),
    engine: args.engine,
    browserStack: args["browser-stack"],
    cdp: args.cdp,
    headful: args.headful === true,
    mailProvider: args["mail-provider"],
    mailDomains: multi(args["mail-domain"]),
    captcha: args.captcha,
    captchaKey: args["captcha-key"],
    keysPerAccount: args["keys-per-account"] !== undefined ? Number(args["keys-per-account"]) : undefined,
    antiBan,
    keyPolicy,
    verifyEmail: args["no-verify"] !== true,
    // blackbox 这类 OAuth provider 专属（其它 provider 的 worker 会忽略）
    sessionFile: args["session-file"],
    googlePool: args["google-pool"],
    keysPath: args["keys-path"],
    keysCreatePath: args["keys-create-path"],
    export: args.export,
    proxies: multi(args.proxy),
    wait: false,
  }).catch((error) => {
    process.stderr.write(`${error.message}\n`);
    return null;
  });
  if (!snapshot) return 1;

  process.stderr.write(`job ${snapshot.job.id} started: ${snapshot.job.requested} × ${snapshot.job.concurrency}\n`);

  // 控制器基于子进程；这里等到它 settle。
  const registrations = found.registrations;
  const finished = await new Promise((done) => {
    const timer = setInterval(() => {
      const status = registrations.status();
      if (status.job?.status !== "running") {
        clearInterval(timer);
        done(status);
      }
    }, 1000);
  });

  const summary = {
    provider: providerId,
    status: finished.job?.status,
    created: finished.job?.created ?? 0,
    failed: finished.job?.failed ?? 0,
    pool: found.pool.stats(),
  };
  if (args.export) summary.export = registrations.export({ format: String(args.export) });
  process.stdout.write(JSON.stringify(summary, null, 2) + "\n");
  return summary.created > 0 ? 0 : 1;
}

async function cmdStop(args) {
  const { runtime } = makeRuntime();
  const found = pickProvider(runtime, args);
  if (!found) return 2;
  const result = found.registrations.stop();
  process.stdout.write(JSON.stringify({ provider: found.provider.id, ...result }, null, 2) + "\n");
  return 0;
}

async function cmdPool(args) {
  const { runtime, tools } = makeRuntime();
  const found = pickProvider(runtime, args);
  if (!found) return 2;
  const result = await tools.call("provider_pool", {
    provider: found.provider.id,
    limit: Number(args.limit ?? 100),
  });
  process.stdout.write(JSON.stringify(result, null, 2) + "\n");
  return 0;
}

async function cmdExport(args) {
  const { runtime, tools } = makeRuntime();
  const found = pickProvider(runtime, args);
  if (!found) return 2;
  const format = String(args.format ?? "jsonl");
  if (!["jsonl", "csv", "pool"].includes(format)) {
    process.stderr.write(`unknown format: ${format}\n`);
    return 2;
  }
  const result = await tools.call("provider_export", {
    provider: found.provider.id,
    format,
    file: args.out ? resolve(args.out) : undefined,
  });
  process.stdout.write(JSON.stringify(result, null, 2) + "\n");
  return 0;
}

async function cmdImport(args) {
  const { runtime, tools } = makeRuntime();
  const found = pickProvider(runtime, args);
  if (!found) return 2;
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
    const apiKey = row.apiKey ?? row.api_key;
    if (!row.email || !apiKey) continue;
    const result = await tools.call("provider_import", {
      provider: found.provider.id,
      email: row.email,
      apiKey,
      password: row.password,
      keyPrefix: row.keyPrefix ?? row.key_prefix,
      keyLast4: row.keyLast4 ?? row.key_last4,
      expireTime: row.expireTime ?? row.expire_time,
      engine: row.engine,
      mintKey: args["no-mint-key"] !== true,
    });
    if (result.created) created++;
    else updated++;
  }
  process.stdout.write(
    JSON.stringify({ provider: found.provider.id, imported: created, updated, pool: found.pool.stats() }, null, 2) + "\n",
  );
  return 0;
}

async function cmdRelay(args) {
  const { runtime, tools } = makeRuntime();
  const found = pickProvider(runtime, args);
  if (!found) return 2;
  const result = await tools.call("provider_relay_info", {
    provider: found.provider.id,
    publicBase: args["public-base"],
  });
  process.stdout.write(JSON.stringify(result, null, 2) + "\n");
  const hint = [
    "",
    "OpenAI SDK 用法：",
    `  base_url = "${result.baseUrl}"`,
    `  api_key  = "sk-dcg-…"   # 用 provider_export 或号池文件里的明文`,
    "",
  ].join("\n");
  process.stderr.write(hint);
  return 0;
}

async function cmdTools() {
  const { tools } = makeRuntime();
  process.stdout.write(JSON.stringify({ tools: tools.list(), openai: tools.openai() }, null, 2) + "\n");
  return 0;
}

async function main() {
  const args = parseArgs(process.argv.slice(2));
  const command = args._[0];
  if (!command || command === "help" || command === "-h") {
    process.stdout.write(USAGE);
    return 0;
  }
  switch (command) {
    case "list":
      return cmdList();
    case "status":
      return cmdStatus(args);
    case "register":
      return cmdRegister(args);
    case "stop":
      return cmdStop(args);
    case "pool":
      return cmdPool(args);
    case "export":
      return cmdExport(args);
    case "import":
      return cmdImport(args);
    case "relay":
      return cmdRelay(args);
    case "tools":
      return cmdTools();
    default:
      process.stderr.write(`unknown command: ${command}\n\n${USAGE}`);
      return 2;
  }
}

process.exitCode = await main();

export { PROVIDERS };
