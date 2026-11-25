// WaveSpeed / Novita 在网关里的集成测试：号池隔离、装配、反代路由、Key 绑定。
//
// 不打真网络：上游用本地 http server 冒充 provider 的 apiBase / taskApiBase。
//
//   node --test tests/providers-wavespeed-novita.test.mjs
import { test } from "node:test";
import assert from "node:assert/strict";
import { createServer } from "node:http";
import { mkdtempSync, existsSync, readFileSync } from "node:fs";
import { tmpdir } from "node:os";
import { join } from "node:path";

import { PROVIDERS, getProvider, providerSummary, resolveEndpoints } from "../src/provider-catalog.mjs";
import { ProviderPool, importAccount, maskAccount, normalizeAccount } from "../src/provider-pool.mjs";
import { createProviderRuntime } from "../src/providers.mjs";

/* ------------------------------------------------------------------ helpers */

const tempData = () => mkdtempSync(join(tmpdir(), "prov-"));

/** 最小 Store 替身：只要 addKey，和 store.mjs 的签名一致。 */
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

const sample = (overrides = {}) => ({
  email: "ws@example.com",
  password: "pw-123456",
  name: "WS",
  apiKey: "ws_key_abcdef0123456789",
  keyPrefix: "ws_key_a",
  keyLast4: "6789",
  keySource: "accesskey-ui",
  engine: "browser",
  ...overrides,
});

/** 起一个假上游，记录收到的请求。 */
async function fakeUpstream(handler) {
  const calls = [];
  const server = createServer((req, res) => {
    const chunks = [];
    req.on("data", (chunk) => chunks.push(chunk));
    req.on("end", () => {
      const body = Buffer.concat(chunks).toString("utf8");
      calls.push({ method: req.method, url: req.url, body, headers: req.headers });
      const result = handler({ method: req.method, url: req.url, body, headers: req.headers });
      const payload = Buffer.from(JSON.stringify(result.body ?? {}));
      res.writeHead(result.status ?? 200, { "Content-Type": "application/json" });
      res.end(payload);
    });
  });
  await new Promise((done) => server.listen(0, "127.0.0.1", done));
  const { port } = server.address();
  return {
    base: `http://127.0.0.1:${port}`,
    calls,
    close: () => new Promise((done) => server.close(done)),
  };
}

/** 起一个只有反代层的网关，返回它的地址。 */
async function relayGateway({ dataDir, store, endpoints, providers }) {
  const runtime = createProviderRuntime({
    dataDir,
    port: 0,
    token: "test-token",
    store,
    log: () => {},
    enabled: providers,
    endpointOverrides: endpoints,
  });
  const server = createServer(async (req, res) => {
    const url = new URL(req.url ?? "/", "http://127.0.0.1");
    // 直接放行鉴权：本测试只关心反代行为
    const handled = await runtime.relay.handle(req, res, url, { auth: { ok: true, kind: "admin" } });
    if (!handled) {
      res.writeHead(404, { "Content-Type": "application/json" });
      res.end(JSON.stringify({ error: "no route" }));
    }
  });
  await new Promise((done) => server.listen(0, "127.0.0.1", done));
  const { port } = server.address();
  return {
    base: `http://127.0.0.1:${port}`,
    runtime,
    close: () => new Promise((done) => server.close(done)),
  };
}

/* -------------------------------------------------------------- catalog */

test("catalog 里有 wavespeed 与 novita，且端点各就各位", () => {
  const ids = PROVIDERS.map((p) => p.id);
  assert.ok(ids.includes("wavespeed"), `catalog 应含 wavespeed，实际 ${ids}`);
  assert.ok(ids.includes("novita"), `catalog 应含 novita，实际 ${ids}`);

  const ws = getProvider("wavespeed");
  assert.equal(ws.apiBase, "https://llm.wavespeed.ai/v1", "wavespeed 的 LLM 基址");
  assert.equal(ws.taskApiBase, "https://api.wavespeed.ai/api/v3", "wavespeed 的生成基址");
  assert.equal(ws.supportsEmailSignup, false, "wavespeed 没有邮箱注册");
  assert.equal(ws.supportsOAuthOnly, true);
  assert.equal(ws.supportsKeyCreationApi, false, "wavespeed 没有建 key 接口");

  const nv = getProvider("novita");
  assert.equal(nv.apiBase, "https://api.novita.ai/openai", "novita 的 OpenAI 兼容前缀");
  assert.equal(nv.taskApiBase, "https://api.novita.ai");
  assert.equal(nv.supportsEmailSignup, true, "novita 有邮箱注册");
  assert.equal(nv.supportsKeyCreationApi, true, "novita 面板能建 key");
  assert.equal(nv.turnstileSiteKey, "0x4AAAAAAAaG28VfN_OxkED8", "从 chunk 反查的 sitekey");

  const summary = providerSummary(ws);
  assert.equal(summary.id, "wavespeed");
  assert.ok(summary.panelBase.startsWith("https://"));
});

