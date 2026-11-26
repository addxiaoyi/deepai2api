// 算力券 / 免费模型 / 累加到主号：路由层（假 worker，真 spawn）。
import { test } from "node:test";
import assert from "node:assert/strict";
import { mkdirSync, mkdtempSync, writeFileSync } from "node:fs";
import { tmpdir } from "node:os";
import { join } from "node:path";

import { loadConfig } from "../src/config.mjs";
import { Store } from "../src/store.mjs";
import { createGateway } from "../src/server.mjs";

const admin = { Authorization: "Bearer test-token" };
const json = { "Content-Type": "application/json" };

function fakeWorker(tool) {
  mkdirSync(join(tool, "amd_reg"), { recursive: true });
  writeFileSync(join(tool, "amd_reg", "__init__.py"), "");
  writeFileSync(join(tool, "amd_reg", "cli.py"), "");
  writeFileSync(
    join(tool, "amd_reg", "__main__.py"),
    [
      "import sys, json",
      "args = sys.argv[1:]",
      "print(json.dumps({'type': 'log', 'message': 'args: ' + ' '.join(args[2:])}), flush=True)",
      "print(json.dumps({'type': 'done', 'summary': {'hours': 40, 'vouchers': 3}}), flush=True)",
    ].join("\n"),
  );
}

async function withServer(run) {
  const dir = mkdtempSync(join(tmpdir(), "amd-vouchers-"));
  const tool = join(dir, "tool");
  fakeWorker(tool);
  const prev = process.env.AMD_REGISTER_DIR;
  process.env.AMD_REGISTER_DIR = tool;
  const config = loadConfig(["--port", "18785", "--token", "test-token"]);
  config.dataDir = dir;
  const store = Store.at(dir);
  const lane = { base: "http://127.0.0.1:1", instance: "test", getCatalog: async () => [], getAllowance: async () => null, checkInFor: () => null };
  const scheduler = { candidates: () => [], record: () => {}, pin: () => {}, status: () => ({}) };
  const gateway = createGateway({ config, lane, store, scheduler, log: () => {} });
  await new Promise((resolve) => gateway.server.listen(0, "127.0.0.1", resolve));
  const base = `http://127.0.0.1:${gateway.server.address().port}`;
  try {
    await run({ base });
  } finally {
    await new Promise((resolve) => gateway.server.close(resolve));
    store.flush?.();
    if (prev === undefined) delete process.env.AMD_REGISTER_DIR;
    else process.env.AMD_REGISTER_DIR = prev;
  }
}

async function runJob(base, route, body) {
  const res = await fetch(`${base}/api/admin/amd/${route}`, {
    method: "POST",
    headers: { ...admin, ...json },
    body: JSON.stringify(body),
  });
  if (res.status !== 202) return { status: res.status, body: await res.json() };
  for (let i = 0; i < 100; i++) {
    const status = await (await fetch(`${base}/api/admin/amd/status`, { headers: admin })).json();
    if (!status.running) return { status: 202, job: status.job };
    await new Promise((resolve) => setTimeout(resolve, 100));
  }
  throw new Error("job never finished");
}

test("vouchers job runs and reports hours", async () => {
  await withServer(async ({ base }) => {
    const out = await runJob(base, "vouchers", {});
    assert.equal(out.status, 202);
    assert.equal(out.job.mode, "vouchers");
    assert.match(out.job.lines.map((l) => l.text).join(" "), /vouchers/);
  });
});

test("consolidate passes master/redeem/amount through", async () => {
  await withServer(async ({ base }) => {
    const out = await runJob(base, "consolidate", { master: "a@example.com", redeem: true, amount: 2 });
    const text = out.job.lines.map((line) => line.text).join(" ");
    assert.match(text, /--master a@example\.com/);
    assert.match(text, /--redeem/);
    assert.match(text, /--amount 2/);
  });
});

test("exchange supports dry-run", async () => {
  await withServer(async ({ base }) => {
    const out = await runJob(base, "exchange", { dryRun: true, amount: 1 });
    assert.match(out.job.lines.map((l) => l.text).join(" "), /--dry-run/);
  });
});

test("models job proxies the subcommand", async () => {
  await withServer(async ({ base }) => {
    const out = await runJob(base, "models", {});
    assert.equal(out.job.mode, "models");
  });
});

test("each route rejects GET", async () => {
  await withServer(async ({ base }) => {
    for (const route of ["vouchers", "exchange", "models", "consolidate"]) {
      const res = await fetch(`${base}/api/admin/amd/${route}`, { headers: admin });
      assert.equal(res.status, 404, `${route} 只接受 POST`);
    }
  });
});
