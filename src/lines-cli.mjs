#!/usr/bin/env node
// 候选线 CLI：看看下一个该做哪条线、要付出什么、怎么落地。
//
//   node src/lines-cli.mjs list                 # 按分数排
//   node src/lines-cli.mjs list --only email     # 只要邮箱就能跑的
//   node src/lines-cli.mjs list --only gated     # 需要手机号/GitHub/绑卡的
//   node src/lines-cli.mjs show agnes            # 一条线的全部细节
//   node src/lines-cli.mjs plan agnes            # 生成"新增这条线"的落地清单
//   node src/lines-cli.mjs probe agnes           # 打一次 /models 看还活着没（联网）
//   node src/lines-cli.mjs markdown --out docs/FREE-LINES.md
import { mkdirSync, writeFileSync } from "node:fs";
import { dirname, resolve } from "node:path";

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
} from "./line-candidates.mjs";

const USAGE = `lines-cli — 免费模型渠道登记表（反代 + 注册机可行性）

  list [--only all|email|gated] [--verdict build|watch|skip] [--limit n] [--json]
  show <id> [--json]
  plan <id>                      新增一条线的 7 步清单
  probe <id> [--timeout ms]      打上游 /models 看连通性与鉴权形状（联网）
  markdown [--out file]          导出 markdown 表格
  dims                           评分维度与权重
`;

function parseArgs(argv) {
  const out = { _: [] };
  for (let i = 0; i < argv.length; i++) {
    const arg = argv[i];
    if (!arg.startsWith("-")) {
      out._.push(arg);
      continue;
    }
    const name = arg.replace(/^-+/, "");
    if (name.includes("=")) {
      const [k, v] = name.split("=", 2);
      out[k] = v;
      continue;
    }
    const next = argv[i + 1];
    if (next !== undefined && !next.startsWith("-")) {
      out[name] = next;
      i++;
    } else {
      out[name] = true;
    }
  }
  return out;
}

const pad = (value, width) => String(value).padEnd(width, " ");

function rowLine(row) {
  const gate = [
    row.signup.methods.join("/"),
    row.signup.phone ? "手机" : null,
    row.signup.card ? "绑卡" : null,
    row.signup.alipay ? "支付宝" : null,
    row.signup.emailVerification ? "验证信" : null,
  ]
    .filter(Boolean)
    .join("+");
  const quota = `${row.free.rpm || "-"}rpm/${row.free.rpd || "-"}rpd`;
  return [
    String(row.score).padStart(5),
    pad(row.id, 14),
    pad(row.verdict, 6),
    pad(quota, 14),
    pad(row.api.openaiCompat === "full" ? "openai" : row.api.openaiCompat, 9),
    pad(gate, 26),
    row.name,
  ].join(" ");
}

function cmdList(args) {
  const only = String(args.only ?? "all");
  let rows = rankCandidates();
  if (only === "email") rows = emailOnlyPicks();
  else if (only === "gated") rows = gatedPicks();
  else if (only !== "all") {
    process.stderr.write(`unknown --only ${only}\n`);
    return 2;
  }
  if (args.verdict) rows = rows.filter((row) => row.verdict === String(args.verdict));
  const limit = Number(args.limit ?? 0);
  if (limit > 0) rows = rows.slice(0, limit);

  if (args.json) {
    process.stdout.write(JSON.stringify({ total: rows.length, lines: rows }, null, 2) + "\n");
    return 0;
  }
  process.stdout.write(
    `${pad("score", 5)} ${pad("id", 14)} ${pad("verdict", 6)} ${pad("quota", 14)} ${pad("compat", 9)} ${pad("门槛", 26)} name\n`,
  );
  process.stdout.write("-".repeat(110) + "\n");
  for (const row of rows) process.stdout.write(rowLine(row) + "\n");
  process.stdout.write(`\n共 ${rows.length} 条（build ${rows.filter((r) => r.verdict === "build").length}）\n`);
  return 0;
}

