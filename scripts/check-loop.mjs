#!/usr/bin/env node
// 闭环自检：把"注册 → 入池 → 出 Key → 反代 → 观测/回收"这条链上的每一环都点一遍。
//
//   node scripts/check-loop.mjs          # 打印矩阵，有缺口则 exit 1
//   node scripts/check-loop.mjs --json   # 机器可读
//
// 它只做静态检查（文件在不在、路由有没有真的登记、schema 字符串对不对），
// 不联网、不读凭据。动态验证交给各自的 e2e/测试。
import { existsSync, readFileSync, readdirSync } from "node:fs";
import { dirname, join, resolve } from "node:path";
import { fileURLToPath } from "node:url";

const here = dirname(fileURLToPath(import.meta.url));
const root = resolve(here, "..");
const read = (p) => readFileSync(join(root, p), "utf8");
const has = (p) => existsSync(join(root, p));

/** 每条线：五环 + 契约。缺字段就是"这条线没有这一环"。 */
export const LINES = [
  {
    id: "deepai",
    name: "DeepAI Code",
    kind: "upstream-lane",
    // 这一条没有"号池文件"：账号在 gateway-state.json 里（OAuth 登录写入）
    state: ["src/store.mjs"],
    worker: "tools/deepai-reg",
    register: ["src/register.mjs"],
    relay: ["src/lane.mjs", "src/anthropic.mjs"],
    routes: ["/v1/chat/completions", "/v1/messages", "/v1/models"],
    adminRoutes: ["/api/admin/register", "/api/admin/register/stop", "/api/admin/state"],
    envPrefix: "DEEPAI_",
    docs: [],
    tests: ["tests/e2e.mjs"],
  },
  {
    id: "allmodels",
    name: "Allmodels",
    kind: "pool-file",
    schema: "dcg.pool/allmodels@1",
    pool: ["src/allmodels-pool.mjs"],
    worker: "tools/allmodels-reg",
    register: ["src/allmodels-register.mjs"],
    relay: ["src/allmodels-tools.mjs"],
    cli: "src/allmodels-cli.mjs",
    routes: [],
    adminRoutes: ["/api/admin/allmodels/status", "/api/admin/allmodels/pool/import", "/api/admin/allmodels/pool/export"],
    envPrefix: "ALLMODELS_",
    docs: ["docs/ALLMODELS-POOL.md"],
    tests: ["tests/allmodels-pool.test.mjs", "tests/allmodels-e2e.mjs"],
  },
  {
    id: "poe",
    name: "Poe",
    kind: "pool-file",
    schema: "dcg.pool/poe",
    pool: ["src/poe-pool.mjs"],
    worker: "tools/poe-reg",
    register: ["src/register.mjs"],
    // Poe 只做「注册 + 号池」：p-b cookie 不能直接当 OpenAI 端点转发，反代不在范围内
    relay: [],
    routes: [],
    adminRoutes: ["/api/poe/accounts", "/api/poe/register", "/api/poe/health"],
    envPrefix: "POE_",
    docs: ["docs/POE-POOL.md"],
    tests: [],
  },
  {
    id: "providers",
    name: "Provider 运行时（wavespeed/novita/moonai/zo/blackbox）",
    kind: "provider-runtime",
    schema: "dcg.pool/",
    pool: ["src/provider-pool.mjs"],
    worker: "tools/moonai-reg",
    workers: ["tools/moonai-reg", "tools/wavespeed-reg", "tools/novita-reg", "tools/zo-reg", "tools/blackbox-reg"],
    register: ["src/provider-register.mjs"],
    relay: ["src/provider-relay.mjs", "src/providers.mjs"],
    cli: "src/provider-cli.mjs",
    routes: ["/p/"],
    adminRoutes: ["/api/admin/providers"],
    envPrefix: "DCG_PROVIDERS",
    docs: ["docs/PROVIDERS.md", "docs/MOONAI-POOL.md", "docs/NOVITA-POOL.md", "docs/BLACKBOX-POOL.md", "docs/ZO-POOL.md"],
    tests: ["tests/providers-wavespeed-novita.test.mjs", "tests/moonai-pool.test.mjs", "tests/blackbox-provider.test.mjs"],
  },
  {
    id: "cleverai",
    name: "Clever AI Hub",
    kind: "pool-file",
    schema: "dcg.pool/cleverai@1",
    pool: ["src/cleverai-pool.mjs"],
    worker: "tools/cleverai-reg",
    register: ["src/cleverai-register.mjs"],
    relay: ["src/cleverai-relay.mjs"],
    cli: "src/cleverai-cli.mjs",
    routes: ["/cleverai/v1/chat/completions", "/cleverai/v1/messages", "/cleverai/v1/models"],
    adminRoutes: ["/api/admin/cleverai/status", "/api/admin/cleverai/pool/import", "/api/admin/cleverai/pool/export"],
    envPrefix: "CLEVERAI_",
    docs: ["docs/CLEVERAI-POOL.md"],
    tests: ["tests/cleverai-pool.test.mjs", "tests/cleverai-routes.test.mjs"],
  },
  {
    id: "agnes",
    name: "Agnes AI",
    kind: "openai-line",
    schema: "dcg.pool/agnes@1",
    pool: ["src/agnes-pool.mjs"],
    worker: "tools/agnes-reg",
    register: ["src/agnes-register.mjs"],
    relay: ["src/agnes-relay.mjs"],
    cli: "src/agnes-cli.mjs",
    routes: ["/agnes/v1/"],
    adminRoutes: ["/api/admin/agnes/status", "/api/admin/agnes/pool/import", "/api/admin/agnes/pool/export"],
    envPrefix: "AGNES_",
    docs: ["docs/AGNES-POOL.md"],
    tests: ["tests/openai-lines.test.mjs", "tests/lines-routes.test.mjs"],
  },
  {
    id: "cerebras",
    name: "Cerebras",
    kind: "openai-line",
    schema: "dcg.pool/cerebras@1",
    pool: ["src/cerebras-pool.mjs"],
    worker: "tools/cerebras-reg",
    register: ["src/cerebras-register.mjs"],
    relay: ["src/cerebras-relay.mjs"],
    cli: "src/cerebras-cli.mjs",
    routes: ["/cerebras/v1/"],
    adminRoutes: ["/api/admin/cerebras/status", "/api/admin/cerebras/pool/import", "/api/admin/cerebras/pool/export"],
    envPrefix: "CEREBRAS_",
    docs: ["docs/CEREBRAS-POOL.md"],
    tests: ["tests/openai-lines.test.mjs", "tests/lines-routes.test.mjs"],
  },
  {
    id: "opencode-zen",
    name: "OpenCode Zen",
    kind: "openai-line",
    schema: "dcg.pool/opencode-zen@1",
    pool: ["src/opencode-zen-pool.mjs"],
    worker: "tools/opencode-zen-reg",
    register: ["src/opencode-zen-register.mjs"],
    relay: ["src/opencode-zen-relay.mjs"],
    cli: "src/opencode-zen-cli.mjs",
    routes: ["/opencode-zen/v1/"],
    adminRoutes: ["/api/admin/opencode-zen/status", "/api/admin/opencode-zen/pool/import", "/api/admin/opencode-zen/pool/export"],
    envPrefix: "OPENCODE_ZEN_",
    docs: ["docs/OPENCODE-ZEN-POOL.md"],
    tests: ["tests/openai-lines.test.mjs", "tests/lines-routes.test.mjs"],
  },
  {
    id: "requesty",
    name: "Requesty",
    kind: "openai-line",
    schema: "dcg.pool/requesty@1",
    pool: ["src/requesty-pool.mjs"],
    worker: "tools/requesty-reg",
    register: ["src/requesty-register.mjs"],
    relay: ["src/requesty-relay.mjs"],
    cli: "src/requesty-cli.mjs",
    routes: ["/requesty/v1/"],
    adminRoutes: ["/api/admin/requesty/status", "/api/admin/requesty/pool/import", "/api/admin/requesty/pool/export"],
    envPrefix: "REQUESTY_",
    docs: ["docs/REQUESTY-POOL.md"],
    tests: ["tests/openai-lines.test.mjs", "tests/lines-routes.test.mjs"],
  },
  {
    id: "unorouter",
    name: "UnoRouter",
    kind: "openai-line",
    schema: "dcg.pool/unorouter@1",
    pool: ["src/unorouter-pool.mjs"],
    worker: "tools/unorouter-reg",
    register: ["src/unorouter-register.mjs"],
    relay: ["src/unorouter-relay.mjs"],
    cli: "src/unorouter-cli.mjs",
    routes: ["/unorouter/v1/"],
    adminRoutes: ["/api/admin/unorouter/status", "/api/admin/unorouter/pool/import", "/api/admin/unorouter/pool/export"],
    envPrefix: "UNOROUTER_",
    docs: ["docs/UNOROUTER-POOL.md"],
    tests: ["tests/openai-lines.test.mjs", "tests/lines-routes.test.mjs"],
  },
  {
    id: "amd",
    name: "AMD 开发者云（GPU）",
    kind: "gpu-pool",
    schema: "dcg.pool/amd@1",
    pool: ["src/amd-pool.mjs"],
    worker: "tools/amd-reg",
    register: ["src/amd-register.mjs"],
    // GPU 资源不是 OpenAI 兼容面：没有模型反代，只有管理面
    relay: [],
    cli: "src/amd-cli.mjs",
    routes: [],
    adminRoutes: ["/api/admin/amd/status", "/api/admin/amd/pool/import", "/api/admin/amd/pool/export"],
    envPrefix: "AMD_",
    docs: ["docs/AMD-POOL.md"],
    tests: ["tests/amd-routes.test.mjs"],
  },
  {
    id: "giteeai",
    name: "Gitee AI（模力方舟）",
    kind: "openai-line",
    schema: "dcg.pool/giteeai@1",
    pool: ["src/giteeai-pool.mjs"],
    worker: "tools/giteeai-reg",
    register: ["src/giteeai-register.mjs"],
    relay: ["src/giteeai-relay.mjs"],
    cli: "src/giteeai-cli.mjs",
    routes: ["/giteeai/v1/"],
    adminRoutes: ["/api/admin/giteeai/status", "/api/admin/giteeai/pool/import", "/api/admin/giteeai/pool/export"],
    envPrefix: "GITEEAI_",
    docs: ["docs/GITEEAI-POOL.md"],
    tests: ["tests/openai-lines.test.mjs", "tests/lines-routes.test.mjs"],
  },
  {
    id: "pollinations",
    name: "Pollinations",
    kind: "openai-line",
    schema: "dcg.pool/pollinations@1",
    pool: ["src/pollinations-pool.mjs"],
    worker: "tools/pollinations-reg",
    register: ["src/pollinations-register.mjs"],
    relay: ["src/pollinations-relay.mjs"],
    cli: "src/pollinations-cli.mjs",
    routes: ["/pollinations/v1/"],
    adminRoutes: ["/api/admin/pollinations/status", "/api/admin/pollinations/pool/import", "/api/admin/pollinations/pool/export"],
    envPrefix: "POLLINATIONS_",
    docs: ["docs/POLLINATIONS-POOL.md"],
    tests: ["tests/openai-lines.test.mjs", "tests/lines-routes.test.mjs"],
  },
  {
    id: "jevai",
    name: "JEV AI Key 池",
    kind: "key-pool",
    schema: "dcg.pool/jevai@1",
    pool: ["src/jevai-pool.mjs"],
    worker: "tools/jevai-reg",
    register: ["src/jevai-register.mjs"],
    relay: ["src/jevai-relay.mjs"],
    cli: "src/jevai-cli.mjs",
    routes: ["/jevai/v1/"],
    adminRoutes: ["/api/admin/jevai/status", "/api/admin/jevai/pool/import", "/api/admin/jevai/apply"],
    envPrefix: "JEVAIS_",
    docs: ["docs/JEVAI-POOL.md"],
    tests: ["tests/jevai-pool.test.mjs", "tests/jevai-routes.test.mjs"],
  },  {
    id: "proxyscrape",
    name: "ProxyScrape 代理池",
    kind: "proxy-pool",
    schema: "dcg.pool/proxyscrape@1",
    pool: ["src/proxyscrape-pool.mjs"],
    worker: "tools/proxyscrape-reg",
    register: ["src/proxyscrape-register.mjs"],
    // 产物是出口，不是模型通道：分发给其它线用，所以没有反代面
    relay: [],
    cli: "src/proxyscrape-cli.mjs",
    routes: [],
    adminRoutes: ["/api/admin/proxyscrape/status", "/api/admin/proxyscrape/pool/import", "/api/admin/proxies/apply"],
    envPrefix: "PROXYSCRAPE_",
    docs: ["docs/PROXYSCRAPE-POOL.md"],
    tests: ["tests/proxyscrape-pool.test.mjs", "tests/proxyscrape-routes.test.mjs"],
  },  {
    id: "gabai",
    name: "Gab AI",
    kind: "pool-file",
    schema: "dcg.pool/gabai@1",
    pool: ["src/gabai-pool.mjs"],
    worker: "tools/gabai-reg",
    register: ["src/gabai-register.mjs"],
    relay: ["src/gabai-proxy.mjs"],
    routes: ["/gab/v1/chat/completions", "/gab/v1/messages"],
    adminRoutes: ["/api/admin/gabai/status", "/api/admin/gabai/pool/import"],
    envPrefix: "GABAI_",
    docs: ["docs/GABAI-POOL.md"],
    tests: ["tests/gabai-routes.test.mjs"],
  },
];

