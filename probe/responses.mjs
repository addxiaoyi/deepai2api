import { loadConfig } from "../src/config.mjs";
import { readStoredAccount } from "../src/credentials.mjs";
import { makeMachineHeader } from "../src/machine.mjs";
import { makeDispatcher } from "../src/dispatcher.mjs";
import { makeLane } from "../src/lane.mjs";

const config = loadConfig([]);
const account = readStoredAccount(config.secretsDir);
const { dispatcher } = makeDispatcher();
const lane = makeLane({ config, account, machineHeader: makeMachineHeader(null), dispatcher });

const probes = [
  { model: "deepseek-v4.1-flash", input: "say OK" },
  { model: "deepseek/deepseek-v4.1-flash-1017", input: "say OK" },
  { model: "gpt-6-luna", input: "say OK" },
  { model: "deepseek-v4.1-flash", input: [{ role: "user", content: "say OK" }] },
];

for (const body of probes) {
  try {
    const res = await lane.call("/responses", Buffer.from(JSON.stringify(body)), { model: body.model });
    const text = await res.text();
    console.log(`OK model=${body.model} input=${typeof body.input} -> ${res.status} ct=${res.headers.get("content-type")}`);
    console.log("  ", text.slice(0, 400).replace(/\n/g, " "));
  } catch (error) {
    console.log(`ERR model=${body.model} input=${typeof body.input} -> ${error.status} ${error.code}`);
    console.log("  ", JSON.stringify(error.body)?.slice(0, 300));
  }
}

// 顶层的 chat/completions 用模型别名是什么？看目录里 id 与上游真实名的对应
const res = await lane.call("/chat/completions", Buffer.from(JSON.stringify({
  model: "deepseek-v4.1-flash", messages: [{ role: "user", content: "hi" }], stream: false,
})), { model: "deepseek-v4.1-flash" });
const j = await res.json();
console.log("\nchat/completions 返回 model 字段:", j.model);