function cmdShow(args) {
  const id = args._[1];
  const row = byId(id);
  if (!row) {
    process.stderr.write(`未知 id：${id ?? "(missing)"}\n`);
    return 2;
  }
  if (args.json) {
    process.stdout.write(JSON.stringify({ ...row, score: scoreLine(row) }, null, 2) + "\n");
    return 0;
  }
  const lines = [
    `${row.name}  (${row.id})  score=${scoreLine(row)}  verdict=${row.verdict}`,
    `base    ${row.baseUrl}`,
    `免模型  ${(row.free.models || []).join(", ")}`,
    `额度    ${row.free.rpm || "-"} RPM / ${row.free.rpd || "-"} RPD  ${row.free.note ?? ""}`,
    `注册    ${row.signup.methods.join(", ")}` +
      `${row.signup.emailVerification ? " + 邮箱验证" : ""}` +
      `${row.signup.phone ? " + 手机号" : ""}` +
      `${row.signup.card ? " + 绑卡" : ""}` +
      `${row.signup.alipay ? " + 支付宝" : ""}` +
      `${row.signup.note ? `（${row.signup.note}）` : ""}`,
    `反代    openai=${row.api.openaiCompat} stream=${row.api.stream} tools=${row.api.tools} anthropic=${row.api.anthropic}`,
    `评分    automation=${row.automation} relay=${row.relay} quota=${row.quota} penalty=${row.penalty}`,
    `结论    ${row.reason}`,
    `证据    ${(row.evidence || []).join("\n        ")}`,
  ];
  process.stdout.write(lines.join("\n") + "\n");
  return 0;
}

function cmdPlan(args) {
  const plan = workerPlan(args._[1]);
  if (!plan) {
    process.stderr.write(`未知 id：${args._[1] ?? "(missing)"}\n`);
    return 2;
  }
  if (args.json) {
    process.stdout.write(JSON.stringify(plan, null, 2) + "\n");
    return 0;
  }
  process.stdout.write(`# ${plan.name}（${plan.id}）score=${plan.score} ${plan.verdict}\n\n要建的文件：\n`);
  for (const file of plan.files) process.stdout.write(`  - ${file}\n`);
  process.stdout.write("\n落地步骤：\n");
  plan.steps.forEach((step, index) => process.stdout.write(`  ${index + 1}. ${step}\n`));
  process.stdout.write(`\n证据：\n${plan.docs.map((url) => `  - ${url}`).join("\n")}\n`);
  return 0;
}

async function cmdProbe(args) {
  const row = byId(args._[1]);
  if (!row) {
    process.stderr.write(`未知 id：${args._[1] ?? "(missing)"}\n`);
    return 2;
  }
  const timeout = Number(args.timeout ?? 15000);
  const url = `${row.baseUrl.replace(/\/+$/, "")}/models`;
  const controller = new AbortController();
  const timer = setTimeout(() => controller.abort(), timeout);
  const started = Date.now();
  try {
    const response = await fetch(url, { signal: controller.signal, headers: { Accept: "application/json" } });
    const text = (await response.text()).slice(0, 200);
    const verdict =
      response.ok
        ? "匿名可列模型（异常：多数站点应要求 Key）"
        : response.status === 401 || response.status === 403
          ? "存活，要求鉴权（正常）"
          : response.status === 404
            ? "base 路径可能不对"
            : `HTTP ${response.status}`;
    process.stdout.write(
      JSON.stringify({ id: row.id, url, status: response.status, ms: Date.now() - started, verdict, body: text }, null, 2) + "\n",
    );
    return response.status === 401 || response.status === 403 || response.ok ? 0 : 1;
  } catch (error) {
    process.stdout.write(
      JSON.stringify({ id: row.id, url, ok: false, ms: Date.now() - started, error: error.message }, null, 2) + "\n",
    );
    return 1;
  } finally {
    clearTimeout(timer);
  }
}

function cmdMarkdown(args) {
  const text = toMarkdown();
  if (args.out) {
    const target = resolve(String(args.out));
    mkdirSync(dirname(target), { recursive: true });
    writeFileSync(target, text + "\n", "utf8");
    process.stdout.write(JSON.stringify({ ok: true, file: target, rows: CANDIDATES.length }, null, 2) + "\n");
    return 0;
  }
  process.stdout.write(text + "\n");
  return 0;
}

function cmdDims() {
  process.stdout.write(
    JSON.stringify(
      {
        dimensions: DIMENSIONS,
        weights: WEIGHTS,
        formula: "score = 0.40*automation + 0.35*relay + 0.25*quota - penalty",
        penalty: "热门度：越热越容易被封，号池寿命短",
        lines: CANDIDATES.length,
      },
      null,
      2,
    ) + "\n",
  );
  return 0;
}

const args = parseArgs(process.argv.slice(2));
const command = args._[0];
if (!command || command === "help" || command === "-h") {
  process.stdout.write(USAGE);
  process.exitCode = 0;
} else {
  switch (command) {
    case "list":
      process.exitCode = cmdList(args);
      break;
    case "show":
      process.exitCode = cmdShow(args);
      break;
    case "plan":
      process.exitCode = cmdPlan(args);
      break;
    case "probe":
      process.exitCode = await cmdProbe(args);
      break;
    case "markdown":
      process.exitCode = cmdMarkdown(args);
      break;
    case "dims":
      process.exitCode = cmdDims();
      break;
    default:
      process.stderr.write(`unknown command: ${command}\n\n${USAGE}`);
      process.exitCode = 2;
  }
}
