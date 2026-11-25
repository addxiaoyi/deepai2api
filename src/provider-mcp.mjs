#!/usr/bin/env node
// Provider 号池 MCP server（stdio）—— 编程工具调用「注册 / 号池 / 反代」的入口。
//
// 本文件**不自己定义工具**：schema 与 handler 全部来自 `src/provider-tools.mjs`
// 的 `createProviderToolRegistry`，与网关 HTTP 桥（`/api/admin/providers/:id/tools`）
// 和 CLI（`src/provider-cli.mjs`）共用同一份实现。这样三个入口不会各自漂移。
//
// 用法：
//   node src/provider-mcp.mjs                        启动（MCP 客户端拉起）
//   node src/provider-mcp.mjs --print-tools          打印工具清单（MCP + OpenAI function 两种形状）
//   node src/provider-mcp.mjs --provider novita      只装配某一个 provider
//   node src/provider-mcp.mjs --data DIR             号池数据目录（默认 ./data）
//
// 在 Claude Code / Cursor 里只需 `.mcp.json`：
//   {"mcpServers":{"providers":{"command":"node","args":["src/provider-mcp.mjs"]}}}
import { createInterface } from "node:readline";
import { resolve } from "node:path";

import { loadConfig } from "./config.mjs";
import { Store } from "./store.mjs";
import { PROVIDERS, getProvider } from "./provider-catalog.mjs";
import { createProviderRuntime } from "./providers.mjs";
import { DEFAULT_ANTI_BAN } from "./provider-register.mjs";
import { ToolError, createProviderToolRegistry } from "./provider-tools.mjs";

const PROTOCOL_VERSION = "2025-06-18";
const VERSION = "0.1.0";

const argv = process.argv.slice(2);
const argOf = (flag) => {
  const index = argv.indexOf(flag);
  return index !== -1 ? argv[index + 1] : undefined;
};

if (argv.includes("--help") || argv.includes("-h")) {
  process.stdout.write(`provider-mcp — Provider 号池 MCP server (stdio)

  node src/provider-mcp.mjs                     启动（MCP 客户端拉起）
  node src/provider-mcp.mjs --print-tools       打印工具清单后退出
  node src/provider-mcp.mjs --provider novita   只装某一个 provider
  node src/provider-mcp.mjs --data DIR          号池数据目录（默认 ./data）

工具来自 src/provider-tools.mjs，可用 provider：${PROVIDERS.map((p) => p.id).join(", ")}
`);
  process.exit(0);
}

const dataFlag = argOf("--data");
if (dataFlag) process.env.DEEPAI_GATEWAY_DATA = resolve(dataFlag);

const providerFlag = argOf("--provider");
const envProviders = String(process.env.DCG_PROVIDERS ?? "")
  .split(",")
  .map((s) => s.trim())
  .filter(Boolean);
const enabled = providerFlag ? [providerFlag] : envProviders.length ? envProviders : null;

const log = (level, message) => process.stderr.write(`[${level}] provider-mcp: ${message}\n`);

const config = loadConfig([]);
const store = Store.at(config.dataDir);
const runtime = createProviderRuntime({
  dataDir: config.dataDir,
  port: config.port,
  token: config.token ?? process.env.DEEPAI_GATEWAY_TOKEN ?? null,
  store,
  log,
  enabled,
  onAccount: ({ provider, account }) => log("info", `${provider.id} 入池 ${account?.email ?? "?"}`),
});

const tools = createProviderToolRegistry({
  runtime,
  store,
  config,
  log,
  defaultAntiBan: DEFAULT_ANTI_BAN,
});

if (argv.includes("--print-tools")) {
  process.stdout.write(JSON.stringify({ tools: tools.list(), openai: tools.openai() }, null, 2) + "\n");
  process.exit(0);
}

/* --------------------------------------------------------------- transport */

const send = (payload) => process.stdout.write(JSON.stringify(payload) + "\n");
const reply = (id, result) => send({ jsonrpc: "2.0", id, result });
const fail = (id, code, message, data) =>
  send({ jsonrpc: "2.0", id, error: { code, message, ...(data === undefined ? {} : { data }) } });

const toolContent = (result, isError = false) => ({
  content: [{ type: "text", text: typeof result === "string" ? result : JSON.stringify(result, null, 2) }],
  ...(isError ? {} : { structuredContent: result }),
  isError,
});

async function handle(message) {
  const { id, method, params } = message;
  const isNotification = id === undefined || id === null;

  switch (method) {
    case "initialize":
      return reply(id, {
        protocolVersion: typeof params?.protocolVersion === "string" ? params.protocolVersion : PROTOCOL_VERSION,
        capabilities: { tools: { listChanged: false } },
        serverInfo: { name: "provider-pool", version: VERSION },
        instructions:
          "Provider 号池：provider_register 起批量注册（novita 支持域名邮箱 + Turnstile + 协议/指纹浏览器双腿），" +
          "provider_status 轮询进度，provider_pool 看号池，provider_export 导出，" +
          "provider_relay_info 给出编程工具接入配置（OpenAI / Anthropic 兼容 base URL）。",
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
      try {
        return reply(id, toolContent(await tools.call(name, params?.arguments ?? {})));
      } catch (error) {
        const text = error instanceof ToolError ? error.message : String(error?.message ?? error);
        const code = error instanceof ToolError ? error.code : "tool_failed";
        log("error", `${name}: ${text}`);
        return reply(id, toolContent({ ok: false, code, error: text }, true));
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
    .then(() => undefined)
    .catch((error) => {
      log("error", `handler crashed: ${error?.stack ?? error}`);
      if (message?.id !== undefined && message?.id !== null) fail(message.id, -32603, "Internal error");
    });
});

rl.on("close", () => process.exit(0));

log("info", `ready (providers=${[...runtime.pools.keys()].join(",") || "none"}, data=${config.dataDir})`);

export { getProvider };
