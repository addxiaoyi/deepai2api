import { EnvHttpProxyAgent, ProxyAgent, fetch as ufetch } from "undici";

const cred = JSON.parse(
  (await import("node:fs")).readFileSync(
    (await import("node:path")).join(
      (await import("node:os")).homedir(),
      ".deepai-code/userdata/secrets/deepai-account.bin",
    ),
    "utf8",
  ),
);
const headers = {
  "api-key": cred.apiKey,
  "X-DeepAI-Code-Theos": "7",
  "X-DeepAI-Code-Instance": "0123456789abcdef0123456789abcdef",
};
const url = "https://api.deepai.org/deepai_code/models";

const attempt = async (label, dispatcher) => {
  for (let i = 1; i <= 3; i++) {
    try {
      const res = await ufetch(url, { headers, dispatcher, redirect: "error" });
      const body = await res.json();
      console.log(`${label} try${i} ->`, res.status, body.models?.length ?? "n/a");
      return;
    } catch (error) {
      console.log(`${label} try${i} -> FAILED`, error.cause?.code ?? error.message);
    }
  }
};

await attempt("EnvHttpProxyAgent(default)", new EnvHttpProxyAgent());
await attempt("EnvHttpProxyAgent(connectTimeout)", new EnvHttpProxyAgent({ connectTimeout: 15000 }));
await attempt("ProxyAgent(http)", new ProxyAgent("http://127.0.0.1:10808"));
await attempt("ProxyAgent(http)+connectTimeout", new ProxyAgent({ uri: "http://127.0.0.1:10808", connectTimeout: 15000 }));