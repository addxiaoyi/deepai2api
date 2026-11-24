#!/usr/bin/env node
// DeepAI Code Gateway — OAuth-linked, OpenAI-compatible reverse proxy with a WebUI.
//
//   node src/index.mjs                 start the gateway + admin UI
//   node src/index.mjs --list-models   print the live model catalog
//   node src/index.mjs --self-test     check the lane with the linked account
//   node src/index.mjs --import-app-account   adopt the desktop app's stored login
//   node src/index.mjs --show-admin-token     print the WebUI password
import { loadConfig } from "./config.mjs";
import { listSecrets, readStoredAccount } from "./credentials.mjs";
import { makeMachineHeader } from "./machine.mjs";
import { makeDispatcher } from "./dispatcher.mjs";
import { makeLane } from "./lane.mjs";
import { makeScheduler } from "./scheduler.mjs";
import { Store } from "./store.mjs";
import { createGateway } from "./server.mjs";

const LEVELS = { silent: 0, error: 1, warn: 2, info: 3, debug: 4 };

let config;
try {
  config = loadConfig();
} catch (error) {
  // Configuration mistakes are operator errors, not crashes: print the hint only.
  process.stderr.write(`\n配置错误：${error.message}\n\n`);
  process.exit(2);
}
const threshold = LEVELS[config.log] ?? LEVELS.info;
const log = (level, message) => {
  if ((LEVELS[level] ?? 9) <= threshold) process.stderr.write(`[${level}] ${message}\n`);
};

const store = Store.at(config.dataDir);
const machineHeader = makeMachineHeader(config.machineRaw);
const { dispatcher, mode, proxy } = makeDispatcher();
const lane = makeLane({
  config,
  getAccount: () => store.account,
  machineHeader,
  dispatcher,
  log,
});
const scheduler = makeScheduler({ store });

const readAppAccount = () =>
  config.apiKey
    ? { apiKey: config.apiKey, username: "(from --api-key)", isPro: false, source: "--api-key" }
    : readStoredAccount(config.secretsDir);

if (config.flags["show-admin-token"]) {
  // Also works before the first start: generating it here persists the same
  // token a later start will read back.
  console.log(config.token ?? createGateway({ config, lane, store, scheduler, log }).adminToken);
  process.exit(0);
}

if (config.flags["import-app-account"]) {
  const account = readAppAccount();
  store.setAccount({
    apiKey: account.apiKey,
    username: account.username,
    isPro: account.isPro,
    connect: account.connect ?? null,
    source: "desktop-app",
  });
  console.log(`已导入桌面端账号：${account.username}（${account.source}）`);
  process.exit(0);
}

if (config.flags["list-models"]) {
  if (!store.account) {
    console.error("还没有连接账号。先启动网关并在管理台登录，或用 --import-app-account 导入桌面端登录。");
    process.exit(1);
  }
  const catalog = await lane.getCatalog({ force: true });
  for (const m of catalog) {
    const price = m.theosPerRequest === undefined ? "" : m.theosPerRequest === "0" ? "免费" : `${m.theosPerRequest} theos`;
    console.log(`${m.id.padEnd(22)} ${String(m.contextWindow).padStart(8)}  ${price}`);
  }
  process.exit(0);
}

if (config.flags["list-secrets"]) {
  for (const s of listSecrets(config.secretsDir)) console.log(`${s.name}.bin ${s.bytes}B`);
  process.exit(0);
}

if (config.flags["self-test"]) {
  if (!store.account) {
    console.error("还没有连接账号。先启动网关并在管理台登录，或用 --import-app-account 导入桌面端登录。");
    process.exit(1);
  }
  const allowance = await lane.getAllowance();
  const catalog = await lane.getCatalog({ force: true });
  console.log(JSON.stringify({ account: store.account.username, models: catalog.length, allowance }, null, 2));
  process.exit(0);
}

const { server, adminToken } = createGateway({ config, lane, store, scheduler, log });

server.listen(config.port, config.host, async () => {
  const machine = await machineHeader().catch(() => null);
  log("info", `network: ${mode}${proxy ? ` via ${proxy}` : ""}`);
  log("info", `machine header: ${machine ?? "(unavailable)"}`);
  log("info", `lane: ${lane.base} (lane version ${config.laneVersion})`);
  log("info", `data: ${config.dataDir}`);
  log("info", `account: ${store.account ? store.account.username : "not linked yet"}`);
  log("info", `listening on http://${config.host}:${config.port}`);

  const lines = [
    "",
    "  ┌────────────────────────────────────────────────────────────┐",
    "  │  DeepAI Code Gateway                                       │",
    "  └────────────────────────────────────────────────────────────┘",
    `  管理台    http://${config.host === "0.0.0.0" ? "127.0.0.1" : config.host}:${config.port}/`,
    `  管理口令  ${adminToken}`,
    `  OpenAI    http://${config.host === "0.0.0.0" ? "127.0.0.1" : config.host}:${config.port}/v1`,
    "",
    store.account
      ? `  账号      ${store.account.username}${store.account.isPro ? " (Pro)" : ""}`
      : "  账号      未连接 —— 打开管理台，用 DeepAI 账号登录",
    "",
  ];
  process.stdout.write(lines.join("\n") + "\n");
});

for (const signal of ["SIGINT", "SIGTERM"]) {
  process.on(signal, () => {
    log("info", `${signal}: shutting down`);
    try {
      store.flush(); // batched usage counters must not be lost
    } catch (error) {
      log("warn", `state flush failed: ${error.message}`);
    }
    server.close(() => process.exit(0));
    setTimeout(() => process.exit(0), 1500).unref();
  });
}

