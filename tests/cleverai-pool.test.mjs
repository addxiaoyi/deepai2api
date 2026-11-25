// Clever AI Hub pool protocol tests: schema, dedupe, health, selection, export,
// key binding, wallet bookkeeping and the relay's message/tool translation.
import { test } from "node:test";
import assert from "node:assert/strict";
import { mkdtempSync, readFileSync, existsSync } from "node:fs";
import { tmpdir } from "node:os";
import { join } from "node:path";

import { CleveraiPool, POOL_SCHEMA, importAccount, maskAccount } from "../src/cleverai-pool.mjs";
import {
  CleveraiRelay,
  detectPlaceholder,
  normalizeDelta,
  resolveEngine,
  toDialogMessages,
  toUpstreamTools,
} from "../src/cleverai-relay.mjs";

const sample = (overrides = {}) => ({
  email: "pool@example.com",
  password: "pw-123456",
  name: "Pool",
  uid: "uid-abc123",
  idToken: "id-token-1",
  refreshToken: "refresh-token-abcdef0123456789",
  appCheck: "app-check-1",
  appCheckExp: 0,
  wallet: { totalCredit: 75, renewableCredit: 30 },
  emailVerified: true,
  engine: "protocol",
  ...overrides,
});

function fakeStore() {
  const keys = [];
  return {
    keys,
    addKey({ label, hash, prefix, limits }) {
      const key = { id: `k-${keys.length + 1}`, label, hash, prefix, limits, createdAt: new Date().toISOString() };
      keys.push(key);
      return key;
    },
  };
}

test("pool writes dcg.pool/cleverai@1 and keeps credentials on disk", () => {
  const dir = mkdtempSync(join(tmpdir(), "cv-pool-"));
  const path = join(dir, "cleverai-pool.json");
  const pool = new CleveraiPool(path);
  pool.add(sample());
  assert.equal(pool.size, 1);
  assert.ok(existsSync(path));
  const raw = JSON.parse(readFileSync(path, "utf8"));
  assert.equal(raw.schema, POOL_SCHEMA);
  assert.equal(raw.provider, "cleverai");
  assert.equal(raw.accounts[0].refreshToken, "refresh-token-abcdef0123456789");
  assert.equal(raw.accounts[0].status, "active");
  assert.equal(raw.accounts[0].wallet.totalCredit, 75);
});

test("importAccount mints exactly one gateway key, idempotently", () => {
  const dir = mkdtempSync(join(tmpdir(), "cv-pool-"));
  const pool = new CleveraiPool(join(dir, "pool.json"));
  const store = fakeStore();

  const first = importAccount({ pool, store, record: sample() });
  assert.equal(first.created, true);
  assert.ok(first.gatewayKey.plaintext.startsWith("sk-dcg-"));
  assert.equal(store.keys.length, 1);

  const again = importAccount({ pool, store, record: sample({ idToken: "id-token-2" }) });
  assert.equal(again.created, false);
  assert.equal(store.keys.length, 1, "re-import must not mint a second key");
  assert.equal(again.account.id, first.account.id);
  assert.equal(again.account.idToken, "id-token-2", "credentials refresh on re-import");
});

test("dedupe falls back to email when the refresh token is missing", () => {
  const dir = mkdtempSync(join(tmpdir(), "cv-pool-"));
  const pool = new CleveraiPool(join(dir, "pool.json"));
  pool.add(sample());
  const second = pool.add(sample({ refreshToken: "", uid: "uid-other" }));
  assert.equal(second.created, false, "same email must merge, not duplicate");
  assert.equal(pool.size, 1);
});

test("pick only returns healthy, non-cooling accounts and rotates", () => {
  const dir = mkdtempSync(join(tmpdir(), "cv-pool-"));
  const pool = new CleveraiPool(join(dir, "pool.json"));
  const a = pool.add(sample({ email: "a@example.com", refreshToken: "r-a" })).account;
  const b = pool.add(sample({ email: "b@example.com", refreshToken: "r-b" })).account;
  pool.add(sample({ email: "c@example.com", refreshToken: "r-c" }));

  const first = pool.pick();
  const second = pool.pick();
  assert.notEqual(first.id, second.id);

  pool.recordResult(a.id, { ok: false, error: "rate_limited", cooldownMs: 60_000 });
  assert.equal(pool.healthy().length, 2);
  const excluded = pool.pick({ exclude: [b.id] });
  assert.ok(excluded && excluded.id !== b.id && excluded.id !== a.id);

  pool.recordResult(a.id, { ok: true });
  assert.equal(pool.healthy().length, 3, "a success clears the cooldown");
});

