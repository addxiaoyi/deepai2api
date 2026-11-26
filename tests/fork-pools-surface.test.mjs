// Fork 表面检查（第二条）：号池总览 + Clever AI / Gab AI 的接线。
//
// 与 `moonai-fork-surface.test.mjs` 同一思路：不还原 vendor 也能抓住"路由拼错、
// 组件导入了不存在的函数、面板没挂载"这类错误。Svelte 语法本身由
// `scripts/validate-svelte.mjs` 负责（真实编译器）。
import assert from "node:assert/strict";
import { existsSync, readFileSync } from "node:fs";
import { join } from "node:path";
import test from "node:test";

const root = process.cwd();
const read = (rel) => readFileSync(join(root, rel), "utf8");

const ROUTER = "patches/files/backend/open_webui/routers/deepai.py";
const API = "patches/files/src/lib/apis/deepai/index.ts";
const PAGE = "patches/files/src/lib/components/admin/Settings/DeepAI.svelte";
const POOLS = "patches/files/src/lib/components/admin/Settings/Pools.svelte";

const routesOf = (source) =>
  [...source.matchAll(/@router\.(get|post|delete)\("([^"]+)"/g)].map((m) => `${m[1].toUpperCase()} ${m[2]}`);

const exportsOf = (source) => new Set([...source.matchAll(/export const (\w+)/g)].map((m) => m[1]));

test("router: 总览 + Clever AI + Gab AI 的透传路由都在", () => {
  const routes = routesOf(read(ROUTER));
  for (const expected of [
    "GET /pools",
    "GET /cleverai/status",
    "POST /cleverai/register",
    "POST /cleverai/register/stop",
    "GET /cleverai/pool",
    "POST /cleverai/pool/import",
    "POST /cleverai/pool/export",
    "DELETE /cleverai/pool/{account_id}",
    "GET /cleverai/keys",
    "POST /cleverai/appcheck",
    "GET /cleverai/connect",
    "GET /gabai/status",
    "POST /gabai/register",
    "POST /gabai/register/stop",
    "GET /gabai/pool",
    "POST /gabai/pool/import",
    "POST /gabai/pool/{account_id}/mint",
    "POST /gabai/pool/export",
    "DELETE /gabai/pool/{account_id}",
    "GET /gabai/connect",
    "GET /proxyscrape/status",
    "POST /proxyscrape/register",
    "POST /proxyscrape/register/stop",
    "GET /proxyscrape/pool",
    "GET /proxyscrape/proxies",
    "POST /proxyscrape/pool/export",
    "GET /proxies",
    "POST /proxies/apply",
    "GET /lines",
    "GET /lines/{line}/status",
    "POST /lines/{line}/register",
    "POST /lines/{line}/register/stop",
    "GET /lines/{line}/pool",
    "POST /lines/{line}/pool/import",
    "POST /lines/{line}/pool/export",
    "GET /lines/{line}/keys",
    "POST /lines/{line}/apply",
    "GET /lines/{line}/connect",
    "DELETE /lines/{line}/pool/{account_id}",
    "GET /amd/status",
    "GET /amd/pool",
    "POST /amd/pool/import",
    "GET /amd/courses",
    "POST /amd/study",
    "POST /amd/study/stop",
    "POST /amd/exam",
    "POST /amd/vouchers",
    "POST /amd/exchange",
    "POST /amd/models",
    "POST /amd/consolidate",
    "POST /amd/apply",
    "GET /jevai/status",
    "POST /jevai/register",
    "POST /jevai/register/stop",
    "GET /jevai/pool",
    "GET /jevai/keys",
    "POST /jevai/apply",
    "GET /jevai/connect",
    "POST /jevai/checkin",
    "GET /proxies/formats",
    "GET /proxies/export",
    "POST /proxies/check",
    "GET /proxies/shares",
    "POST /proxies/share",
    "DELETE /proxies/shares/{token}",
  ]) {
    assert.ok(routes.includes(expected), `缺少路由 ${expected}`);
  }
  // 既有路由不能被顶掉
  for (const legacy of ["GET /status", "POST /register", "GET /providers", "GET /poe/accounts"]) {
    assert.ok(routes.includes(legacy), `既有路由丢失：${legacy}`);
  }
});

test("router: 表单用 snake_case，转发到网关时转 camelCase", () => {
  const source = read(ROUTER);
  assert.match(source, /class CleveraiRegisterForm\(BaseModel\)/);
  assert.match(source, /class GabaiRegisterForm\(BaseModel\)/);
  for (const field of [
    "browser_stack",
    "mail_provider",
    "mail_domains",
    "appcheck",
    "captcha_key",
    "appcheck_token",
    "create_api_key",
  ]) {
    assert.ok(source.includes(field), `表单字段缺失：${field}`);
  }
  for (const wire of ['"browserStack"', '"mailProvider"', '"appcheckToken"', '"createApiKey"']) {
    assert.ok(source.includes(wire), `请求体缺少 ${wire}`);
  }
});

test("router: /pools 覆盖八条线且字段与 /health 对齐", () => {
  const source = read(ROUTER);
  for (const line of ['"deepai"', '"allmodels"', '"poe"', '"cleverai"', '"gabai"']) {
    assert.ok(source.includes(line), `/pools 少了 ${line}`);
  }
  // /health 的字段名
  for (const field of ["healthy_accounts", "allmodels", "poe", "providers", "cleverai", "gabai"]) {
    assert.ok(source.includes(field), `/pools 没读 health.${field}`);
  }
  // 反代前缀要能直接给下游用
  for (const prefix of ["/v1", "/p/", "/cleverai/v1", "/gab/v1"]) {
    assert.ok(source.includes(prefix), `/pools 缺少前缀 ${prefix}`);
  }
});

test("frontend: API 客户端导出了组件用到的每个函数", () => {
  const exported = exportsOf(read(API));
  const used = new Set(
    [...read(POOLS).matchAll(/import\s*\{([^}]+)\}\s*from\s*'\$lib\/apis\/deepai'/gs)]
      .flatMap((m) => m[1].split(","))
      .map((s) => s.trim())
      .filter(Boolean),
  );
  assert.ok(used.size > 10, "组件应该从 $lib/apis/deepai 引入接口");
  for (const name of used) {
    assert.ok(exported.has(name), `index.ts 没导出 ${name}（组件会 import 失败）`);
  }
  assert.ok(exported.has("getPoolsOverview"), "缺总览接口");
});