test("getProvider 对未知 id 抛出可识别的错误", () => {
  assert.throws(
    () => getProvider("nope"),
    (error) => error.status === 400 && error.code === "unknown_provider",
  );
});

test("resolveEndpoints 支持环境变量覆盖", () => {
  const provider = getProvider("wavespeed");
  const before = process.env.WAVESPEED_API_BASE;
  process.env.WAVESPEED_API_BASE = "http://127.0.0.1:9/override";
  try {
    assert.equal(resolveEndpoints(provider).apiBase, "http://127.0.0.1:9/override");
  } finally {
    if (before === undefined) delete process.env.WAVESPEED_API_BASE;
    else process.env.WAVESPEED_API_BASE = before;
  }
});

/* ---------------------------------------------------------------- pool */

test("号池按 provider 物理隔离：互不串号", () => {
  const dir = tempData();
  const wsPool = ProviderPool.at(dir, getProvider("wavespeed"));
  const nvPool = ProviderPool.at(dir, getProvider("novita"));

  wsPool.add(sample({ email: "a@ws.com", apiKey: "ws_key_aaaaaaaaaaaaaaaa" }));
  nvPool.add(sample({ email: "b@nv.com", apiKey: "sk_nvbbbbbbbbbbbbbbbb", keyPrefix: "sk_nvbbb" }));

  assert.equal(wsPool.size, 1, "wavespeed 号池只应有 1 个账号");
  assert.equal(nvPool.size, 1, "novita 号池只应有 1 个账号");
  assert.ok(existsSync(join(dir, "wavespeed-pool.json")), "应有 wavespeed-pool.json");
  assert.ok(existsSync(join(dir, "novita-pool.json")), "应有 novita-pool.json");

  const wsRaw = JSON.parse(readFileSync(join(dir, "wavespeed-pool.json"), "utf8"));
  assert.equal(wsRaw.schema, "dcg.pool/wavespeed@1");
  assert.equal(wsRaw.provider, "wavespeed");
  assert.equal(wsRaw.accounts.length, 1);
  assert.equal(wsRaw.accounts[0].email, "a@ws.com");

  const nvRaw = JSON.parse(readFileSync(join(dir, "novita-pool.json"), "utf8"));
  assert.equal(nvRaw.schema, "dcg.pool/novita@1");
  assert.equal(nvRaw.provider, "novita");
  assert.equal(nvRaw.accounts[0].email, "b@nv.com");

  // 交叉确认：没有任何一方把对方的账号写进自己的文件
  assert.ok(!wsRaw.accounts.some((a) => a.email === "b@nv.com"));
  assert.ok(!nvRaw.accounts.some((a) => a.email === "a@ws.com"));
});

test("importAccount 幂等地只发一把网关 Key", () => {
  const dir = tempData();
  const pool = ProviderPool.at(dir, getProvider("wavespeed"));
  const store = fakeStore();

  const first = importAccount({ pool, store, record: sample() });
  assert.equal(first.created, true);
  assert.ok(first.gatewayKey.plaintext.startsWith("sk-dcg-"), "应发 sk-dcg- Key");
  assert.equal(store.keys.length, 1);

  const second = importAccount({ pool, store, record: sample() });
  assert.equal(second.created, false, "同 apiKey 第二次应是更新");
  assert.equal(store.keys.length, 1, "不该重复发 Key");
  assert.equal(pool.size, 1);
});

test("maskAccount 不泄露面板 Key 明文", () => {
  const masked = maskAccount(normalizeAccount(sample()));
  assert.ok(!JSON.stringify(masked).includes("ws_key_abcdef0123456789"), "不能出现完整 Key");
  assert.ok(masked.apiKeyPreview.includes("…"), "应只给预览");
  assert.equal(masked.engine, "browser");
});

/* ------------------------------------------------------------- runtime */

