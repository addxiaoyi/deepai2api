import { loadConfig } from "../src/config.mjs";
import { readStoredAccount } from "../src/credentials.mjs";
import { makeMachineHeader } from "../src/machine.mjs";
import { makeDispatcher } from "../src/dispatcher.mjs";
import { makeLane } from "../src/lane.mjs";

const config = { ...loadConfig([]), log: "debug" };
const account = readStoredAccount(config.secretsDir);
const { dispatcher, mode } = makeDispatcher();
const lane = makeLane({
  config,
  account,
  machineHeader: makeMachineHeader(config.machineRaw),
  dispatcher,
  log: (l, m) => console.log(`[${l}] ${m}`),
});
console.log("network:", mode);

console.log("\n--- catalog ---");
try {
  const c = await lane.getCatalog({ force: true });
  console.log("ok", c.length);
} catch (e) {
  console.log("ERR", e.name, e.message, "status=", e.status, "code=", e.code);
  console.log(JSON.stringify(e.body, null, 2));
}

console.log("\n--- allowance ---");
try {
  console.log(await lane.getAllowance());
} catch (e) {
  console.log("ERR", e.message, e.status, JSON.stringify(e.body));
}

console.log("\n--- chat without stream flag ---");
try {
  const res = await lane.call("/chat/completions", Buffer.from(JSON.stringify({
    model: "deepseek-v4.1-flash",
    messages: [{ role: "user", content: "hi" }],
  })), { model: "deepseek-v4.1-flash" });
  console.log("status", res.status, "ct", res.headers.get("content-type"));
  const text = await res.text();
  console.log(text.slice(0, 300));
} catch (e) {
  console.log("ERR", e.message, e.status, JSON.stringify(e.body));
}

console.log("\n--- chat with stream:false ---");
{
  const res = await lane.call("/chat/completions", Buffer.from(JSON.stringify({
    model: "deepseek-v4.1-flash",
    messages: [{ role: "user", content: "hi" }],
    stream: false,
  })), { model: "deepseek-v4.1-flash" });
  console.log("status", res.status, "ct", res.headers.get("content-type"));
  const text = await res.text();
  console.log(text.slice(0, 200));
}