test("panel: Pools 组件被挂到设置页，且没有重复挂载", () => {
  const page = read(PAGE);
  assert.match(page, /import Pools from '\$lib\/components\/admin\/Settings\/Pools\.svelte'/);
  const mounts = page.match(/<Pools\s*\/>/g) ?? [];
  assert.equal(mounts.length, 1, "Pools 应当只挂载一次");
  // 仍然保留 MoonAI，避免覆盖别人的改动
  assert.ok(page.includes("<MoonAI />"), "MoonAI 挂载被弄丢了");
});

test("panel: 两条线都有启动/停止/导入/导出入口", () => {
  const card = read(POOLS);
  for (const fn of [
    "startCleveraiRegister",
    "stopCleveraiRegister",
    "importCleveraiAccount",
    "downloadCleveraiPool",
    "refreshCleveraiAppCheck",
    "startGabaiRegister",
    "stopGabaiRegister",
    "importGabaiAccount",
    "mintGabaiKey",
    "downloadGabaiPool",
    "startProxyscrapeRegister",
    "stopProxyscrapeRegister",
    "applySharedProxies",
    "downloadProxyscrapeProxies",
  ]) {
    assert.ok(card.includes(`${fn}(`), `面板缺少 ${fn} 调用`);
  }
  // 轮询要能停：onDestroy 必须清 timer
  assert.ok(card.includes("onDestroy"), "组件没有清理定时器");
  assert.match(card, /clearInterval/);
});

test("panel: 六条线各有独立页面（LinePools）", () => {
  const file = join(root, "patches/files/src/lib/components/admin/Settings/LinePools.svelte");
  assert.ok(existsSync(file), "缺少 LinePools.svelte");
  const card = readFileSync(file, "utf8");

  for (const fn of [
    "getLinesOverview",
    "getLineStatus",
    "startLineRegister",
    "stopLineRegister",
    "getLinePool",
    "getLineKeys",
    "applyLine",
    "getLineConnect",
    "downloadLineKeys",
    "deleteLineAccount"
  ]) {
    assert.ok(card.includes(fn), `LinePools 缺少 ${fn}`);
  }
  // 左栏 = 号池列表，等于侧边栏入口
  assert.ok(card.includes("lines as row"), "缺号池列表渲染");
  assert.match(card, /select\(row\.id\)/);
  // 轮询要能停
  assert.match(card, /clearInterval/);
  assert.ok(card.includes("onDestroy"), "组件没有清理定时器");

  // 设置页里必须挂上它
  const settings = read("patches/files/src/lib/components/admin/Settings/DeepAI.svelte");
  assert.ok(settings.includes("<LinePools />"), "DeepAI 设置页没挂 LinePools");
  assert.equal((settings.match(/<LinePools \/>/g) ?? []).length, 1, "只应挂一次");
});