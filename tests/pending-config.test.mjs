// 待配置项清单的完整性 + 环境检查逻辑测试（纯函数）。
import { test } from "node:test";
import assert from "node:assert/strict";

import { KINDS, PENDING, byId, checkEnv, forLine, openItems, summary, toMarkdown } from "../src/pending-config.mjs";

test("registry is consistent", () => {
  const ids = new Set();
  for (const item of PENDING) {
    assert.match(item.id, /^[a-z][a-z0-9-]*$/, `id 规范：${item.id}`);
    assert.ok(!ids.has(item.id), `id 唯一：${item.id}`);
    ids.add(item.id);
    assert.ok(KINDS.includes(item.kind), `${item.id} kind`);
    assert.ok(["open", "solved", "blocked"].includes(item.status), `${item.id} status`);
    assert.ok(item.title.length > 4 && item.impact.length > 6, `${item.id} 要写清影响`);
    assert.ok(item.how.length > 10, `${item.id} 要有"怎么补"`);
    assert.ok(Array.isArray(item.env) && item.env.length, `${item.id} 要列变量`);
    if (item.status === "blocked") assert.ok(item.evidence, `${item.id} blocked 必须带证据`);
  }
});

test("summary and filters agree", () => {
  const s = summary();
  assert.equal(s.total, PENDING.length);
  assert.equal(s.open + s.solved + s.blocked, s.total);
  assert.ok(s.byKind.credential >= 1 && s.byLine.all >= 1);

  assert.ok(forLine("agnes").some((item) => item.id === "agnes-captcha"));
  assert.ok(forLine("agnes").every((item) => item.line === "agnes" || item.line === "all"));
  assert.ok(openItems().every((item) => item.status !== "solved"));
  assert.equal(byId("nope"), null);
});

test("checkEnv: 缺变量 = 未就绪，solved 项永远就绪", () => {
  const empty = checkEnv({});
  const mail = empty.find((row) => row.id === "mail-domain");
  assert.equal(mail.ready, false);
  assert.ok(mail.missing.includes("LINE_MAIL_DOMAIN"));

  const solved = empty.find((row) => row.id === "agnes-captcha");
  assert.equal(solved.status, "solved");
  assert.equal(solved.ready, true, "代码已实现的项不因缺变量而未就绪");

  const filled = checkEnv({
    LINE_MAIL_DOMAIN: "example.com",
    LINE_IMAP_HOST: "imap.example.com",
    LINE_IMAP_USER: "a@example.com",
    LINE_IMAP_PASSWORD: "x",
  });
  assert.equal(filled.find((row) => row.id === "mail-domain").ready, true);
  assert.deepEqual(filled.find((row) => row.id === "mail-domain").missing, []);
});

test("markdown carries status marks and the how-to", () => {
  const md = toMarkdown(PENDING, {});
  assert.match(md.split("\n")[0], /^# 待配置项/);
  for (const item of PENDING) {
    assert.ok(md.includes(item.title), `缺标题 ${item.id}`);
  }
  assert.ok(md.includes("✅") && md.includes("⛔"), "要有状态标记");
  assert.match(md, /实测证据/);
});
