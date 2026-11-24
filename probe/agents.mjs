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

const tryAgent = async (label, dispatcher) => {
  try {
    const res = await ufetch(url, { headers, dispatcher });
    const body = await res.json();
    console.log(label, "->", res.status, "models:", body.models?.length ?? "n/a");
  } catch (error) {
    console.log(label, "-> FAILED", error.message, error.cause?.code ?? "");
  }
};

await tryAgent("EnvHttpProxyAgent", new EnvHttpProxyAgent());
await tryAgent("ProxyAgent(http://127.0.0.1:10808)", new ProxyAgent("http://127.0.0.1:10808"));
await tryAgent("ProxyAgent(https_proxy env)", new ProxyAgent(process.env.https_proxy));
await tryAgent("no dispatcher", undefined);