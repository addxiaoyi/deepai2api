// AMD 考试后台任务：假 worker 验证 spawn/事件流/参数拼装。
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
      "print(json.dumps({'type': 'log', 'message': 'exam args: ' + ' '.join(args[2:])}), flush=True)",
      "print(json.dumps({'type': 'exam', 'email': 'a@b.c', 'child': 'ch-1', 'questions': 2, 'passed': True}), flush=True)",
    ].join("\n"),
  );
}

async function withServer(run) {
  const dir = mkdtempSync(join(tmpdir(), "amd-exam-"));
  const tool = join(dir, "tool");
  fakeWorker(tool);
  const prev = process.env.AMD_REGISTER_DIR;
  process.env.AMD_REGISTER_DIR = tool;
  const config = loadConfig(["--port", "18786", "--token", "test-token"]);
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

async function seed(base) {
  await fetch(`${base}/api/admin/amd/pool/import`, {
    method: "POST",
    headers: { ...admin, ...json },
    body: JSON.stringify({ email: "a@b.c", apiKey: "SESSION=cookie" }),
  });
}

test("exam requires a course id", async () => {
  await withServer(async ({ base }) => {
    await seed(base);
    const res = await fetch(`${base}/api/admin/amd/exam`, {
      method: "POST",
      headers: { ...admin, ...json },
      body: JSON.stringify({}),
    });
    assert.equal(res.status, 400);
  });
});

test("exam requires an account", async () => {
  await withServer(async ({ base }) => {
    const res = await fetch(`${base}/api/admin/amd/exam`, {
      method: "POST",
      headers: { ...admin, ...json },
      body: JSON.stringify({ course: "c-1" }),
    });
    assert.equal(res.status, 400);
    assert.equal((await res.json()).error.code, "no_account");
  });
});

test("exam starts a job with --auto and the right flags", async () => {
  await withServer(async ({ base }) => {
    await seed(base);
    const res = await fetch(`${base}/api/admin/amd/exam`, {
      method: "POST",
      headers: { ...admin, ...json },
      body: JSON.stringify({ course: "c-1", child: "ch-9", strategy: "all", rounds: 2 }),
    });
    assert.equal(res.status, 202);
    const started = await res.json();
    assert.equal(started.job.mode, "exam");

    for (let i = 0; i < 100; i++) {
      const status = await (await fetch(`${base}/api/admin/amd/status`, { headers: admin })).json();
      if (!status.running) {
        const text = status.job.lines.map((line) => line.text).join(" ");
        assert.match(text, /--course c-1/, "要带课程 id");
        assert.match(text, /--child ch-9/, "要带章节 id");
        assert.match(text, /--auto/, "默认自动答题");
        assert.match(text, /--strategy all/, "策略要透传");
        assert.match(text, /--rounds 2/, "轮数要透传");
        assert.match(text, /--cookie SESSION=cookie/, "把池里的 cookie 传给 worker");
        return;
      }
      await new Promise((resolve) => setTimeout(resolve, 100));
    }
    assert.fail("exam job never finished");
  });
});