const WORKER_ENTRIES = ["cli.py", "__main__.py"];

/** 一条线可能带多个 worker（provider 运行时就是 5 个）。 */
const workerList = (line) => [line.worker, ...(line.workers ?? [])].filter(Boolean);

/** 已知豁免：目录存在但明确不是产品线（要有理由，不能空着）。 */
export const EXEMPT_WORKERS = {
  "overchat-reg": "只有逆向留档与骨架包，未登记为 LINES；见 tools/overchat-reg/README.md",
};

function checkLine(line, serverSource, envSource) {
  const problems = [];
  const detail = {};

  if (line.schema) {
    const pool = line.pool?.[0];
    if (!pool || !has(pool)) {
      problems.push(`号池模块缺失（${pool ?? "未声明"}）`);
    } else if (!read(pool).includes(line.schema)) {
      problems.push(`号池模块里没有 schema 字符串 ${line.schema}`);
    }
  }
  for (const file of [...(line.state ?? []), ...(line.pool ?? []), ...(line.register ?? []), ...(line.relay ?? []), ...(line.cli ? [line.cli] : [])]) {
    if (!has(file)) problems.push(`文件缺失 ${file}`);
  }

  for (const worker of workerList(line)) {
    const pkg = worker.split("/").pop().replace(/-/g, "_");
    const entries = WORKER_ENTRIES.map((e) => join(worker, pkg, e));
    const found = entries.filter(has);
    if (!found.length) problems.push(`worker 入口缺失 ${worker}/${pkg}/{cli.py,__main__.py}`);

    // 有 worker 就必须有离线协议测试：不然"跑通了"只能靠人品
    const testsDir = join(worker, "tests");
    if (!has(testsDir)) {
      problems.push(`worker 离线测试缺失 ${testsDir}/`);
    } else {
      const count = readdirSync(join(root, testsDir)).filter((f) => f.endsWith(".py")).length;
      if (!count) problems.push(`worker 离线测试为空 ${testsDir}/`);
    }
  }

  // 环境变量：每条线的前缀要能在 .env.example 里查到，部署时才知道要配什么
  if (line.envPrefix && envSource && !envSource.includes(line.envPrefix)) {
    problems.push(`.env.example 缺少 ${line.envPrefix}* 变量`);
  }

  for (const route of line.routes ?? []) {
    // 路由可以逐条写死，也可以按前缀挂载（`/gab/v1/*` 就是一个前缀）。
    const parts = route.split("/").filter(Boolean);
    const prefix = parts.length >= 2 ? `/${parts.slice(0, 2).join("/")}/` : route;
    const wired = serverSource.includes(route) || serverSource.includes(`"${route}`) || serverSource.includes(prefix);
    if (!wired) problems.push(`反代路由未接线 ${route}`);
  }
  for (const route of line.adminRoutes ?? []) {
    if (!serverSource.includes(route)) problems.push(`管理路由未接线 ${route}`);
  }

  for (const doc of line.docs ?? []) if (!has(doc)) problems.push(`文档缺失 ${doc}`);
  for (const test of line.tests ?? []) if (!has(test)) problems.push(`测试缺失 ${test}`);

  return { problems, detail };
}

