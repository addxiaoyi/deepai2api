// 针对性调试：坏账号在前时，调度器是否真的先试它。
import { mkdtempSync } from "node:fs";
import { tmpdir } from "node:os";
import { join } from "node:path";
import { loadConfig } from "../src/config.mjs";
import { makeMachineHeader } from "../src/machine.mjs";
import { makeDispatcher } from "../src/dispatcher.mjs";
import { makeLane } from "../src/lane.mjs";
import { makeScheduler } from "../src/scheduler.mjs";
import { Store } from "../src/store.mjs";
import { startMockUpstream } from "../tests/mock-upstream.mjs";

const mock = await startMockUpstream();
const config = { ...loadConfig([]), dataDir: mkdtempSync(join(tmpdir(), "dbg-")), apiOrigin: mock.origin, log: "warn" };
const store = Store.at(config.dataDir);
const { dispatcher } = makeDispatcher();
const lane = makeLane({ config, getAccount: () => store.account, machineHeader: makeMachineHeader("11111111-2222-3333-4444-555555555555"), dispatcher });
const scheduler = makeScheduler({ store });

store.upsertAccount({ apiKey: "mock-account-api-key", username: "good" });
store.upsertAccount({ apiKey: "bad-key", username: "broken" });
const broken = store.accounts.find((a) => a.apiKey === "bad-key");
store.state.accounts = [broken, ...store.state.accounts.filter((a) => a.id !== broken.id)];
store.save();

console.log("accounts order:", store.accounts.map((a) => `${a.username}(healthy=${a.healthy})`));
console.log("candidates:", scheduler.candidates({ keyId: null, model: "deepai-cyber" }).map((a) => a.username));

try {
  const res = await lane.call("/chat/completions", Buffer.from(JSON.stringify({ model: "deepai-cyber", messages: [{ role: "user", content: "hi" }] })), {
    account: broken,
    model: "deepai-cyber",
  });
  console.log("lane.call on broken -> UNEXPECTED success", res.status);
} catch (e) {
  console.log("lane.call on broken -> error", e.status, e.code, e.message);
}

scheduler.record({ accountId: broken.id, ok: false, error: { status: 401, code: "auth_required", message: "revoked" } });
console.log("after record: broken.healthy =", broken.healthy, "cooldownUntil in", Math.round((broken.cooldownUntil - Date.now()) / 1000), "s");
console.log("final order:", store.accounts.map((a) => `${a.username}(healthy=${a.healthy})`));

await mock.close();
process.exit(0);