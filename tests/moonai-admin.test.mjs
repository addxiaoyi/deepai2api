// Provider 管理面冒烟：真实网关进程内启动，验证路由挂载、鉴权、入池与隔离。
//
// 关注点：新增的 `/api/admin/providers/*` 与既有 `/api/admin/*`、`/api/poe/*`
// 不冲突；`/p/<provider>/v1/*` 挂在鉴权之外但自带 Key 校验。
import assert from "node:assert/strict";
import { mkdtempSync, rmSync } from "node:fs";
import { tmpdir } from "node:os";
import { join } from "node:path";

import { loadConfig } from "../src/config.mjs";
import { makeMachineHeader } from "../src/machine.mjs";
import { makeDispatcher } from "../src/dispatcher.mjs";
import { makeLane } from "../src/lane.mjs";
import { Store } from "../src/store.mjs";
import { createGateway } from "../src/server.mjs";
import { makeScheduler } from "../src/scheduler.mjs";
import { startMockUpstream } from "./mock-upstream.mjs";

const adminToken = "provider-admin-token";
const dataDir = mkdtempSync(join(tmpdir(), "moonai-admin-"));
const mock = await startMockUpstream();

const config = {
  ...loadConfig([]),
  port: 0,
  host: "127.0.0.1",
  token: adminToken,
  dataDir,
  apiOrigin: mock.origin,
  signInOrigin: mock.origin,
  log: "warn",
};
const store = Store.at(dataDir);
const { dispatcher } = makeDispatcher();
const lane = makeLane({
  config,
  getAccount: () => store.account,
  machineHeader: makeMachineHeader("11111111-2222-3333-4444-555555555555"),
  dispatcher,
});
const scheduler = makeScheduler({ store });
const gateway = createGateway({ config, lane, store, scheduler });
await new Promise((resolve) => gateway.server.listen(0, "127.0.0.1", resolve));
const base = `http://127.0.0.1:${gateway.server.address().port}`;

const call = async (path, { method = "GET", body, token, key } = {}) => {
  const headers = { "content-type": "application/json" };
  if (token) headers.authorization = `Bearer ${token}`;
  if (key) headers.authorization = `Bearer ${key}`;
  const res = await fetch(`${base}${path}`, { method, headers, body: body ? JSON.stringify(body) : undefined });
  const text = await res.text();
  let json = null;
  try {
    json = JSON.parse(text);
  } catch {
    json = null;
  }
  return { status: res.status, text, json };
};

let passed = 0;
const check = (name, fn) => {
  try {
    fn();
    passed++;
    console.log(`  ok   ${name}`);
  } catch (error) {
    console.error(`  FAIL ${name}\n       ${error.message}`);
    throw error;
  }
};

