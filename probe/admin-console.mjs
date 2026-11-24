// 抓页面的真实 console / 异常，定位为何脚本没跑起来。
import { spawn } from "node:child_process";
import { existsSync } from "node:fs";
import { tmpdir } from "node:os";
import { join } from "node:path";

const port = 9444;
const chrome = [
  `${process.env.PROGRAMFILES}\\Google\\Chrome\\Application\\chrome.exe`,
  `${process.env["PROGRAMFILES(X86)"]}\\Microsoft\\Edge\\Application\\msedge.exe`,
].find((p) => p && existsSync(p));

const child = spawn(chrome, ["--headless=new", "--disable-gpu", `--user-data-dir=${join(tmpdir(), "dcg-console-" + Date.now())}`, `--remote-debugging-port=${port}`, "about:blank"], { stdio: "ignore" });
const sleep = (ms) => new Promise((r) => setTimeout(r, ms));

let version;
for (let i = 0; i < 60; i++) {
  try {
    const res = await fetch(`http://127.0.0.1:${port}/json/version`);
    if (res.ok) { version = await res.json(); break; }
  } catch {}
  await sleep(250);
}

const ws = new WebSocket(version.webSocketDebuggerUrl);
await new Promise((r) => ws.addEventListener("open", r, { once: true }));
let id = 0;
const pending = new Map();
const logs = [];
ws.addEventListener("message", (event) => {
  const msg = JSON.parse(event.data);
  if (msg.id && pending.has(msg.id)) {
    const { resolve, reject } = pending.get(msg.id);
    pending.delete(msg.id);
    msg.error ? reject(new Error(msg.error.message)) : resolve(msg.result);
    return;
  }
  if (msg.method === "Runtime.exceptionThrown") {
    logs.push("EXCEPTION: " + (msg.params.exceptionDetails?.exception?.description ?? JSON.stringify(msg.params.exceptionDetails).slice(0, 400)));
  }
  if (msg.method === "Runtime.consoleAPICalled" && msg.params.type === "error") {
    logs.push("console.error: " + msg.params.args.map((a) => a.description ?? a.value).join(" "));
  }
  if (msg.method === "Log.entryAdded" && msg.params.entry.level === "error") {
    logs.push("LOG: " + msg.params.entry.text);
  }
});
const send = (method, params = {}, sessionId) => {
  const n = ++id;
  return new Promise((resolve, reject) => {
    pending.set(n, { resolve, reject });
    ws.send(JSON.stringify({ id: n, method, params, ...(sessionId ? { sessionId } : {}) }));
  });
};

const { targetId } = await send("Target.createTarget", { url: "about:blank" });
const { sessionId } = await send("Target.attachToTarget", { targetId, flatten: true });
await send("Runtime.enable", {}, sessionId);
await send("Log.enable", {}, sessionId);
await send("Page.enable", {}, sessionId);
await send("Page.navigate", { url: "http://127.0.0.1:8787/" }, sessionId);
await sleep(3000);

const { result } = await send("Runtime.evaluate", {
  expression: `JSON.stringify({ hasReload: typeof reload, hasRender: typeof render, gateHidden: document.getElementById('gate').hidden, appHidden: document.getElementById('app').hidden })`,
  returnByValue: true,
}, sessionId);
console.log("probe:", result.value);
console.log("logs:");
for (const line of logs) console.log("  " + line);

ws.close();
child.kill();
process.exit(0);