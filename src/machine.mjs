// Machine fingerprint the lane expects in `X-DeepAI-Code-Machine`.
// The app reads a firmware identifier that survives reinstalling the app, then
// sends sha256(raw) — never the raw identifier (src/deepai/machineId.ts).
import { createHash } from "node:crypto";
import { execFile } from "node:child_process";
import { readFileSync } from "node:fs";
import { promisify } from "node:util";

const exec = promisify(execFile);

const UUID = /[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}/i;
const SMBIOS_PLACEHOLDERS = new Set([
  "00000000-0000-0000-0000-000000000000",
  "ffffffff-ffff-ffff-ffff-ffffffffffff",
  "03000200-0400-0500-0006-000700080009",
]);

async function run(file, args) {
  try {
    const { stdout } = await exec(file, args, { encoding: "utf8", timeout: 10_000, windowsHide: true });
    return stdout;
  } catch {
    return "";
  }
}

async function detectRawMachineId() {
  if (process.platform === "win32") {
    const smbios = (await run("powershell", [
      "-NoProfile", "-NonInteractive", "-Command",
      "(Get-CimInstance Win32_ComputerSystemProduct).UUID",
    ])).trim().toLowerCase();
    const match = UUID.exec(smbios)?.[0];
    if (match && !SMBIOS_PLACEHOLDERS.has(match)) return match;

    const reg = await run("reg", ["query", "HKLM\\SOFTWARE\\Microsoft\\Cryptography", "/v", "MachineGuid"]);
    const guid = /MachineGuid\s+REG_SZ\s+(\S+)/.exec(reg)?.[1];
    if (guid && UUID.test(guid)) return guid.toLowerCase();
    return null;
  }
  if (process.platform === "darwin") {
    const ioreg = await run("ioreg", ["-rd1", "-c", "IOPlatformExpertDevice"]);
    const match = /"IOPlatformUUID"\s*=\s*"([^"]+)"/.exec(ioreg)?.[1];
    return match && UUID.test(match) ? match.toLowerCase() : null;
  }
  for (const path of ["/etc/machine-id", "/var/lib/dbus/machine-id"]) {
    try {
      const id = readFileSync(path, "utf8").trim().toLowerCase();
      if (/^[0-9a-f]{32}$/.test(id) && !/^0+$/.test(id)) return id;
    } catch {}
  }
  return null;
}

export function hashMachineId(raw) {
  return createHash("sha256").update(raw).digest("hex");
}

/** Resolve the digest once per process; null when nothing readable is available. */
export function makeMachineHeader(overrideRaw = null) {
  let pending = null;
  return () => {
    if (overrideRaw) return Promise.resolve(hashMachineId(overrideRaw));
    pending ??= detectRawMachineId().then((raw) => (raw === null ? null : hashMachineId(raw)));
    return pending;
  };
}