console.log("MoonAI provider admin");
try {
  {
    const res = await call("/api/admin/providers");
    check("匿名访问 provider 列表被拒（401）", () => assert.equal(res.status, 401));
  }

  {
    const res = await call("/api/admin/providers", { token: adminToken });
    check("管理口令可以看到 provider 列表，moonai 在其中", () => {
      assert.equal(res.status, 200);
      const ids = res.json.providers.map((p) => p.id);
      // 目录会持续增长（zo / blackbox …），这里只要求早期三条都在，
      // 避免每加一个 provider 就把这条断言改一次。
      for (const id of ["moonai", "novita", "wavespeed"]) {
        assert.ok(ids.includes(id), `${id} 不在 provider 列表里：${ids.join(",")}`);
      }
      assert.equal(new Set(ids).size, ids.length, "provider id 必须唯一");
      const moonai = res.json.providers.find((p) => p.id === "moonai");
      assert.equal(moonai.relay.kind, "moonai-llm-proxy");
      assert.equal(moonai.models.length, 6);
      assert.equal(moonai.pool.schema, "dcg.pool/moonai@1");
      assert.equal(moonai.toolPresent, true);
    });
  }

  {
    const res = await call("/api/admin/providers/moonai", { token: adminToken });
    check("provider 详情带端点、python、job 与号池统计", () => {
      assert.equal(res.status, 200);
      assert.equal(res.json.id, "moonai");
      assert.ok(res.json.endpoints.apiBase.startsWith("https://core.mooonai.com"));
      assert.equal(typeof res.json.python, "string");
      assert.equal(res.json.pool.total, 0);
      assert.equal(res.json.toolDir.endsWith("moonai-reg") || res.json.toolDir.includes("moonai-reg"), true);
    });
  }

  {
    const res = await call("/api/admin/providers/moonai/pool/import", {
      method: "POST",
      token: adminToken,
      body: {
        account: {
          email: "import@example.com",
          password: "pw",
          apiKey: "api_IMPORTED",
          accessToken: "jwt-imported",
          refreshToken: "rf-imported",
          organizationId: "5100",
        },
      },
    });
    check("导入账号：入池 + 发网关 Key（明文只回一次）", () => {
      assert.equal(res.status, 200);
      assert.equal(res.json.created, true);
      assert.match(res.json.gatewayKey.plaintext, /^sk-dcg-/);
      assert.equal(res.json.account.email, "import@example.com");
      assert.equal(res.json.pool.total, 1);
      assert.equal(res.json.pool.keys, 1);
    });
  }

  {
    const res = await call("/api/admin/providers/moonai/pool", { token: adminToken });
    check("号池视图脱敏：没有令牌明文", () => {
      assert.equal(res.status, 200);
      assert.equal(res.json.accounts.length, 1);
      assert.equal(res.text.includes("jwt-imported"), false);
      assert.equal(res.text.includes("rf-imported"), false);
      assert.equal(res.json.accounts[0].hasRefreshToken, true);
      assert.equal(res.json.accounts[0].apiKeyPreview.toLowerCase().startsWith("api_impo"), true);
    });
  }

  {
    const res = await call("/api/admin/providers/moonai/pool/import", {
      method: "POST",
      token: adminToken,
      body: { account: { email: "import@example.com", apiKey: "api_IMPORTED" } },
    });
    check("重复导入幂等：不重复发 Key、不清掉令牌", () => {
      assert.equal(res.status, 200);
      assert.equal(res.json.created, false);
      assert.equal(res.json.pool.total, 1);
    });
  }

  {
    const res = await call("/api/admin/providers/moonai/keys", { token: adminToken });
    check("Key 视图给前缀而不是明文", () => {
      assert.equal(res.status, 200);
      assert.equal(res.json.keys.length, 1);
      assert.equal(res.json.keys[0].email, "import@example.com");
      assert.equal(res.text.includes("jwt-imported"), false);
    });
  }

  {
    const res = await call("/api/admin/providers/nope/pool", { token: adminToken });
    check("未知 provider 返回 404 unknown_provider", () => {
      assert.equal(res.status, 404);
      assert.match(res.text, /unknown_provider/);
    });
  }

  {
    const res = await call("/health");
    check("/health 里多了 providers 段，原有字段还在", () => {
      assert.equal(res.status, 200);
      assert.ok(res.json.providers.moonai);
      assert.equal(res.json.providers.moonai.accounts, 1);
      assert.ok(res.json.allmodels);
      assert.ok("accounts" in res.json);
    });
  }

  {
    const res = await call("/p/moonai/v1/models", { key: "" });
    check("/p/moonai/v1/models 无 Key → 401", () => assert.equal(res.status, 401));
  }

  {
    const res = await call("/api/admin/allmodels/status", { token: adminToken });
    check("既有 allmodels 路由不受影响", () => assert.equal(res.status, 200));
  }

  console.log(`\n${passed} 项通过`);
} finally {
  gateway.server.close();
  await mock.close?.();
  store.flush?.();
  rmSync(dataDir, { recursive: true, force: true });
}
