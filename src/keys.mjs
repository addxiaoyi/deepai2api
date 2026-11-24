// Gateway API keys.
//
// Downstream tools (OpenWebUI, Cline, any OpenAI SDK) authenticate with a key
// minted here. Only the SHA-256 of a key is stored: leaking the state file does
// not hand out working credentials, and the plaintext is shown exactly once.
import { createHash, randomBytes } from "node:crypto";

export const KEY_PREFIX = "sk-dcg-";

export const hashKey = (plaintext) => createHash("sha256").update(plaintext).digest("hex");

export const displayPrefix = (plaintext) => plaintext.slice(0, KEY_PREFIX.length + 8);

export function mintKey() {
  const plaintext = `${KEY_PREFIX}${randomBytes(32).toString("base64url")}`;
  return { plaintext, hash: hashKey(plaintext), prefix: displayPrefix(plaintext) };
}

/** Pull the bearer out of `Authorization: Bearer ...`, `x-api-key`, or `?key=`. */
export function extractPresentedKey(req, url) {
  const header = req.headers.authorization ?? "";
  if (header.startsWith("Bearer ")) return header.slice(7).trim();
  const apiKey = req.headers["x-api-key"];
  if (typeof apiKey === "string" && apiKey.trim()) return apiKey.trim();
  const query = url?.searchParams?.get("key");
  return query ? query.trim() : "";
}

export function looksLikeGatewayKey(value) {
  return typeof value === "string" && value.startsWith(KEY_PREFIX) && value.length >= KEY_PREFIX.length + 20;
}