test("stats aggregate credits, verification and engines", () => {
  const dir = mkdtempSync(join(tmpdir(), "cv-pool-"));
  const pool = new CleveraiPool(join(dir, "pool.json"));
  pool.add(sample({ email: "a@example.com", refreshToken: "r-a", wallet: { totalCredit: 75 } }));
  pool.add(sample({ email: "b@example.com", refreshToken: "r-b", engine: "browser", wallet: { totalCredit: 30 } }));
  const stats = pool.stats();
  assert.equal(stats.total, 2);
  assert.equal(stats.credits, 105);
  assert.equal(stats.verified, 2);
  assert.equal(stats.engines.protocol, 1);
  assert.equal(stats.engines.browser, 1);
});

test("recordWallet merges the upstream wallet snapshot", () => {
  const dir = mkdtempSync(join(tmpdir(), "cv-pool-"));
  const pool = new CleveraiPool(join(dir, "pool.json"));
  const account = pool.add(sample()).account;
  pool.recordWallet(account.id, { totalCredit: 12, renewableCredit: 0 });
  const stored = pool.getAccount(account.id);
  assert.equal(stored.wallet.totalCredit, 12);
  assert.equal(stored.wallet.renewableCredit, 0);
});

test("export shapes: jsonl, csv and a full pool snapshot", () => {
  const dir = mkdtempSync(join(tmpdir(), "cv-pool-"));
  const pool = new CleveraiPool(join(dir, "pool.json"));
  pool.add(sample());

  const jsonl = pool.exportTo(join(dir, "out.jsonl"), { format: "jsonl" });
  assert.equal(jsonl.rows, 1);
  const line = readFileSync(jsonl.file, "utf8").trim();
  assert.equal(JSON.parse(line).email, "pool@example.com");

  const csv = pool.exportTo(join(dir, "out.csv"), { format: "csv" });
  const head = readFileSync(csv.file, "utf8").split("\n")[0];
  assert.ok(head.includes("refresh_token"), "csv must carry the refresh token column");

  const snapshot = pool.exportTo(join(dir, "out.json"), { format: "pool" });
  const parsed = JSON.parse(readFileSync(snapshot.file, "utf8"));
  assert.equal(parsed.schema, POOL_SCHEMA);
});

test("maskAccount never leaks tokens", () => {
  const dir = mkdtempSync(join(tmpdir(), "cv-pool-"));
  const pool = new CleveraiPool(join(dir, "pool.json"));
  pool.add(sample());
  const masked = maskAccount(pool.accounts[0]);
  const blob = JSON.stringify(masked);
  assert.ok(!blob.includes("refresh-token-abcdef0123456789"));
  assert.ok(masked.refreshTokenPreview.includes("…"));
});

/* --------------------------------------------------------------- relay bits */

test("OpenAI messages become the app's dialog envelope", () => {
  const dialog = toDialogMessages([
    { role: "system", content: "be terse" },
    { role: "user", content: "hi" },
    {
      role: "assistant",
      content: null,
      tool_calls: [{ id: "call-1", type: "function", function: { name: "read_file", arguments: '{"path":"a"}' } }],
    },
    { role: "tool", tool_call_id: "call-1", content: "file body" },
  ]);
  assert.equal(dialog.length, 4);
  assert.equal(dialog[0].type, "human");
  assert.equal(dialog[0].contents[0].type, "text");
  assert.equal(dialog[2].type, "ai");
  const call = dialog[2].contents.find((item) => item.type === "tool_call");
  assert.equal(call.name, "read_file");
  assert.deepEqual(call.arguments, { path: "a" });
  const result = dialog[3].contents[0];
  assert.equal(result.type, "tool_result");
  assert.equal(result.tool_call_id, "call-1");
});

