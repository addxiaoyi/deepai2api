import { ProxyAgent, EnvHttpProxyAgent, fetch as undiciFetch } from "undici";

/**
 * One dispatcher for the whole process.
 *
 * The app routes DeepAI traffic through `EnvHttpProxyAgent` when proxy env vars
 * are set (src/deepaiFetch.ts), because the lane is unreachable directly from
 * some networks. Same here: honour HTTP(S)_PROXY/ALL_PROXY/NO_PROXY.
 */
export function makeDispatcher() {
  const env = process.env;
  const hasProxy = Boolean(env.https_proxy || env.HTTPS_PROXY || env.http_proxy || env.HTTP_PROXY || env.all_proxy || env.ALL_PROXY);
  if (!hasProxy) return { dispatcher: undefined, mode: "direct", proxy: null };

  try {
    // No options: passing connectTimeout here gets forwarded into the inner
    // Agent and silently breaks proxying (verified against undici 7.30).
    return { dispatcher: new EnvHttpProxyAgent(), mode: "env", proxy: env.https_proxy || env.HTTPS_PROXY || env.http_proxy || env.HTTP_PROXY || env.all_proxy };
  } catch {
    return { dispatcher: new ProxyAgent(env.https_proxy || env.HTTPS_PROXY || env.http_proxy || env.HTTP_PROXY || env.all_proxy), mode: "proxy", proxy: env.https_proxy || env.HTTPS_PROXY || env.http_proxy || env.HTTP_PROXY };
  }
}

export async function laneFetch(url, init, dispatcher) {
  return undiciFetch(url, { ...init, dispatcher });
}

const TRANSIENT = new Set([
  "ECONNRESET",
  "ECONNREFUSED",
  "EPIPE",
  "ETIMEDOUT",
  "EAI_AGAIN",
  "UND_ERR_SOCKET",
  "UND_ERR_CONNECT_TIMEOUT",
  "UND_ERR_HEADERS_TIMEOUT",
  "UND_ERR_CLOSED",
]);

export const isTransient = (error) => {
  for (let depth = 0, e = error; depth < 4 && e; depth++, e = e.cause) {
    if (e.code && TRANSIENT.has(e.code)) return true;
    if (e.name === "TypeError" && /fetch failed|terminated/i.test(e.message ?? "")) return true;
  }
  return false;
};

/**
 * Browser-style transports reset idle tunnels constantly; a loopback gateway must
 * not surface that as a model failure. Retry transient connect/read errors only —
 * never a status refusal.
 */
export async function fetchRetry(url, init, dispatcher, { attempts = 3, baseDelayMs = 400, signal = init?.signal } = {}) {
  for (let attempt = 1; ; attempt++) {
    try {
      return await laneFetch(url, init, dispatcher);
    } catch (error) {
      if (signal?.aborted || attempt >= attempts || !isTransient(error)) throw error;
      await new Promise((resolve) => {
        const timer = setTimeout(resolve, baseDelayMs * attempt);
        signal?.addEventListener("abort", () => {
          clearTimeout(timer);
          resolve();
        }, { once: true });
      });
      if (signal?.aborted) throw signal.reason;
    }
  }
}