function main() {
  const json = process.argv.includes("--json");
  const serverSource = read("src/server.mjs");
  const envSource = has(".env.example") ? read(".env.example") : "";
  const rows = [];
  let gaps = 0;

  for (const line of LINES) {
    const { problems } = checkLine(line, serverSource, envSource);
    gaps += problems.length;
    rows.push({ ...line, problems, ok: problems.length === 0 });
  }

  if (json) {
    process.stdout.write(
      JSON.stringify(
        {
          ok: gaps === 0,
          lines: rows.map((r) => ({
            id: r.id,
            name: r.name,
            kind: r.kind,
            schema: r.schema ?? null,
            routes: r.routes ?? [],
            adminRoutes: r.adminRoutes ?? [],
            worker: r.worker ?? null,
            docs: r.docs ?? [],
            tests: r.tests ?? [],
            ok: r.ok,
            problems: r.problems,
          })),
        },
        null,
        2,
      ) + "\n",
    );
    process.exitCode = gaps === 0 ? 0 : 1;
    return;
  }

  const pad = (s, n) => String(s).padEnd(n);
  const mark = (ok) => (ok ? "✅" : "❌");

  console.log("闭环自检：注册 → 入池 → 出 Key → 反代 → 观测/回收\n");
  console.log(
    pad("线", 16) + pad("号池 schema", 26) + pad("worker", 22) + pad("反代", 8) + pad("管理", 8) + "结论",
  );
  console.log("-".repeat(96));
  for (const row of rows) {
    console.log(
      pad(row.id, 16) +
        pad(row.schema ?? "（state 内）", 26) +
        pad(row.worker ? "✅" : "—", 22) +
        pad((row.routes ?? []).length ? "✅" : "—", 8) +
        pad((row.adminRoutes ?? []).length ? "✅" : "—", 8) +
        mark(row.ok),
    );
  }
  console.log("-".repeat(96));

  // 孤儿检查：目录里有的东西，要么登记成一条线，要么在 EXEMPT 里写明理由。
  const workersOnDisk = readdirSync(join(root, "tools"), { withFileTypes: true })
    .filter((d) => d.isDirectory() && d.name.endsWith("-reg"))
    .map((d) => d.name);
  const registeredWorkers = new Set(LINES.flatMap((l) => workerList(l).map((w) => w.split("/").pop())));
  const orphanWorkers = workersOnDisk.filter((w) => !registeredWorkers.has(w) && !EXEMPT_WORKERS[w]);

  const poolsOnDisk = readdirSync(join(root, "src"))
    .filter((f) => f.endsWith("-pool.mjs"))
    .map((f) => f);
  const registeredPools = new Set(LINES.flatMap((l) => (l.pool ?? []).map((p) => p.split("/").pop())));
  const orphanPools = poolsOnDisk.filter((p) => !registeredPools.has(p));

  if (orphanWorkers.length) {
    console.log(`\n未登记的 worker：${orphanWorkers.join(", ")}（补进 LINES，或写进 EXEMPT_WORKERS 说明理由）`);
  }
  if (orphanPools.length) {
    console.log(`\n未登记的号池模块：${orphanPools.join(", ")}（补进 LINES 的 pool 字段）`);
  }
  const orphanGaps = orphanWorkers.length + orphanPools.length;

  if (!gaps && !orphanGaps) {
    console.log(`\n所有声明的环都在位（${LINES.length} 条线，${workersOnDisk.length} 个 worker 目录全部登记）。`);
    return;
  }
  if (gaps) {
    console.log(`\n发现 ${gaps} 处缺口：`);
    for (const row of rows) {
      if (row.ok) continue;
      console.log(`\n  ${row.id}（${row.name}）`);
      for (const problem of row.problems) console.log(`    - ${problem}`);
    }
  }
  process.exitCode = 1;
}

main();