test("OpenAI tools flatten to the upstream function descriptors", () => {
  const tools = toUpstreamTools([
    { type: "function", function: { name: "run", description: "run a shell command", parameters: { type: "object" } } },
    { name: "noop" },
    { type: "function", function: {} },
  ]);
  assert.equal(tools.length, 2);
  assert.equal(tools[0].name, "run");
  assert.equal(tools[0].description, "run a shell command");
  assert.equal(tools[1].name, "noop");
});

test("SSE deltas split text from tool calls", () => {
  const text = normalizeDelta({ delta: { content: [{ type: "text", text: "he" }, { type: "text", text: "y" }] } });
  assert.equal(text.text, "hey");
  assert.equal(text.finish, null);

  const call = normalizeDelta({
    delta: { content: [{ type: "tool_call", id: "c1", name: "read_file", arguments: { path: "a" } }] },
  });
  assert.equal(call.toolCalls.length, 1);
  assert.equal(call.toolCalls[0].arguments, '{"path":"a"}');

  const done = normalizeDelta({ finishReason: "stop", delta: { content: [{ type: "tool_call", id: "c1", name: "x" }] } });
  assert.equal(done.finish, "tool_calls", "a tool call at stop means finish_reason=tool_calls");
});

test("model aliases map onto real engine ids", () => {
  assert.equal(resolveEngine("gpt-4o"), "openai-gpt-6-luna");
  assert.equal(resolveEngine("cleverai/default"), "openai-gpt-6-luna");
  assert.equal(resolveEngine("google-gemini-4-neon"), "google-gemini-4-neon");
  assert.equal(resolveEngine(""), "openai-gpt-6-luna");
});
test("streamOpenAI emits OpenAI SSE chunks including tool_calls", async () => {
  const dir = mkdtempSync(join(tmpdir(), "cv-pool-"));
  const pool = new CleveraiPool(join(dir, "pool.json"));
  const relay = new CleveraiRelay({ pool });

  // 用一条假的上游事件流替换真实 chat（不联网）
  relay.chat = async function* () {
    yield { type: "text", text: "Let me check.", engine: "openai-gpt-6-luna" };
    yield {
      type: "tool_call",
      call: { id: "call-7", name: "read_file", arguments: '{"path":"README.md"}' },
      engine: "openai-gpt-6-luna",
    };
    yield { type: "done", finish: "tool_calls", engine: "openai-gpt-6-luna" };
  };

  const chunks = [];
  for await (const chunk of relay.streamOpenAI({
    messages: [{ role: "user", content: "read the readme" }],
    model: "gpt-4o",
  })) {
    chunks.push(chunk);
  }
  const frames = chunks
    .filter((c) => c.startsWith("data: ") && !c.includes("[DONE]"))
    .map((c) => JSON.parse(c.slice(6)));
  assert.ok(frames.length >= 4);
  assert.equal(frames[0].object, "chat.completion.chunk");
  assert.equal(frames[0].choices[0].delta.role, "assistant");

  const textFrame = frames.find((f) => f.choices[0].delta.content === "Let me check.");
  assert.ok(textFrame, "text delta must be forwarded");

  const nameFrame = frames.find((f) => f.choices[0].delta.tool_calls?.[0]?.function?.name === "read_file");
  assert.ok(nameFrame, "tool name must arrive before arguments");
  const argsFrame = frames.find((f) => f.choices[0].delta.tool_calls?.[0]?.function?.arguments);
  assert.equal(argsFrame.choices[0].delta.tool_calls[0].index, 0);
  assert.equal(argsFrame.choices[0].delta.tool_calls[0].function.arguments, '{"path":"README.md"}');

  const last = frames.at(-1);
  assert.equal(last.choices[0].finish_reason, "tool_calls");
  assert.equal(chunks.at(-1), "data: [DONE]\n\n");
});

