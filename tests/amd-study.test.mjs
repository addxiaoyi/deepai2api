// AMD 自动刷课的路由层：courses 同步查询 + study 后台任务（假 worker，跑真 spawn）。
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
      "if 'courses' in args:",
      "    print(json.dumps([{'email': 'a@b.c', 'ok': True, 'courses': [{'id': 'c-1', 'title': 'ROCm 入门'}, {'id': 'c-2', 'title': 'MI300X'}]}]))",
      "    sys.exit(0)",
      "print(json.dumps({'type': 'log', 'message': 'study started'}), flush=True)",
      "print(json.dumps({'type': 'course', 'course': 'c-1', 'studyRate': 100, 'seconds': 3600}), flush=True)",
      "print(json.dumps({'type': 'done', 'email': 'a@b.c', 'courses': 1, 'gained': 10}), flush=True)",
    ].join("\n"),
  );
}

async function withServer(run) {
  const dir = mkdtempSync(join(tmpdir(), "amd-study-"));
  const tool = join(dir, "tool");
  fakeWorker(tool);
  const prev = process.env.AMD_REGISTER_DIR;
  process.env.AMD_REGISTER_DIR = tool;
  const config = loadConfig(["--port", "18787", "--token", "test-token"]);
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

async function importAccount(base) {
  return (
    await fetch(`${base}/api/admin/amd/pool/import`, {
      method: "POST",
      headers: { ...admin, ...json },
      body: JSON.stringify({ email: "a@b.c", apiKey: "SESSION=cookie" }),
    })
  ).json();
}

test("courses endpoint returns the parsed course list", async () => {
  await withServer(async ({ base }) => {
    await importAccount(base);
    const body = await (await fetch(`${base}/api/admin/amd/courses`, { headers: admin })).json();
    assert.equal(body.ok, true);
    const list = body.courses?.[0]?.courses ?? [];
    assert.deepEqual(list.map((c) => c.id), ["c-1", "c-2"]);
  });
});

test("courses requires an account in the pool", async () => {
  await withServer(async ({ base }) => {
    const res = await fetch(`${base}/api/admin/amd/courses`, { headers: admin });
    assert.equal(res.status, 400);
    const body = await res.json();
    assert.equal(body.error.code, "no_account");
  });
});

test("study starts a job that streams course events", async () => {
  await withServer(async ({ base }) => {
    await importAccount(base);
    const res = await fetch(`${base}/api/admin/amd/study`, {
      method: "POST",
      headers: { ...admin, ...json },
      body: JSON.stringify({ step: 30, target: 60, pause: 0 }),
    });
    assert.equal(res.status, 202);
    const started = await res.json();
    assert.equal(started.job.mode, "study");

    for (let i = 0; i < 100; i++) {
      const status = await (await fetch(`${base}/api/admin/amd/status`, { headers: admin })).json();
      if (!status.running) {
        assert.equal(status.job.status, "done");
        const texts = status.job.lines.map((line) => line.text).join(" ");
        assert.match(texts, /study started/, "要能看到 worker 的日志行");
        return;
      }
      await new Promise((resolve) => setTimeout(resolve, 100));
    }
    assert.fail("study job never finished");
  });
});

test("study rejects a second concurrent job", async () => {
  await withServer(async ({ base }) => {
    await importAccount(base);
    const first = await fetch(`${base}/api/admin/amd/study`, {
      method: "POST",
      headers: { ...admin, ...json },
      body: JSON.stringify({ pause: 0 }),
    });
    assert.equal(first.status, 202);
    const second = await fetch(`${base}/api/admin/amd/study`, {
      method: "POST",
      headers: { ...admin, ...json },
      body: JSON.stringify({ pause: 0 }),
    });
    assert.equal(second.status, 409, "同一时间只允许一个任务");
  });
});
