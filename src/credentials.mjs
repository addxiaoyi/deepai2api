// Account credentials for the managed lane.
//
// The desktop app keeps them as a plain JSON file under
//   <DEEPAI_CODE_HOME>/userdata/secrets/deepai-account.bin
// (src/auth/ServerSecretStore.ts writes secrets to `<secretsDir>/<name>.bin`,
// chmod 0600 on POSIX, no extra encryption layer). Schema:
//   { apiKey, username, isPro, connect?: { token, expiresAt }, connectUnavailable? }
import { readFileSync, readdirSync, statSync } from "node:fs";
import { join } from "node:path";

export class CredentialsError extends Error {
  constructor(message, options) {
    super(message, options);
    this.name = "CredentialsError";
  }
}

export function readStoredAccount(secretsDir) {
  const path = join(secretsDir, "deepai-account.bin");
  let raw;
  try {
    raw = readFileSync(path, "utf8");
  } catch (cause) {
    throw new CredentialsError(
      `No DeepAI account found at ${path}. Sign in once in the DeepAI Code desktop app, or pass --api-key.`,
      { cause },
    );
  }
  let parsed;
  try {
    parsed = JSON.parse(raw);
  } catch (cause) {
    throw new CredentialsError(`${path} is not valid JSON.`, { cause });
  }
  if (!parsed || typeof parsed.apiKey !== "string" || parsed.apiKey.length === 0) {
    throw new CredentialsError(`${path} has no apiKey.`);
  }
  return {
    apiKey: parsed.apiKey,
    username: typeof parsed.username === "string" ? parsed.username : "",
    isPro: parsed.isPro === true,
    connect: parsed.connect ?? null,
    source: path,
  };
}

/** Every secret the app stores, without their contents. */
export function listSecrets(secretsDir) {
  try {
    return readdirSync(secretsDir)
      .filter((name) => name.endsWith(".bin"))
      .map((name) => {
        const path = join(secretsDir, name);
        return { name: name.slice(0, -4), bytes: statSync(path).size };
      });
  } catch {
    return [];
  }
}