test("complete() folds the stream into one assistant message with tool_calls", async () => {
  const dir = mkdtempSync(join(tmpdir(), "cv-pool-"));
  const pool = new CleveraiPool(join(dir, "pool.json"));
  const relay = new CleveraiRelay({ pool });
  relay.chat = async function* () {
    yield { type: "text", text: "hi", engine: "openai-gpt-6-luna" };
    yield { type: "tool_call", call: { id: "c1", name: "shell", arguments: "{}" }, engine: "openai-gpt-6-luna" };
    yield { type: "done", finish: "tool_calls", engine: "openai-gpt-6-luna" };
  };
  const out = await relay.complete({ messages: [{ role: "user", content: "x" }], model: "cleverai/default" });
  assert.equal(out.object, "chat.completion");
  assert.equal(out.choices[0].message.content, "hi");
  assert.equal(out.choices[0].message.tool_calls[0].function.name, "shell");
  assert.equal(out.choices[0].finish_reason, "tool_calls");
  assert.equal(out.model, "openai-gpt-6-luna");
});
test("placeholder replies from the upstream are recognised", () => {
  assert.equal(
    detectPlaceholder("Please update to the latest version. This update includes essential bug fixes"),
    "update_required",
  );
  assert.equal(
    detectPlaceholder("We apologize for the inconvenience. Our service is currently experiencing heavy traffic"),
    "heavy_traffic",
  );
  assert.equal(detectPlaceholder("**Clever AI** is _analyzing_ your request...\n"), "analyzing");
  assert.equal(detectPlaceholder("pong"), "");
  assert.equal(detectPlaceholder(""), "");
});

test("complete() carries the placeholder code and the pool counts it", async () => {
  const dir = mkdtempSync(join(tmpdir(), "cv-pool-"));
  const pool = new CleveraiPool(join(dir, "pool.json"));
  const account = pool.add(sample()).account;
  const relay = new CleveraiRelay({ pool });

  relay.chat = async function* () {
    yield { type: "text", text: "**Clever AI** is _analyzing_ your request...\nPlease update to the latest version." };
    yield { type: "notice", code: "update_required" };
    yield { type: "done", finish: "stop", engine: "openai-gpt-6-luna", notice: "update_required" };
  };

  const out = await relay.complete({ messages: [{ role: "user", content: "hi" }], model: "cleverai/default" });
  assert.equal(out["x_cleverai_notice"], "update_required");

  pool.recordNotice(account.id, "update_required");
  const stats = pool.stats();
  assert.equal(stats.placeholderReplies, 1);
  assert.equal(pool.getAccount(account.id).notices.byCode.update_required, 1);
});

test("streamOpenAI emits an SSE comment when the reply is a placeholder", async () => {
  const dir = mkdtempSync(join(tmpdir(), "cv-pool-"));
  const pool = new CleveraiPool(join(dir, "pool.json"));
  const relay = new CleveraiRelay({ pool });
  relay.chat = async function* () {
    yield { type: "text", text: "Service is currently experiencing heavy traffic" };
    yield { type: "notice", code: "heavy_traffic" };
    yield { type: "done", finish: "stop", engine: "antropic-calude-sonnet-5-5", notice: "heavy_traffic" };
  };
  const chunks = [];
  for await (const chunk of relay.streamOpenAI({ messages: [{ role: "user", content: "hi" }], model: "" })) {
    chunks.push(chunk);
  }
  assert.ok(chunks.some((c) => c.includes("cleverai-notice=heavy_traffic")), "notice must be visible in the stream");
  assert.equal(chunks.at(-1), "data: [DONE]\n\n");
});
test("costFor / userType follow the upstream catalog", () => {
  const dir = mkdtempSync(join(tmpdir(), "cv-pool-"));
  const pool = new CleveraiPool(join(dir, "pool.json"));
  const relay = new CleveraiRelay({ pool });
  assert.equal(relay.costFor("openai-gpt-6-luna"), 2);
  assert.equal(relay.costFor("cleverai/openai-gpt-6-luna"), 2);
  assert.equal(relay.costFor("google-gemini-4-argon"), 30);
  assert.equal(relay.costFor("totally-unknown-engine"), 2);
  assert.equal(relay.userType({}), "FREE");
  assert.equal(relay.userType({ subscription: "PRO" }), "PRO");
  assert.equal(relay.userType({ userType: "PRO_TRIAL" }), "PRO_TRIAL");
  assert.equal(relay.userType({ subscription: "bogus" }), "FREE");
});
