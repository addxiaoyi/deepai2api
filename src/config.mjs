import { existsSync } from "node:fs";
import { homedir } from "node:os";
import { join } from "node:path";

export function loadConfig(argv = process.argv.slice(2)) {
  const flags = parseFlags(argv);
  const env = process.env;

  const home = env.DEEPAI_CODE_HOME?.trim() || join(homedir(), ".deepai-code");
  const secretsDir = join(home, "userdata", "secrets");
  const port = Number(flags.port ?? env.DEEPAI_GATEWAY_PORT ?? 8787);
  if (!Number.isInteger(port) || port < 1 || port > 65535) {
    throw new Error(`端口不合法：${flags.port ?? env.DEEPAI_GATEWAY_PORT}（应为 1-65535 的整数）`);
  }

  const host = flags.host ?? env.DEEPAI_GATEWAY_HOST ?? "127.0.0.1";
  const token = flags.token ?? env.DEEPAI_GATEWAY_TOKEN ?? null;

  // Binding to every interface publishes the admin API; never do it silently
  // with an auto-generated token that only exists on this machine.
  if (host !== "127.0.0.1" && host !== "localhost" && !token) {
    throw new Error(
      `绑定到 ${host} 会让管理接口暴露到网络，必须显式设置管理口令：\n` +
        `  --token <强口令>  或  DEEPAI_GATEWAY_TOKEN=<强口令>`,
    );
  }

  return {
    flags,
    host,
    port,
    // Admin token: unlock the admin API. Persisted under the data dir on first run.
    token,
    home,
    dataDir: flags.data ?? env.DEEPAI_GATEWAY_DATA ?? join(process.cwd(), "data"),
    secretsDir,
    apiKey: flags["api-key"] ?? env.DEEPAI_API_KEY ?? null,
    machineRaw: env.DEEPAI_MACHINE_ID ?? env.DEEPAI_MACHINE_RAW ?? null,
    defaultModel: env.DEEPAI_DEFAULT_MODEL ?? "deepseek-v4.1-flash",
    laneVersion: Number(env.DEEPAI_LANE_VERSION ?? 7),
    appVersion: env.DEEPAI_APP_VERSION ?? "0.3.50",
    apiOrigin: env.DEEPAI_API_ORIGIN ?? "https://api.deepai.org",
    signInOrigin: env.DEEPAI_SIGNIN_ORIGIN ?? "https://deepai.org",
    // Open WebUI is the UI; "/" redirects here when set.
    webuiUrl: flags.webui ?? env.DEEPAI_WEBUI_URL ?? null,
    publicBase: flags["public-base"] ?? env.DEEPAI_PUBLIC_BASE ?? `http://127.0.0.1:${port}`,
    // Upstream serialises metered turns one at a time per account; free models bypass it.
    maxConcurrentMetered: Number(env.DEEPAI_MAX_CONCURRENT ?? 1),
    catalogTtlMs: Number(env.DEEPAI_CATALOG_TTL_MS ?? 300_000),
    log: (env.DEEPAI_GATEWAY_LOG ?? "info").toLowerCase(),
  };
}

function parseFlags(argv) {
  const out = {};
  for (let i = 0; i < argv.length; i++) {
    const a = argv[i];
    if (!a.startsWith("--")) continue;
    const eq = a.indexOf("=");
    if (eq !== -1) {
      out[a.slice(2, eq)] = a.slice(eq + 1);
      continue;
    }
    const name = a.slice(2);
    const next = argv[i + 1];
    if (next !== undefined && !next.startsWith("--")) {
      out[name] = next;
      i++;
    } else out[name] = true;
  }
  return out;
}