// 候选线登记表的完整性 + 评分逻辑测试（纯本地，不联网）。
import { test } from "node:test";
import assert from "node:assert/strict";

import {
  CANDIDATES,
  DIMENSIONS,
  WEIGHTS,
  byId,
  emailOnlyPicks,
  gatedPicks,
  rankCandidates,
  scoreLine,
  toMarkdown,
  workerPlan,
} from "../src/line-candidates.mjs";

test("registry is internally consistent", () => {
  const ids = new Set();
  for (const row of CANDIDATES) {
    assert.ok(/^[a-z][a-z0-9-]*$/.test(row.id), `id 规范：${row.id}`);
    assert.ok(!ids.has(row.id), `id 唯一：${row.id}`);
    ids.add(row.id);
    assert.ok(row.name && row.baseUrl.startsWith("https://"), `${row.id} baseUrl 必须 https`);
    assert.ok(["firstparty", "inference", "aggregator", "cn"].includes(row.category), `${row.id} category`);
    assert.ok(["build", "watch", "skip"].includes(row.verdict), `${row.id} verdict`);
    assert.ok(row.reason && row.reason.length > 10, `${row.id} 要有结论理由`);
    assert.ok(Array.isArray(row.evidence) && row.evidence.length >= 1, `${row.id} 要有证据链接`);
    assert.ok(Array.isArray(row.free.models) && row.free.models.length >= 1, `${row.id} 免费模型`);
    assert.ok(Array.isArray(row.signup.methods) && row.signup.methods.length >= 1, `${row.id} 注册方式`);
    for (const dim of DIMENSIONS) {
      assert.ok(row[dim] >= 0 && row[dim] <= 5, `${row.id}.${dim} 必须在 0..5`);
    }
    assert.ok(row.penalty >= 0 && row.penalty <= 1, `${row.id}.penalty 必须在 0..1`);
  }
  assert.ok(CANDIDATES.length >= 20, "至少 20 条候选");
});

test("score follows the documented formula and stays in range", () => {
  const row = byId("agnes");
  const expected = WEIGHTS.automation * row.automation + WEIGHTS.relay * row.relay + WEIGHTS.quota * row.quota - row.penalty;
  assert.equal(scoreLine(row), Math.round(expected * 100) / 100);

  for (const candidate of CANDIDATES) {
    const score = scoreLine(candidate);
    assert.ok(score > -1.5 && score <= 5, `${candidate.id} score ${score} 越界`);
  }
});

test("ranking is descending and stable on ties", () => {
  const ranked = rankCandidates();
  for (let i = 1; i < ranked.length; i++) {
    const prev = ranked[i - 1];
    const cur = ranked[i];
    assert.ok(prev.score >= cur.score, "分数必须降序");
    if (prev.score === cur.score) assert.ok(prev.automation >= cur.automation, "同分时 automation 高的在前");
  }
  assert.equal(ranked.length, CANDIDATES.length);
});

test("the obvious email-only picks are on top and card-gated ones are not", () => {
  const picks = emailOnlyPicks();
  assert.ok(picks.length >= 5, "至少要能挑出 5 条纯邮箱线");
  for (const row of picks) {
    assert.equal(row.signup.card, false);
    assert.equal(row.signup.phone, false);
    assert.ok(row.automation >= 4 && row.relay >= 4);
  }
  const top3 = picks.slice(0, 3).map((row) => row.id);
  assert.ok(top3.includes("agnes"), `agnes 应该在前三：${top3.join(",")}`);

  const gated = gatedPicks();
  assert.ok(gated.some((row) => row.id === "kluster"), "绑卡的线要能被单独挑出来");
  assert.ok(gated.some((row) => row.id === "runcrate"), "GitHub 门槛要能挑出来");
  // 高分的纯邮箱线不应该出现在 gated 里
  for (const id of top3) assert.ok(!gated.some((row) => row.id === id), `${id} 不该被判成 gated`);
});

test("workerPlan produces the repo's 7-step checklist", () => {
  const plan = workerPlan("cerebras");
  assert.equal(plan.id, "cerebras");
  assert.equal(plan.verdict, "build");
  assert.ok(plan.files.includes("src/cerebras-pool.mjs"));
  assert.ok(plan.files.some((file) => file.includes("tools/cerebras-reg/cerebras_reg")));
  assert.ok(plan.steps.some((step) => step.includes("check-loop.mjs")), "要提登记闭环自检");
  assert.ok(plan.steps.some((step) => step.includes("绑卡：不需要")));
  assert.ok(plan.steps.some((step) => /OpenAI|openai/.test(step)), "要写反代契合");
  assert.equal(workerPlan("nope"), null);

  const mistral = workerPlan("mistral");
  assert.ok(mistral.steps.some((step) => step.includes("手机号")), "mistral 要提示手机号");
});

test("markdown export lists every line with a score column", () => {
  const md = toMarkdown();
  assert.match(md.split("\n")[0], /^\| 分数 \| 线 \|/);
  for (const row of CANDIDATES) {
    assert.ok(md.includes(row.name), `表格缺 ${row.name}`);
  }
});

test("at least a few lines are recommended to build", () => {
  const build = rankCandidates().filter((row) => row.verdict === "build");
  assert.ok(build.length >= 8, `build 太少：${build.length}`);
  // 结论与评分方向一致：skip 的平均分应当明显低于 build
  const avg = (rows) => rows.reduce((sum, row) => sum + row.score, 0) / Math.max(1, rows.length);
  assert.ok(avg(build) > avg(rankCandidates().filter((row) => row.verdict === "skip")));
});
