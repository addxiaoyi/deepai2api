// 用真实 Svelte 工具链校验 fork 里的 .svelte 文件（不需要 vendor/open-webui）。
//
// vendor 没还原时 `npm run build` 跑不了，但组件本身的语法 / 指令 / TS 类型
// 剥离错误用 svelte/compiler + svelte-preprocess 就能抓出来 —— 比正则强得多。
//
//   npm install --no-save --no-package-lock svelte@4 svelte-preprocess@5 typescript@5
//   node scripts/validate-svelte.mjs
//
// 没装依赖时退出码 2（CI 里可当 skip）。
import { readFileSync, readdirSync, statSync } from "node:fs";
import { join, relative } from "node:path";

const roots = process.argv.slice(2);
const targets = roots.length ? roots : ["patches/files/src"];

const walk = (dir) =>
  readdirSync(dir).flatMap((name) => {
    const full = join(dir, name);
    if (statSync(full).isDirectory()) return walk(full);
    return full.endsWith(".svelte") ? [full] : [];
  });

const files = targets.flatMap((root) => walk(root));
if (!files.length) {
  console.error(`没有找到 .svelte 文件（${targets.join(", ")}）`);
  process.exit(1);
}

let compile;
let preprocess;
let sveltePreprocess;
try {
  ({ compile, preprocess } = await import("svelte/compiler"));
  sveltePreprocess = (await import("svelte-preprocess")).default;
} catch {
  console.error(
    "需要 Svelte 工具链：npm install --no-save --no-package-lock svelte@4 svelte-preprocess@5 typescript@5",
  );
  process.exit(2);
}

const tsPreprocess = sveltePreprocess({ typescript: true });
const IGNORED_WARNINGS = new Set([
  "a11y_click_events_have_key_events",
  "a11y_no_static_element_interactions",
  "a11y_no_noninteractive_element_interactions",
  "a11y_label_has_associated_control",
  // Svelte 4 的 generate:"dom" 在新版本里改了名；Open WebUI 0.13.4 仍是 Svelte 4
  "options_renamed_ssr_dom",
]);

let failed = 0;
for (const file of files) {
  const source = readFileSync(file, "utf8");
  try {
    const pre = await preprocess(source, tsPreprocess, { filename: file });
    const result = compile(pre.code, {
      filename: file,
      generate: "dom",
      dev: false,
      css: "injected",
    });
    const warnings = (result.warnings ?? []).filter((w) => !IGNORED_WARNINGS.has(w.code));
    console.log(
      `ok   ${relative(process.cwd(), file)}  (${result.js?.code?.length ?? 0}B js${
        warnings.length ? `, ${warnings.length} warning` : ""
      })`,
    );
    for (const warning of warnings.slice(0, 5)) {
      console.log(`     warn ${warning.code}: ${warning.message}`);
    }
  } catch (error) {
    failed++;
    console.error(`FAIL ${relative(process.cwd(), file)}`);
    console.error(`     ${error.message}`);
    if (error.start) console.error(`     at line ${error.start.line}, column ${error.start.column}`);
  }
}

console.log(`\n${files.length - failed}/${files.length} 编译通过`);
process.exit(failed ? 1 : 0);
