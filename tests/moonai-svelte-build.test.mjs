// 真实 Svelte 编译校验（不需要 vendor/open-webui，但需要 svelte 工具链）。
//
//   npm install --no-save --no-package-lock svelte@4 svelte-preprocess@5 typescript@5
//
// 没装时这个用例自动 skip 而不是失败 —— 它验证的是"组件能否编译"，
// 不是"仓库是否装了编译器"。
import assert from "node:assert/strict";
import { spawnSync } from "node:child_process";
import test from "node:test";

const run = () =>
  spawnSync(process.execPath, ["scripts/validate-svelte.mjs"], {
    cwd: process.cwd(),
    encoding: "utf8",
    timeout: 120_000,
  });

test("fork 组件能被真实 Svelte 编译器编译", (t) => {
  const result = run();
  if (result.status === 2) {
    t.skip("没装 svelte 工具链（npm install --no-save svelte@4 svelte-preprocess@5 typescript@5）");
    return;
  }
  assert.equal(result.status, 0, `${result.stdout}\n${result.stderr}`);
  assert.match(result.stdout, /2\/2 编译通过/);
  assert.match(result.stdout, /MoonAI\.svelte/);
});