test("runtime 只装配指定的 provider，并各自持有一份号池", () => {
  const dir = tempData();
  const store = fakeStore();
  const runtime = createProviderRuntime({
    dataDir: dir,
    port: 8787,
    token: "t",
    store,
    log: () => {},
    enabled: ["wavespeed", "novita"],
  });

  assert.deepEqual(
    runtime.providers.map((p) => p.id).sort(),
    ["novita", "wavespeed"],
    "只装配 requested 的两个",
  );
  assert.equal(runtime.pools.size, 2);
  assert.equal(runtime.registrations.size, 2);

  const list = runtime.list();
  assert.equal(list.length, 2);
  for (const row of list) {
    assert.equal(row.pool.total, 0, `${row.id} 初始号池应为空`);
    assert.equal(typeof row.toolPresent, "boolean", `${row.id} 应报告 worker 是否存在`);
  }

  // 未知 provider 应该 404，而不是静默落到别的 provider
  assert.throws(() => runtime.requireProvider("moonai"), (error) => error.status === 404);
});

test("runtime.stats 按 provider 分桶", () => {
  const dir = tempData();
  const runtime = createProviderRuntime({
    dataDir: dir,
    port: 8787,
    token: "t",
    store: fakeStore(),
    log: () => {},
    enabled: ["wavespeed", "novita"],
  });
  runtime.pools.get("wavespeed").add(sample());
  const stats = runtime.stats();
  assert.equal(stats.wavespeed.accounts, 1);
  assert.equal(stats.novita.accounts, 0);
});

/* --------------------------------------------------------------- relay */

test("反代：wavespeed LLM 用 apiBase，转发 Authorization 与 body", async () => {
  const dir = tempData();
  const store = fakeStore();
  const upstream = await fakeUpstream(({ url }) => {
    if (url === "/v1/models") {
      return { body: { data: [{ id: "deepseek/deepseek-v4-flash" }] } };
    }
    return {
      body: {
        id: "chatcmpl-1",
        object: "chat.completion",
        choices: [{ index: 0, message: { role: "assistant", content: "hi" } }],
      },
    };
  });

  const gw = await relayGateway({
    dataDir: dir,
    store,
    providers: ["wavespeed"],
    endpoints: { wavespeed: { apiBase: upstream.base } },
  });
  gw.runtime.pools.get("wavespeed").add(sample());

  try {
    const res = await fetch(`${gw.base}/p/wavespeed/v1/chat/completions`, {
      method: "POST",
      headers: { Authorization: "Bearer sk-dcg-test", "Content-Type": "application/json" },
      body: JSON.stringify({ model: "deepseek/deepseek-v4-flash", messages: [{ role: "user", content: "hi" }] }),
    });
    assert.equal(res.status, 200);
    const payload = await res.json();
    assert.equal(payload.choices[0].message.content, "hi");

    const call = upstream.calls.find((c) => c.url === "/v1/chat/completions");
    assert.ok(call, `应打到 /v1/chat/completions，实际 ${upstream.calls.map((c) => c.url)}`);
    assert.equal(call.headers.authorization, `Bearer ${sample().apiKey}`, "上游应收到面板 Key");
    assert.equal(JSON.parse(call.body).model, "deepseek/deepseek-v4-flash");
    assert.equal(res.headers.get("x-gateway-provider"), "wavespeed");
  } finally {
    await gw.close();
    await upstream.close();
  }
});

test("反代：wavespeed 异步提交走 taskApiBase，轮询走 predictions/result", async () => {
  const dir = tempData();
  const store = fakeStore();
  const upstream = await fakeUpstream(({ url, method }) => {
    if (method === "POST") {
      return {
        body: {
          code: 200,
          data: { id: "task-1", status: "created", urls: { get: "https://x/api/v3/predictions/task-1/result" } },
        },
      };
    }
    return { body: { code: 200, data: { id: "task-1", status: "completed", outputs: ["img"] } } };
  });

  const gw = await relayGateway({
    dataDir: dir,
    store,
    providers: ["wavespeed"],
    endpoints: { wavespeed: { apiBase: upstream.base, taskApiBase: upstream.base } },
  });
  gw.runtime.pools.get("wavespeed").add(sample());

  try {
    const submit = await fetch(`${gw.base}/p/wavespeed/v1/async/wavespeed-ai/z-image/turbo`, {
      method: "POST",
      headers: { Authorization: "Bearer sk-dcg-test", "Content-Type": "application/json" },
      body: JSON.stringify({ prompt: "a cat" }),
    });
    assert.equal(submit.status, 200);
    const submitted = await submit.json();
    assert.equal(submitted.data.id, "task-1");

    const submitCall = upstream.calls.find((c) => c.method === "POST");
    assert.ok(submitCall, "应有提交请求");
    assert.equal(submitCall.url, "/wavespeed-ai/z-image/turbo", "提交应打到 taskApiBase + model_id（taskApiBase 已含 /api/v3）");

    const poll = await fetch(`${gw.base}/p/wavespeed/v1/async/result?id=task-1`, {
      headers: { Authorization: "Bearer sk-dcg-test" },
    });
    assert.equal(poll.status, 200);
    const polled = await poll.json();
    assert.equal(polled.data.status, "completed");
    const pollCall = upstream.calls.find((c) => c.url.includes("predictions"));
    assert.ok(pollCall, `轮询应打到 predictions/<id>/result，实际 ${upstream.calls.map((c) => c.url)}`);
    assert.equal(pollCall.url, "/predictions/task-1/result");
  } finally {
    await gw.close();
    await upstream.close();
  }
});

