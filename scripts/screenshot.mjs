// Render the running admin UI to a PNG with headless Chrome over CDP.
// Used to verify the page actually draws (not just that the HTML is served).
//
//   node scripts/screenshot.mjs [--url http://127.0.0.1:8787] [--out shot.png]
//                              [--token <admin token>] [--size 1440x1800]
import { spawn } from "node:child_process";
import { mkdirSync, writeFileSync, existsSync } from "node:fs";
import { dirname, join } from "node:path";
import { tmpdir } from "node:os";

const args = process.argv.slice(2);
const flag = (name, fallback) => {
  const i = args.indexOf(`--${name}`);
  return i === -1 ? fallback : (args[i + 1] ?? fallback);
};

const url = flag("url", "http://127.0.0.1:8787").replace(/\/+$/, "");
const out = flag("out", join(process.cwd(), "data", "admin.png"));
const token = flag("token", "");
const [width, height] = flag("size", "1440x1800").split("x").map(Number);
const port = Number(flag("debug-port", "9333"));

const CHROME_CANDIDATES = [
  `${process.env.PROGRAMFILES}\\Google\\Chrome\\Application\\chrome.exe`,
  `${process.env["PROGRAMFILES(X86)"]}\\Microsoft\\Edge\\Application\\msedge.exe`,
  `${process.env.PROGRAMFILES}\\Microsoft\\Edge\\Application\\msedge.exe`,
  `${process.env.LOCALAPPDATA}\\Google\\Chrome\\Application\\chrome.exe`,
];
const chrome = CHROME_CANDIDATES.find((p) => p && existsSync(p));
if (!chrome) {
  console.error("No Chrome/Edge found.");
  process.exit(1);
}

// A throwaway profile keeps the operator's own browser session untouched.
const profile = join(tmpdir(), `dcg-shot-${Date.now()}`);
const child = spawn(chrome, [
  "--headless=new",
  "--disable-gpu",
  "--hide-scrollbars",
  "--no-first-run",
  "--no-default-browser-check",
  `--user-data-dir=${profile}`,
  `--remote-debugging-port=${port}`,
  "about:blank",
], { stdio: "ignore" });

const sleep = (ms) => new Promise((r) => setTimeout(r, ms));

async function waitForDevtools() {
  for (let i = 0; i < 60; i++) {
    try {
      const res = await fetch(`http://127.0.0.1:${port}/json/version`);
      if (res.ok) return await res.json();
    } catch {}
    await sleep(250);
  }
  throw new Error("Chrome devtools did not come up.");
}

class Cdp {
  constructor(ws) {
    this.ws = ws;
    this.id = 0;
    this.pending = new Map();
    ws.addEventListener("message", (event) => {
      const message = JSON.parse(event.data);
      if (message.id && this.pending.has(message.id)) {
        const { resolve, reject } = this.pending.get(message.id);
        this.pending.delete(message.id);
        message.error ? reject(new Error(message.error.message)) : resolve(message.result);
      }
    });
  }
  send(method, params = {}, sessionId) {
    const id = ++this.id;
    return new Promise((resolve, reject) => {
      this.pending.set(id, { resolve, reject });
      this.ws.send(JSON.stringify({ id, method, params, ...(sessionId ? { sessionId } : {}) }));
    });
  }
}

const version = await waitForDevtools();
const ws = new WebSocket(version.webSocketDebuggerUrl);
await new Promise((resolve, reject) => {
  ws.addEventListener("open", resolve, { once: true });
  ws.addEventListener("error", reject, { once: true });
});
const cdp = new Cdp(ws);

const { targetId } = await cdp.send("Target.createTarget", { url: "about:blank" });
const { sessionId } = await cdp.send("Target.attachToTarget", { targetId, flatten: true });

await cdp.send("Page.enable", {}, sessionId);
await cdp.send("Network.enable", {}, sessionId);
await cdp.send("Emulation.setDeviceMetricsOverride",
  { width, height, deviceScaleFactor: 1, mobile: false }, sessionId);

// Log in first when a token is given, so the shot shows the real dashboard.
if (token) {
  const res = await fetch(`${url}/api/admin/login`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ token }),
  });
  if (!res.ok) {
    console.error(`admin login failed: HTTP ${res.status}`);
    process.exit(1);
  }
  const setCookie = res.headers.get("set-cookie") ?? "";
  const value = setCookie.split(";")[0].split("=").slice(1).join("=");
  // `url` is what the browser actually trusts here; domain-only cookies from a
  // loopback origin get dropped, which silently produces a logged-out shot.
  const { origin } = new URL(url);
  const applied = await cdp.send("Network.setCookie", {
    name: "dcg_admin",
    value,
    url: `${origin}/`,
    path: "/",
    httpOnly: true,
    sameSite: "Lax",
  }, sessionId);
  if (!applied?.success) console.error("cookie not applied:", JSON.stringify(applied));
}

if (process.env.OWUI_TOKEN) {
  // Open WebUI keeps the session JWT in localStorage; seed it before the app boots.
  await cdp.send("Page.navigate", { url: `${url}/` }, sessionId);
  await sleep(1200);
  await cdp.send("Runtime.evaluate", {
    expression: `localStorage.setItem('token', ${JSON.stringify(process.env.OWUI_TOKEN)})`,
  }, sessionId);
}
const target = process.env.SHOT_URL ? `${url}${process.env.SHOT_URL}` : `${url}/`;
await cdp.send("Page.navigate", { url: target }, sessionId);
await sleep(5000);
if (process.env.SHOT_EVAL) {
  // Drive client-side routing (settings panels are modal state, not routes).
  const evalResult = await cdp.send("Runtime.evaluate", { expression: process.env.SHOT_EVAL, awaitPromise: true, returnByValue: true }, sessionId);
  console.log("eval:", JSON.stringify(evalResult.result?.value ?? evalResult.result?.description ?? null));
  await sleep(3500);
}

const { data } = await cdp.send("Page.captureScreenshot",
  { format: "png", captureBeyondViewport: true }, sessionId);

mkdirSync(dirname(out), { recursive: true });
writeFileSync(out, Buffer.from(data, "base64"));
console.log(`screenshot: ${out} (${width}x${height})`);

// Text snapshot: proves the page rendered real data, not just an empty shell.
const { result } = await cdp.send("Runtime.evaluate", {
  expression: `JSON.stringify({
    title: document.title,
    url: location.href,
    nodes: document.querySelectorAll('*').length,
    modelOptions: document.querySelectorAll('[role="option"], [data-model-id]').length,
    text: (document.body.innerText || "").replace(/\s+/g, " ").slice(0, 900)
  })`,
  returnByValue: true,
}, sessionId);
console.log("\n--- rendered DOM ---");
try {
  console.log(JSON.stringify(JSON.parse(result.value), null, 2));
} catch {
  console.log(result.value);
}

ws.close();
child.kill();
process.exit(0);




