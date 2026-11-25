#!/usr/bin/env node
// Allmodels 号池 MCP server（stdio）。
//
// 让 Claude Code / Cursor / Codex CLI / VS Code 这类编程工具，把「批量注册 Allmodels
// 账号、自动建 Key、写号池、导出」当成普通工具调用：
//
//   {"jsonrpc":"2.0","id":1,"method":"initialize",...}
//   {"jsonrpc":"2.0","id":2,"method":"tools/list"}
//   {"jsonrpc":"2.0","id":3,"method":"tools/call","params":{"name":"allmodels_register","arguments":{"count":5}}}
//
// 传输是 stdio 上的行分隔 JSON-RPC 2.0（MCP 规范）；stdout 只走协议，日志一律 stderr。
// 手工试用：node src/allmodels-mcp.mjs --print-tools
import { createInterface } from "node:readline";
import { dirname, resolve } from "node:path";
import { fileURLToPath } from "node:url";

import { loadConfig } from "./config.mjs";
import { AllmodelsPool } from "./allmodels-pool.mjs";
import { AllmodelsRegistrations } from "./allmodels-register.mjs";
import { Store } from "./store.mjs";
import { ToolError, createToolRegistry } from "./allmodels-tools.mjs";

const here = dirname(fileURLToPath(import.meta.url));
const pkgVersion = "0.2.0";
const PROTOCOL_VERSION = "2025-06-18";

const argv = process.argv.slice(2);
if (argv.includes("--help") || argv.includes("-h")) {
  process.stdout.write(`allmodels-mcp — Allmodels 号池 MCP server (stdio)

  node src/allmodels-mcp.mjs                 启动（MCP 客户端拉起）
  node src/allmodels-mcp.mjs --print-tools   打印工具清单后退出
  node src/allmodels-mcp.mjs --data DIR      指定号池数据目录（默认 ./data）

工具：allmodels_status / allmodels_register / allmodels_pool / allmodels_export /
      allmodels_import / allmodels_keys / allmodels_mark_health / allmodels_remove /
      allmodels_probe
`);
  process.exit(0);
}

const dataFlag = argv.indexOf("--data");
if (dataFlag !== -1 && argv[dataFlag + 1]) process.env.DEEPAI_GATEWAY_DATA = resolve(argv[dataFlag + 1]);

const log = (level, message) => process.stderr.write(`[${level}] allmodels-mcp: ${message}\n`);

const config = loadConfig([]);
const store = Store.at(config.dataDir);
const pool = AllmodelsPool.at(config.dataDir, {
  baseUrl: process.env.ALLMODELS_BASE_URL || "https://dash.allmodels.io",
  apiBase: process.env.ALLMODELS_API_BASE || "https://api.allmodels.io/v1",
  log: (level, message) => log(level, message),
});
const registrations = new AllmodelsRegistrations({
  dataDir: config.dataDir,
  port: config.port,
  token: config.token ?? process.env.DEEPAI_GATEWAY_TOKEN ?? null,
  pool,
  store,
  log: (level, message) => log(level, message),
  onAccount: ({ account }) => log("info", `入池 ${account.email}（号池 ${pool.size}）`),
});
const tools = createToolRegistry({ pool, store, registrations, config, log });

if (argv.includes("--print-tools")) {
  process.stdout.write(JSON.stringify({ tools: tools.list(), openai: tools.openai() }, null, 2) + "\n");
  process.exit(0);
}

/* --------------------------------------------------------------- transport */

const send = (payload) => process.stdout.write(JSON.stringify(payload) + "\n");

const reply = (id, result) => send({ jsonrpc: "2.0", id, result });
const fail = (id, code, message, data) =>
  send({ jsonrpc: "2.0", id, error: { code, message, ...(data === undefined ? {} : { data }) } });

function toolContent(result, isError = false) {
  return {
    content: [{ type: "text", text: typeof result === "string" ? result : JSON.stringify(result, null, 2) }],
    structuredContent: isError ? undefined : result,
    isError,
  };
}

async function handle(message) {
  const { id, method, params } = message;
  const isNotification = id === undefined || id === null;

  switch (method) {
    case "initialize":
      return reply(id, {
        protocolVersion: typeof params?.protocolVersion === "string" ? params.protocolVersion : PROTOCOL_VERSION,
        capabilities: { tools: { listChanged: false } },
        serverInfo: { name: "allmodels-pool", version: pkgVersion },
        instructions:
          "Allmodels 号池：allmodels_register 批量注册账号并自动创建网关 Key；allmodels_status 轮询进度；" +
          "allmodels_export 统一导出账号与 Key。默认 data 目录与网关一致（./data）。",
      });

    case "notifications/initialized":
    case "notifications/cancelled":
      return undefined;

    case "ping":
      return reply(id, {});

    case "tools/list":
      return reply(id, { tools: tools.list() });

    case "tools/call": {
      const name = String(params?.name ?? "");
      const args = params?.arguments ?? {};
      try {
        const result = await tools.call(name, args);
        return reply(id, toolContent(result));
      } catch (error) {
        const messageText = error instanceof ToolError ? error.message : String(error?.message ?? error);
        const code = error instanceof ToolError ? error.code : "tool_failed";
        log("error", `${name}: ${messageText}`);
        return reply(id, toolContent({ ok: false, code, error: messageText }, true));
      }
    }

    case "resources/list":
      return reply(id, { resources: [] });
    case "prompts/list":
      return reply(id, { prompts: [] });
    case "logging/setLevel":
      return reply(id, {});

    default:
      if (isNotification) return undefined;
      return fail(id, -32601, `Method not found: ${method}`);
  }
}

const rl = createInterface({ input: process.stdin, crlfDelay: Infinity });
rl.on("line", (line) => {
  const trimmed = line.trim();
  if (!trimmed) return;
  let message;
  try {
    message = JSON.parse(trimmed);
  } catch {
    fail(null, -32700, "Parse error");
    return;
  }
  handle(message)
    .then((result) => {
      if (result !== undefined) void result;
    })
    .catch((error) => {
      log("error", `handler crashed: ${error?.stack ?? error}`);
      if (message?.id !== undefined && message?.id !== null) fail(message.id, -32603, "Internal error");
    });
});

rl.on("close", () => {
  try {
    store.flush?.();
  } catch {
    // shutting down anyway
  }
  process.exit(0);
});

log("info", `ready (data=${config.dataDir}, pool=${pool.size})`);