test("反代：novita 异步提交加 /v3/async 前缀", async () => {
  const dir = tempData();
  const store = fakeStore();
  const upstream = await fakeUpstream(() => ({ body: { task_id: "nv-1" } }));

  const gw = await relayGateway({
    dataDir: dir,
    store,
    providers: ["novita"],
    endpoints: { novita: { apiBase: upstream.base, taskApiBase: upstream.base } },
  });
  gw.runtime.pools.get("novita").add(
    sample({ email: "nv@example.com", apiKey: "sk_" + "n".repeat(32), keyPrefix: "sk_nnnnn" }),
  );

  try {
    const res = await fetch(`${gw.base}/p/novita/v1/async/txt2img`, {
      method: "POST",
      headers: { Authorization: "Bearer sk-dcg-test", "Content-Type": "application/json" },
      body: JSON.stringify({ model_name: "sd_xl_base_1.0.safetensors", request: { prompt: "x" } }),
    });
    assert.equal(res.status, 200);
    const call = upstream.calls[0];
    assert.equal(call.url, "/v3/async/txt2img", `novita 提交路径应带 /v3/async，实际 ${call.url}`);
  } finally {
    await gw.close();
    await upstream.close();
  }
});

test("反代：401 的账号会被冷却，并换下一个账号", async () => {
  const dir = tempData();
  const store = fakeStore();
  const upstream = await fakeUpstream(({ headers }) => {
    // 第一把 key 401，第二把 200
    if (headers.authorization === "Bearer badkey0000000000000") {
      return { status: 401, body: { error: "invalid key" } };
    }
    return { body: { data: [{ id: "m" }] } };
  });

  const gw = await relayGateway({
    dataDir: dir,
    store,
    providers: ["wavespeed"],
    endpoints: { wavespeed: { apiBase: upstream.base } },
  });
  const pool = gw.runtime.pools.get("wavespeed");
  pool.add(sample({ email: "bad@x.com", apiKey: "badkey0000000000000" }));
  pool.add(sample({ email: "good@x.com", apiKey: "goodkey000000000000" }));

  try {
    const res = await fetch(`${gw.base}/p/wavespeed/v1/models`, {
      headers: { Authorization: "Bearer sk-dcg-test" },
    });
    assert.equal(res.status, 200, "应该换号成功");
    const payload = await res.json();
    assert.equal(payload.data[0].id, "m");

    const bad = pool.byEmail("bad@x.com");
    assert.equal(bad.healthy, false, "401 的账号应被标记不健康");
    assert.ok(bad.cooldownUntil > Date.now(), "401 的账号应进入冷却");
  } finally {
    await gw.close();
    await upstream.close();
  }
});

test("反代：缺 Key 返回 401，未知 provider 返回 404", async () => {
  const dir = tempData();
  const gw = await relayGateway({
    dataDir: dir,
    store: fakeStore(),
    providers: ["wavespeed"],
    endpoints: {},
  });
  try {
    const noKey = await fetch(`${gw.base}/p/wavespeed/v1/models`);
    assert.equal(noKey.status, 401, "没有 Key 应 401");

    const unknown = await fetch(`${gw.base}/p/nope/v1/models`, {
      headers: { Authorization: "Bearer sk-dcg-test" },
    });
    assert.equal(unknown.status, 404, "未知 provider 应 404");
  } finally {
    await gw.close();
  }
});

test("反代：号池为空时返回 503 no_account", async () => {
  const dir = tempData();
  const upstream = await fakeUpstream(() => ({ body: {} }));
  const gw = await relayGateway({
    dataDir: dir,
    store: fakeStore(),
    providers: ["novita"],
    endpoints: { novita: { apiBase: upstream.base } },
  });
  try {
    const res = await fetch(`${gw.base}/p/novita/v1/chat/completions`, {
      method: "POST",
      headers: { Authorization: "Bearer sk-dcg-test", "Content-Type": "application/json" },
      body: JSON.stringify({ model: "x", messages: [] }),
    });
    assert.equal(res.status, 503);
    const payload = await res.json();
    assert.equal(payload.error.code, "no_account");
  } finally {
    await gw.close();
    await upstream.close();
  }
});
