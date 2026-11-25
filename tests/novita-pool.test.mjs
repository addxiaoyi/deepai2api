// Novita 与本项目的整合用例（离线，不起网关、不联网）。
//
// 目标是证明「融入而不冲突」，所以断言的都是**边界**：
//   1) 号池文件按 provider 物理隔离：写 novita 不会碰 allmodels / wavespeed 的池子；
//   2) 同一份 dataDir 下多 provider 并存，schema 各自独立；
//   3) 入池幂等：重推同一账号不会重复发网关 Key，标签带 provider 前缀；
//   4) 反代层路径拼装正确：catalog 的 apiBase 带 `/openai`，不能再叠加一层；
//   5) worker CLI 契约与 src/provider-register.mjs 的传参一致（静态比对，防回归）；
//   6) 管理接口路由前缀与既有 /api/admin/allmodels/* 不重叠。
import assert from "node:assert/strict";
import { mkdtempSync, readFileSync, readdirSync, existsSync } from "node:fs";
import { tmpdir } from "node:os";
import { fileURLToPath } from "node:url";
import { join } from "node:path";
import test from "node:test";

import { PROVIDERS, getProvider } from "../src/provider-catalog.mjs";
import { ProviderPool, importAccount, maskAccount } from "../src/provider-pool.mjs";
import { createProviderRuntime } from "../src/providers.mjs";

// 中文路径必须走 fileURLToPath，URL.pathname 会百分号编码「项目」
const repoRoot = fileURLToPath(new URL("..", import.meta.url));

function tempDataDir() {
  return mkdtempSync(join(tmpdir(), "novita-pool-"));
}

/** 最小 store：只实现 provider-pool 用到的 addKey。 */
function fakeStore() {
  const keys = [];
  return {
    keys,
    addKey({ label, hash, prefix, limits }) {
      const key = { id: `k${keys.length + 1}`, label, hash, prefix, limits, createdAt: new Date().toISOString() };
      keys.push(key);
      return key;
    },
  };
}

const account = (email, apiKey) => ({ email, apiKey, password: "p", engine: "protocol" });

test("号池按 provider 物理隔离，互不覆盖", () => {
  const dir = tempDataDir();
  const novita = ProviderPool.at(dir, getProvider("novita"));
  const wavespeed = ProviderPool.at(dir, getProvider("wavespeed"));
  const store = fakeStore();

  importAccount({ pool: novita, store, record: account("ada@example.com", "sk_novita_aaaaaaaaaaaaaaaaaaaa") });
  importAccount({ pool: wavespeed, store, record: account("bob@example.com", "sk_wave_bbbbbbbbbbbbbbbbbbbbbb") });

  assert.equal(novita.size, 1);
  assert.equal(wavespeed.size, 1);
  assert.ok(existsSync(join(dir, "novita-pool.json")));
  assert.ok(existsSync(join(dir, "wavespeed-pool.json")));

  const novitaFile = JSON.parse(readFileSync(join(dir, "novita-pool.json"), "utf8"));
  const waveFile = JSON.parse(readFileSync(join(dir, "wavespeed-pool.json"), "utf8"));
  assert.equal(novitaFile.schema, "dcg.pool/novita@1");
  assert.equal(waveFile.schema, "dcg.pool/wavespeed@1");
  assert.equal(novitaFile.provider, "novita");
  // 交叉污染会让两边的调度器互相偷号：这里必须一条都不串
  assert.ok(!JSON.stringify(novitaFile).includes("bob@example.com"));
  assert.ok(!JSON.stringify(waveFile).includes("ada@example.com"));

  // 重新打开也不该读到对方的条目
  const reopened = ProviderPool.at(dir, getProvider("novita"));
  assert.equal(reopened.size, 1);
  assert.equal(reopened.accounts[0].email, "ada@example.com");
});

test("入池幂等：重推不重复发 Key，且标签带 provider 前缀", () => {
  const dir = tempDataDir();
  const pool = ProviderPool.at(dir, getProvider("novita"));
  const store = fakeStore();
  const row = account("ada@example.com", "sk_novita_aaaaaaaaaaaaaaaaaaaa");

  const first = importAccount({ pool, store, record: row });
  const second = importAccount({ pool, store, record: { ...row, password: "changed" } });

  assert.equal(first.created, true);
  assert.equal(second.created, false);
  assert.equal(store.keys.length, 1, "同一账号只能铸一把网关 Key");
  assert.equal(first.gatewayKey.prefix, second.gatewayKey.prefix);
  assert.match(store.keys[0].label, /^novita:/);
  assert.equal(pool.size, 1);
});

test("脱敏视图不泄露面板 Key 明文", () => {
  const dir = tempDataDir();
  const pool = ProviderPool.at(dir, getProvider("novita"));
  const store = fakeStore();
  const { account: stored } = importAccount({
    pool,
    store,
    record: account("ada@example.com", "sk_novita_0123456789abcdefghij"),
  });
  const masked = maskAccount(stored);
  assert.ok(!JSON.stringify(masked).includes("sk_novita_0123456789abcdefghij"));
  assert.match(masked.apiKeyPreview, /^sk_novit…/);
});

