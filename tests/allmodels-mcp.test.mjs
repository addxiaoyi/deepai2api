// MCP over stdio: handshake, tool discovery, tool calls.
//
// Spawns the real server the way a coding tool would (JSON-RPC lines on stdio)
// and drives it against a throwaway data dir.
import { test } from "node:test";
import assert from "node:assert/strict";
import { spawn } from "node:child_process";
import { existsSync, mkdtempSync, readFileSync, rmSync } from "node:fs";
import { tmpdir } from "node:os";
import { dirname, join, resolve } from "node:path";
import { fileURLToPath } from "node:url";

const repoRoot = resolve(dirname(fileURLToPath(import.meta.url)), "..");

function startServer(dataDir) {
  const child = spawn(process.execPath, [join(repoRoot, "src", "allmodels-mcp.mjs"), "--data", dataDir], {
    cwd: repoRoot,
    stdio: ["pipe", "pipe", "pipe"],
  });
  const pending = new Map();
  let buffer = "";
  let stderr = "";
  child.stderr.setEncoding("utf8");
  child.stderr.on("data", (chunk) => (stderr += chunk));
  child.stdout.setEncoding("utf8");
  child.stdout.on("data", (chunk) => {
    buffer += chunk;
    let index;
    while ((index = buffer.indexOf("\n")) !== -1) {
      const line = buffer.slice(0, index).trim();
      buffer = buffer.slice(index + 1);
      if (!line) continue;
      let message;
      try {
        message = JSON.parse(line);
      } catch {
        continue;
      }
      const waiter = pending.get(message.id);
      if (waiter) {
        pending.delete(message.id);
        waiter(message);
      }
    }
  });

  let nextId = 1;
  const rpc = (method, params) =>
    new Promise((done, fail) => {
      const id = nextId++;
      pending.set(id, done);
      child.stdin.write(JSON.stringify({ jsonrpc: "2.0", id, method, params }) + "\n");
      setTimeout(() => {
        if (pending.delete(id)) fail(new Error(`timeout waiting for ${method}\n${stderr.slice(-800)}`));
      }, 60_000).unref();
    });

  return { child, rpc, stderr: () => stderr };
}

test("MCP handshake, tool list and tool calls", async (t) => {
  const dataDir = mkdtempSync(join(tmpdir(), "am-mcp-"));
  const server = startServer(dataDir);
  t.after(() => {
    server.child.kill();
    rmSync(dataDir, { recursive: true, force: true });
  });

  const init = await server.rpc("initialize", {
    protocolVersion: "2025-06-18",
    capabilities: {},
    clientInfo: { name: "unit-test", version: "0" },
  });
  assert.equal(init.result.serverInfo.name, "allmodels-pool");
  assert.equal(init.result.protocolVersion, "2025-06-18");
  assert.ok(init.result.capabilities.tools);

  const listed = await server.rpc("tools/list", {});
  const names = listed.result.tools.map((tool) => tool.name);
  for (const expected of [
    "allmodels_status",
    "allmodels_register",
    "allmodels_pool",
    "allmodels_export",
    "allmodels_import",
    "allmodels_keys",
    "allmodels_mark_health",
    "allmodels_remove",
    "allmodels_probe",
  ]) {
    assert.ok(names.includes(expected), `missing tool ${expected}`);
  }
  const register = listed.result.tools.find((tool) => tool.name === "allmodels_register");
  assert.equal(register.inputSchema.properties.engine.enum.includes("browser"), true);

  const imported = await server.rpc("tools/call", {
    name: "allmodels_import",
    arguments: {
      email: "mcp@example.com",
      apiKey: "sk_live_mcp_test_0123456789abcdef",
      projectId: "proj1",
      guardrailId: "gr1",
      emailVerified: true,
    },
  });
  assert.equal(imported.result.isError, false);
  const payload = imported.result.structuredContent;
  assert.equal(payload.ok, true);
  assert.ok(payload.gatewayKey.prefix.startsWith("sk-dcg-"));

  const pool = await server.rpc("tools/call", { name: "allmodels_pool", arguments: {} });
  assert.equal(pool.result.structuredContent.accounts.length, 1);
  assert.ok(pool.result.structuredContent.accounts[0].apiKeyPreview.includes("…"));

  const exported = await server.rpc("tools/call", {
    name: "allmodels_export",
    arguments: { format: "jsonl" },
  });
  const file = exported.result.structuredContent.file;
  assert.ok(existsSync(file));
  assert.match(readFileSync(file, "utf8"), /sk-dcg-/);

  const health = await server.rpc("tools/call", {
    name: "allmodels_mark_health",
    arguments: { email: "mcp@example.com", ok: false, error: "429", cooldownMs: 60_000 },
  });
  assert.equal(health.result.structuredContent.account.healthy, false);
  assert.equal(health.result.structuredContent.pool.cooling, 1);

  const status = await server.rpc("tools/call", { name: "allmodels_status", arguments: {} });
  assert.equal(status.result.structuredContent.pool.total, 1);

  const unknown = await server.rpc("tools/call", { name: "nope", arguments: {} });
  assert.equal(unknown.result.isError, true);
  assert.match(unknown.result.content[0].text, /unknown_tool|未知工具/);

  const removed = await server.rpc("tools/call", {
    name: "allmodels_remove",
    arguments: { email: "mcp@example.com" },
  });
  assert.equal(removed.result.structuredContent.pool.total, 0);
});
