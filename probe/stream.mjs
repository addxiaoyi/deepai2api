// Verify that undici goes through the local HTTP proxy and that the lane streams SSE.
import { ProxyAgent, fetch as ufetch } from "undici";
import { createHash } from "node:crypto";
import { readFileSync } from "node:fs";
import { homedir } from "node:os";
import { join } from "node:path";

const proxy = process.env.https_proxy || process.env.HTTPS_PROXY || process.env.http_proxy;
const dispatcher = proxy ? new ProxyAgent(proxy) : undefined;

const cred = JSON.parse(
  readFileSync(join(homedir(), ".deepai-code", "userdata", "secrets", "deepai-account.bin"), "utf8"),
);
const machine = "dde678678d920d5a9c9de37c6275259d5c7ad34625c0df730445738b0be2fef2";
const inst = createHash("sha256").update(String(Date.now())).digest("hex").slice(0, 32);

const headers = {
  "Content-Type": "application/json",
  "X-DeepAI-Code-Theos": "7",
  "X-DeepAI-Code-Instance": inst,
  "X-DeepAI-Code-Machine": machine,
  "api-key": cred.apiKey,
};

const res = await ufetch("https://api.deepai.org/deepai_code/chat/completions", {
  method: "POST",
  headers,
  dispatcher,
  body: JSON.stringify({
    model: "deepseek-v4.1-flash",
    messages: [{ role: "user", content: "count 1 to 5" }],
    stream: true,
    stream_options: { include_usage: true },
  }),
});

console.log("status", res.status, "ct", res.headers.get("content-type"));
const reader = res.body.getReader();
const dec = new TextDecoder();
let buf = "";
let n = 0;
for (;;) {
  const { done, value } = await reader.read();
  if (done) break;
  buf += dec.decode(value, { stream: true });
  const lines = buf.split("\n");
  buf = lines.pop() ?? "";
  for (const line of lines) {
    if (!line.startsWith("data:")) continue;
    if (++n <= 6) console.log(line.slice(0, 260));
  }
}
console.log("sse data lines:", n);