test("运行时只装配被启用的 provider，其它线不受影响", () => {
  const dir = tempDataDir();
  const runtime = createProviderRuntime({
    dataDir: dir,
    port: 0,
    token: "t",
    store: fakeStore(),
    enabled: ["novita"],
  });
  assert.deepEqual([...runtime.pools.keys()], ["novita"]);
  const listed = runtime.list();
  assert.equal(listed.length, 1);
  assert.equal(listed[0].id, "novita");
  assert.equal(listed[0].pool.total, 0);
  // 未启用的 provider 必须明确报错，而不是悄悄落到别的池子
  assert.throws(() => runtime.requireProvider("allmodels"), /未知 provider/);
});

test("反代路径：catalog 的 apiBase 已含 /openai，不能再叠一层", () => {
  const novita = getProvider("novita");
  assert.equal(novita.apiBase, "https://api.novita.ai/openai");
  const url = `${novita.apiBase.replace(/\/+$/, "")}${novita.modelsPath || "/v1/models"}`;
  assert.equal(url, "https://api.novita.ai/openai/v1/models");
  assert.ok(!url.includes("/openai/openai"));
  // 控制台接口基址与推理基址必须是两个 host，别混用
  assert.equal(novita.panelApiBase, "https://api-server.novita.ai");
  assert.equal(novita.panelApi.keyCreate.path, "/v2/user/key");
  assert.equal(novita.turnstileSiteKey, "0x4AAAAAAAaG28VfN_OxkED8");
});

test("worker CLI 契约与 provider-register 的传参一致", () => {
  const cli = readFileSync(join(repoRoot, "tools", "novita-reg", "novita_reg", "cli.py"), "utf8");
  const register = readFileSync(join(repoRoot, "src", "provider-register.mjs"), "utf8");

  // provider-register 会传这些开关；worker 少一个就会 argparse 退出（exit 2）
  const passed = [...register.matchAll(/args\.push\(\s*"(--[a-z-]+)"/g)].map((m) => m[1]);
  const inArray = [...register.matchAll(/\n\s+"(--[a-z-]+)",/g)].map((m) => m[1]);
  const expected = new Set([...passed, ...inArray].filter((flag) => flag.startsWith("--")));

  const missing = [...expected].filter((flag) => flag !== "--json" && !cli.includes(`"${flag}"`));
  assert.deepEqual(missing, [], `worker CLI 缺少网关会传的参数：${missing.join(", ")}`);

  // 事件名必须被 provider-register 的 switch 认识
  const runner = readFileSync(join(repoRoot, "tools", "novita-reg", "novita_reg", "runner.py"), "utf8");
  const emitted = new Set([...runner.matchAll(/"type":\s*"([a-z_]+)"/g)].map((m) => m[1]));
  const handled = new Set([...register.matchAll(/case "([a-z_]+)":/g)].map((m) => m[1]));
  const unknown = [...emitted].filter((type) => !handled.has(type));
  assert.deepEqual(unknown, [], `provider-register 不认识这些事件：${unknown.join(", ")}`);
});

test("推送路由与网关管理接口前缀对齐，且不与 allmodels 冲突", () => {
  const push = readFileSync(join(repoRoot, "tools", "novita-reg", "novita_reg", "push.py"), "utf8");
  assert.match(push, /\/api\/admin\/providers\/novita\/pool\/import/);

  const admin = readFileSync(join(repoRoot, "src", "provider-admin.mjs"), "utf8");
  assert.match(admin, /\/api\/admin\/providers/);
  // 注释里提到 allmodels 是说明"并列"，只要不定义它的路由就没问题
  assert.ok(!/path\s*===\s*"\/api\/admin\/allmodels/.test(admin), "provider 管理接口不应接管 allmodels 路由");

  // 号池文件名由 provider.id 派生 → 新增 provider 不会撞名
  const poolFiles = PROVIDERS.map((p) => p.poolFile);
  assert.equal(new Set(poolFiles).size, poolFiles.length, "provider 号池文件名必须唯一");
  assert.ok(poolFiles.includes("novita-pool.json"));
});

test("数据目录里每个 provider 各留一份 worker 产物，互不覆盖", () => {
  const dir = tempDataDir();
  const runtime = createProviderRuntime({ dataDir: dir, port: 0, token: "t", store: fakeStore(), enabled: ["novita", "wavespeed"] });
  const registrations = [...runtime.registrations.keys()].sort();
  assert.deepEqual(registrations, ["novita", "wavespeed"]);
  // 状态文件按 provider 命名，两个 job 不会写同一份
  const paths = [...runtime.registrations.values()].map((r) => r.statePath);
  assert.equal(new Set(paths).size, paths.length);
  assert.ok(paths.some((p) => p.includes("novita-register-job.json")));
  assert.ok(paths.some((p) => p.includes("wavespeed-register-job.json")));
  assert.deepEqual(readdirSync(dir), [], "装配阶段不应写盘");
});
