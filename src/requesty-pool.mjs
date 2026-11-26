// Requesty 号池（薄壳：实现全在 src/openai-lines.mjs）。
import { LinePool, defineLine, maskAccount } from "./openai-lines.mjs";

export const POOL_SCHEMA = "dcg.pool/requesty@1";

export const LINE = defineLine({
  id: "requesty",
  name: "Requesty",
  baseUrl: "https://app.requesty.ai",
  apiBase: "https://router.requesty.ai/v1",
  keyPrefix: "sk-",
  freeSuffix: "",
  anonymous: false,
  anthropic: false,
  models: ["nvidia/nemotron-3-ultra", "gemma-4-31b"],
  envPrefix: "REQUESTY_",
  docs: ["docs/REQUESTY-POOL.md"],
});

export class Pool extends LinePool {
  static at(dataDir, options) {
    return new LinePool(LINE, undefined, options) && LinePool.at(LINE, dataDir, options);
  }
}

export const at = (dataDir, options) => LinePool.at(LINE, dataDir, options);

export const mask = maskAccount;

export default LINE;
