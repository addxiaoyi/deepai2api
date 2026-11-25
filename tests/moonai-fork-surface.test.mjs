// Fork 表面的静态检查（不需要 vendor/open-webui 就能跑）。
//
// vendor 没还原时无法 `npm run build`，但"接线错误"（路由拼错、组件导入了
// 不存在的函数、面板没挂载、Svelte 块不配对）用文本就能抓住，而且比构建快得多。
import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import { join } from "node:path";
import test from "node:test";

const root = process.cwd();
const read = (rel) => readFileSync(join(root, rel), "utf8");

const ROUTER = "patches/files/backend/open_webui/routers/deepai.py";
const API = "patches/files/src/lib/apis/deepai/index.ts";
const PAGE = "patches/files/src/lib/components/admin/Settings/DeepAI.svelte";
const CARD = "patches/files/src/lib/components/admin/Settings/MoonAI.svelte";

const routesOf = (source) =>
  [...source.matchAll(/@router\.(get|post|delete)\("([^"]+)"/g)].map((m) => `${m[1].toUpperCase()} ${m[2]}`);

const exportsOf = (source) =>
  new Set([...source.matchAll(/export const (\w+)/g)].map((m) => m[1]));

test("router: 所有 provider 透传路由都在", () => {
  const routes = routesOf(read(ROUTER));
  for (const expected of [
    "GET /providers",
    "GET /providers/{provider_id}",
    "GET /providers/{provider_id}/register",
    "POST /providers/{provider_id}/register",
    "POST /providers/{provider_id}/register/stop",
    "GET /providers/{provider_id}/pool",
    "POST /providers/{provider_id}/pool/import",
    "POST /providers/{provider_id}/pool/{account_id}/health",
    "DELETE /providers/{provider_id}/pool/{account_id}",
    "POST /providers/{provider_id}/pool/export",
    "GET /providers/{provider_id}/keys",
    "POST /providers/{provider_id}/probe",
    "GET /providers/{provider_id}/connect",
  ]) {
    assert.ok(routes.includes(expected), `缺少路由 ${expected}`);
  }
  // 既有路由没被顶掉
  for (const legacy of ["GET /status", "POST /register", "GET /poe/accounts", "GET /keys"]) {
    assert.ok(routes.includes(legacy), `既有路由丢失：${legacy}`);
  }
});

test("router: provider 路由都用 snake_case 表单并做 camelCase 映射", () => {
  const source = read(ROUTER);
  assert.match(source, /class ProviderRegisterForm\(BaseModel\)/);
  for (const field of ["browser_stack", "mail_provider", "keys_per_account", "verify_email", "anti_ban"]) {
    assert.ok(source.includes(field), `表单字段缺失：${field}`);
  }
  // 网关侧收的是 camelCase，必须转换
  for (const wire of ["browserStack", "mailProvider", "keysPerAccount", "verifyEmail", "antiBan"]) {
    assert.ok(source.includes(`"${wire}"`), `请求体缺少 ${wire}`);
  }
});

test("api client: 导出与路由同名、且都被卡片用到", () => {
  const api = read(API);
  const exported = exportsOf(api);
  for (const fn of [
    "listProviders",
    "getProvider",
    "getProviderRegister",
    "startProviderRegister",
    "stopProviderRegister",
    "getProviderPool",
    "importProviderAccount",
    "reportProviderHealth",
    "deleteProviderAccount",
    "probeProvider",
    "getProviderKeys",
    "getProviderConnectInfo",
    "exportProviderPool",
    "downloadProviderPool",
  ]) {
    assert.ok(exported.has(fn), `index.ts 缺少导出 ${fn}`);
  }

  const card = read(CARD);
  const importBlock = /import \{([^}]*)\} from '\$lib\/apis\/deepai'/.exec(card)?.[1] ?? "";
  const imported = importBlock
    .split(",")
    .map((name) => name.trim())
    .filter(Boolean);
  assert.ok(imported.length >= 6, "卡片应当从 deepai 客户端导入若干函数");
  for (const name of imported) {
    assert.ok(exported.has(name), `MoonAI.svelte 导入了不存在的函数：${name}`);
  }
});

test("卡片: Svelte 块配对 + 关键交互都在", () => {
  const card = read(CARD);
  const count = (needle) => (card.match(new RegExp(needle.replace(/[{}]/g, "\\$&"), "g")) ?? []).length;
  assert.equal(count("<script"), 1);
  assert.equal(count("</script>"), 1);
  assert.equal(count("{#each"), count("{/each}"));
  assert.equal(count("{#if"), count("{/if}"));

  for (const must of [
    "startProviderRegister",
    "stopProviderRegister",
    "probeProvider",
    "downloadProviderPool",
    "getProviderConnectInfo",
    "importProviderAccount",
    "removeAccount",
    "MoonAI 号池",
  ]) {
    assert.ok(card.includes(must), `卡片缺少 ${must}`);
  }
  // 手工导入：一行一个 JSON 或数组，两种都要认
  assert.ok(card.includes("parseAccounts"));
  assert.match(card, /startsWith\('\['\)/);
  // placeholder 里的 JSON 必须走变量，直接写会被 Svelte 当插值（编译报 Expected token }）
  assert.equal(/placeholder='\{/.test(card), false, "placeholder 里不能直接写 {");
  // 令牌明文绝不能出现在面板上
  assert.equal(card.includes("accessToken}"), false);
  assert.equal(card.includes("account.accessToken"), false);
});

test("页面: MoonAI 卡片已挂载（未改 SearchModal 等上游文件）", () => {
  const page = read(PAGE);
  assert.match(page, /import MoonAI from '\$lib\/components\/admin\/Settings\/MoonAI\.svelte'/);
  assert.match(page, /<MoonAI \/>/);
  // 挂载点必须在根容器内、加载提示之后
  const mountIndex = page.indexOf("<MoonAI />");
  const loadingIndex = page.indexOf("{#if loading}");
  assert.ok(loadingIndex > 0 && mountIndex > loadingIndex, "<MoonAI /> 应挂在页面末尾");
});

test("fork 文档记录了新文件与新路由", () => {
  const doc = read("patches/files/DEEPAI_FORK.md");
  assert.ok(doc.includes("MoonAI.svelte"));
  assert.ok(doc.includes("/api/v1/deepai/providers"));
});
