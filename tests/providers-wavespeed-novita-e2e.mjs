// 端到端（离线）：mock 上游 → 网关号池 → 反代出 Key。
//
// 验证的是用户真正关心的那条链：注册进来的账号能不能立刻变成可用的 OpenAI 兼容接口。
//
//   node tests/providers-wavespeed-novita-e2e.mjs
import assert from "node:assert/strict";
import { createServer } from "node:http";
import { mkdtempSync, readFileSync, existsSync } from "node:fs";
import { tmpdir } from "node:os";
import { join } from "node:path";

import { startMockProvider } from "./mock-provider.mjs";
import { createProviderRuntime } from "../src/providers.mjs";
import { createProviderAdmin } from "../src/provider-admin.mjs";

const step = (name) => process.stdout.write(`\n▸ ${name}\n`);
const ok = (msg) => process.stdout.write(`  ✓ ${msg}\n`);

function fakeStore() {
  const keys = [];
  return {
    keys,
    addKey({ label, hash, prefix, limits }) {
      const key = { id: `k-${keys.length + 1}`, label, hash, prefix, limits, createdAt: new Date().toISOString() };
      keys.push(key);
      return key;
    },
  };
}

async function main() {
  const mock = await startMockProvider();
  const dataDir = mkdtempSync(join(tmpdir(), "provider-e2e-"));
  const store = fakeStore();

  const runtime = createProviderRuntime({
    dataDir,
    port: 0,
    token: "e2e-token",
    store,
    log: () => {},
    enabled: ["wavespeed", "novita"],
    endpointOverrides: {
      wavespeed: { panelBase: mock.panelBase, apiBase: mock.apiBase, taskApiBase: mock.taskBase },
      novita: { panelBase: mock.panelBase, apiBase: mock.apiBase, taskApiBase: mock.taskBase },
    },
  });

  // 一个只挂 provider 反代 + admin 的小网关
  const admin = createProviderAdmin({ runtime, store, log: () => {} });
  const gateway = createServer(async (req, res) => {
    const url = new URL(req.url ?? "/", "http://127.0.0.1");
    const path = url.pathname.replace(/\/+$/, "") || "/";
    if (path.startsWith("/api/admin/providers")) {
      const handled = await admin.handle(req, res, path);
      if (handled) return;
    }
    if (path.startsWith("/p/")) {
      const handled = await runtime.relay.handle(req, res, url, { auth: { ok: true, kind: "admin" } });
      if (handled) return;
    }
    res.writeHead(404, { "Content-Type": "application/json" });
    res.end(JSON.stringify({ error: "no route" }));
  });
  await new Promise((done) => gateway.listen(0, "127.0.0.1", done));
  const gwBase = `http://127.0.0.1:${gateway.address().port}`;

  try {
    /* ---------------------------------------------------------- 1. 入池 */
    step("注册结果入池（模拟 worker 的 push）");
    const novitaKey = mock.seedAccount({ email: "nv-e2e@example.com", provider: "novita" });
    const importRes = await fetch(`${gwBase}/api/admin/providers/novita/pool/import`, {
      method: "POST",
      headers: { Authorization: "Bearer e2e-token", "Content-Type": "application/json" },
      body: JSON.stringify({
        email: "nv-e2e@example.com",
        password: "pw",
        apiKey: novitaKey,
        keySource: "panel-api",
        engine: "protocol",
        emailVerified: true,
      }),
    });
    assert.ok(importRes.status >= 200 && importRes.status < 300, `入池应 2xx，实际 ${importRes.status}`);
    const imported = await importRes.json();
    assert.ok(imported.gatewayKey?.prefix, "应发一把网关 Key");
    ok(`入池成功，网关 Key 前缀 ${imported.gatewayKey.prefix}`);
    assert.ok(existsSync(join(dataDir, "novita-pool.json")), "应落盘 novita-pool.json");

    /* --------------------------------------------------- 2. 号池联动 */
    step("号池联动：wavespeed 与 novita 各管各的");
    const wsKey = mock.seedAccount({ email: "ws-e2e@example.com", provider: "wavespeed" });
    await fetch(`${gwBase}/api/admin/providers/wavespeed/pool/import`, {
      method: "POST",
      headers: { Authorization: "Bearer e2e-token", "Content-Type": "application/json" },
      body: JSON.stringify({ email: "ws-e2e@example.com", apiKey: wsKey, engine: "browser", emailVerified: true }),
    });
    const nvPool = JSON.parse(readFileSync(join(dataDir, "novita-pool.json"), "utf8"));
    const wsPool = JSON.parse(readFileSync(join(dataDir, "wavespeed-pool.json"), "utf8"));
    assert.equal(nvPool.accounts.length, 1);
    assert.equal(wsPool.accounts.length, 1);
    assert.equal(nvPool.accounts[0].email, "nv-e2e@example.com");
    assert.equal(wsPool.accounts[0].email, "ws-e2e@example.com");
    ok("两份号池互不串号");

    /* -------------------------------------------- 3. 反代：模型列表 */
    step("反代：/v1/models 用面板 Key 打上游");
    const modelsRes = await fetch(`${gwBase}/p/novita/v1/models`, {
      headers: { Authorization: "Bearer sk-dcg-e2e" },
    });
    assert.equal(modelsRes.status, 200);
    const models = await modelsRes.json();
    assert.ok(models.data.length >= 1, "应返回模型列表");
    assert.ok(
      models.data.some((m) => m.id.includes("deepseek")),
      `应有 deepseek 模型，实际 ${models.data.map((m) => m.id)}`,
    );
    ok(`拿到 ${models.data.length} 个模型`);

    /* ---------------------------------------- 4. 反代：对话出内容 */
    step("反代：/v1/chat/completions");
    const chatRes = await fetch(`${gwBase}/p/novita/v1/chat/completions`, {
      method: "POST",
      headers: { Authorization: "Bearer sk-dcg-e2e", "Content-Type": "application/json" },
      body: JSON.stringify({
        model: "deepseek/deepseek-v4.1-flash",
        messages: [{ role: "user", content: "ping" }],
      }),
    });
    assert.equal(chatRes.status, 200);
    const chat = await chatRes.json();
    assert.equal(chat.choices[0].message.content, "echo:ping", "应回显 mock 的答复");
    assert.equal(chatRes.headers.get("x-gateway-provider"), "novita");
    ok("对话返回正常，且带 X-Gateway-Provider 头");

    /* -------------------------------------- 5. 反代：异步生成任务 */
    step("反代：wavespeed 异步生成（提交 + 轮询）");
    const submitRes = await fetch(`${gwBase}/p/wavespeed/v1/async/wavespeed-ai/z-image/turbo`, {
      method: "POST",
      headers: { Authorization: "Bearer sk-dcg-e2e", "Content-Type": "application/json" },
      body: JSON.stringify({ prompt: "a cat" }),
    });
    assert.equal(submitRes.status, 200);
    const submitted = await submitRes.json();
    const taskId = submitted.data?.id;
    assert.ok(taskId, `应拿到 task id，实际 ${JSON.stringify(submitted)}`);
    ok(`任务已提交 ${taskId}`);

    let status = "";
    for (let i = 0; i < 5 && status !== "completed"; i++) {
      const pollRes = await fetch(`${gwBase}/p/wavespeed/v1/async/result?id=${taskId}`, {
        headers: { Authorization: "Bearer sk-dcg-e2e" },
      });
      assert.equal(pollRes.status, 200);
      const polled = await pollRes.json();
      status = polled.data?.status ?? "";
    }
    assert.equal(status, "completed", `轮询应到 completed，实际 ${status}`);
    ok("轮询到 completed");

    /* ------------------------------------ 6. 换号：坏 Key 自动切换 */
    step("换号：一把坏 Key 不影响整体可用");
    // 注意：**不要**往 mock 里 seed 这把 key —— 我们要的正是"mock 不认它"的 401。
    await fetch(`${gwBase}/api/admin/providers/novita/pool/import`, {
      method: "POST",
      headers: { Authorization: "Bearer e2e-token", "Content-Type": "application/json" },
      body: JSON.stringify({ email: "bad-e2e@example.com", apiKey: "sk_broken_broken_broken" }),
    });
    let success = 0;
    for (let i = 0; i < 4; i++) {
      const res = await fetch(`${gwBase}/p/novita/v1/chat/completions`, {
        method: "POST",
        headers: { Authorization: "Bearer sk-dcg-e2e", "Content-Type": "application/json" },
        body: JSON.stringify({ model: "m", messages: [{ role: "user", content: "x" }] }),
      });
      if (res.status === 200) success++;
    }
    assert.equal(success, 4, `4 次请求都应成功（坏 Key 被跳过），实际 ${success}`);
    const bad = runtime.pools.get("novita").byEmail("bad-e2e@example.com");
    assert.equal(bad.healthy, false, "坏 Key 账号应被标记不健康");
    ok("坏 Key 被冷却并跳过");

    /* ------------------------------- 7. 管理接口：导出与状态 */
    step("管理接口：状态 / 导出");
    const statusRes = await fetch(`${gwBase}/api/admin/providers`, {
      headers: { Authorization: "Bearer e2e-token" },
    });
    assert.equal(statusRes.status, 200);
    const overview = await statusRes.json();
    assert.ok(overview.providers.some((p) => p.id === "novita"));

    const exportRes = await fetch(`${gwBase}/api/admin/providers/novita/pool/export?format=jsonl`, {
      headers: { Authorization: "Bearer e2e-token" },
    });
    assert.ok(exportRes.status < 400, `导出应成功，实际 ${exportRes.status}`);
    ok("状态与导出可用");

    process.stdout.write("\n全部端到端检查通过。\n");
    return 0;
  } finally {
    await new Promise((done) => gateway.close(done));
    await mock.close();
  }
}

try {
  process.exitCode = await main();
} catch (error) {
  process.stderr.write(`\n✗ e2e 失败：${error?.stack ?? error}\n`);
  process.exitCode = 1;
}
