// 直接打印网关返回体，定位 502 的真实原因。
import { loadConfig } from "../src/config.mjs";
import { readStoredAccount } from "../src/credentials.mjs";
import { makeMachineHeader } from "../src/machine.mjs";
import { makeDispatcher } from "../src/dispatcher.mjs";
import { makeLane } from "../src/lane.mjs";
import { createGateway } from "../src/server.mjs";

const config = { ...loadConfig([]), port: 0, token: "t", log: "debug" };
const account = readStoredAccount(config.secretsDir);
const { dispatcher } = makeDispatcher();
const lane = makeLane({
  config,
  account,
  machineHeader: makeMachineHeader(config.machineRaw),
  dispatcher,
  log: (l, m) => console.log(`[lanelog ${l}] ${m}`),
});
const { server } = createGateway({ config, lane, account, log: (l, m) => console.log(`[gw ${l}] ${m}`) });
await new Promise((r) => server.listen(0, "127.0.0.1", r));
const base = `http://127.0.0.1:${server.address().port}`;
const auth = { Authorization: "Bearer t" };

for (const [label, url, init] of [
  ["GET /v1/models", `${base}/v1/models`, { headers: auth }],
  ["GET /v1/usage", `${base}/v1/usage`, { headers: auth }],
  ["POST /v1/completions", `${base}/v1/completions`, {
    method: "POST",
    headers: { ...auth, "Content-Type": "application/json" },
    body: JSON.stringify({ model: "deepseek-v4.1-flash", prompt: "Say OK" }),
  }],
]) {
  const res = await fetch(url, init);
  const text = await res.text();
  console.log(`\n=== ${label} -> ${res.status} ${res.headers.get("content-type")}\n${text.slice(0, 600)}`);
}
server.close();
process.exit(0);