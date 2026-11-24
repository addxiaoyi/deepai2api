// Live probe of the DeepAI Code managed model lane.
// Reconstructed from apps/server/dist/binCli-*.mjs (src/deepai/api.ts,
// src/deepai/localTransport.ts, src/deepai/DeepAIAccount.ts).
import { createHash, randomBytes } from "node:crypto";
import { execFile } from "node:child_process";
import { readFileSync } from "node:fs";
import { promisify } from "node:util";
import { homedir } from "node:os";
import { join } from "node:path";

const exec = promisify(execFile);
const API = "https://api.deepai.org";
const BASE = `${API}/deepai_code`;
const LANE_VERSION = "7";

function secret(name) {
  return readFileSync(join(homedir(), ".deepai-code", "userdata", "secrets", `${name}.bin`), "utf8");
}

function credentials() {
  return JSON.parse(secret("deepai-account"));
}

async function machineId() {
  // Windows: SMBIOS UUID lowercased, sha256 hex. Falls back to MachineGuid.
  const uuidRe = /[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}/i;
  const placeholders = new Set([
    "00000000-0000-0000-0000-000000000000",
    "ffffffff-ffff-ffff-ffff-ffffffffffff",
    "03000200-0400-0500-0006-000700080009",
  ]);
  const attempt = async (file, args) => {
    try {
      const { stdout } = await exec(file, args, { encoding: "utf8", timeout: 10000, windowsHide: true });
      return stdout;
    } catch {
      return "";
    }
  };
  let raw = null;
  if (process.platform === "win32") {
    const smbios = (await attempt("powershell", [
      "-NoProfile", "-NonInteractive", "-Command",
      "(Get-CimInstance Win32_ComputerSystemProduct).UUID",
    ])).trim().toLowerCase();
    const u = uuidRe.exec(smbios)?.[0];
    if (u && !placeholders.has(u)) raw = u;
    if (!raw) {
      const reg = await attempt("reg", ["query", "HKLM\\SOFTWARE\\Microsoft\\Cryptography", "/v", "MachineGuid"]);
      const m = /MachineGuid\s+REG_SZ\s+(\S+)/.exec(reg)?.[1];
      if (m && uuidRe.test(m)) raw = m.toLowerCase();
    }
  } else if (process.platform === "darwin") {
    const ioreg = await attempt("ioreg", ["-rd1", "-c", "IOPlatformExpertDevice"]);
    const m = /"IOPlatformUUID"\s*=\s*"([^"]+)"/.exec(ioreg)?.[1];
    if (m && uuidRe.test(m)) raw = m.toLowerCase();
  } else {
    for (const p of ["/etc/machine-id", "/var/lib/dbus/machine-id"]) {
      try {
        const id = readFileSync(p, "utf8").trim().toLowerCase();
        if (/^[0-9a-f]{32}$/.test(id) && !/^0+$/.test(id)) { raw = id; break; }
      } catch {}
    }
  }
  return raw === null ? null : createHash("sha256").update(raw).digest("hex");
}

const instance = randomBytes(16).toString("hex");
let machine = null;
let checkIn = null;

function laneHeaders() {
  return {
    "Content-Type": "application/json",
    "X-DeepAI-Code-Theos": LANE_VERSION,
    "X-DeepAI-Code-Instance": instance,
    ...(machine ? { "X-DeepAI-Code-Machine": machine } : {}),
    "api-key": credentials().apiKey,
  };
}

function beacon({ pi = true, focus = true } = {}) {
  return [`v=0.3.50`, `up=1`, `pi=${pi ? 1 : 0}`, `focus=${focus ? 1 : 0}`].join(";");
}

async function theos() {
  const headers = { ...laneHeaders(), "X-DeepAI-Code-Beacon": beacon() };
  if (checkIn) headers["X-DeepAI-Code-Check-In"] = checkIn;
  const res = await fetch(`${BASE}/theos`, {
    method: "POST",
    headers,
    body: JSON.stringify({}),
    redirect: "error",
  });
  const text = await res.text();
  let body;
  try { body = JSON.parse(text); } catch { body = text; }
  if (body && typeof body === "object" && body.check_in && typeof body.check_in.nonce === "string") {
    checkIn = body.check_in.nonce;
  }
  return { status: res.status, body };
}

async function main() {
  machine = await machineId();
  console.log("machine header:", machine ?? "(none)");
  console.log("account:", (() => { const c = credentials(); return `${c.username} pro=${c.isPro}`; })());

  const t = await theos();
  console.log("\n=== POST /deepai_code/theos ===", t.status);
  console.log(JSON.stringify(t.body, null, 2).slice(0, 1200));

  const modelsRes = await fetch(`${BASE}/models`, { headers: laneHeaders(), redirect: "error" });
  const modelsText = await modelsRes.text();
  console.log("\n=== GET/POST /deepai_code/models ===", modelsRes.status);
  console.log(modelsText.slice(0, 4000));
}

await main();