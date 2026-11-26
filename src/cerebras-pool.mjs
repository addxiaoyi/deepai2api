// Cerebras 号池（薄壳：实现全在 src/openai-lines.mjs）。
import { LinePool, defineLine, maskAccount } from "./openai-lines.mjs";

export const POOL_SCHEMA = "dcg.pool/cerebras@1";

export const LINE = defineLine({
  id: "cerebras",
  name: "Cerebras",
  baseUrl: "https://cloud.cerebras.ai",
  apiBase: "https://api.cerebras.ai/v1",
  keyPrefix: "csk-",
  freeSuffix: "",
  anonymous: false,
  anthropic: false,
  models: ["llama-3.3-70b", "qwen-3-235b-a22b"],
  envPrefix: "CEREBRAS_",
  docs: ["docs/CEREBRAS-POOL.md"],
});

export class Pool extends LinePool {
  static at(dataDir, options) {
    return new LinePool(LINE, undefined, options) && LinePool.at(LINE, dataDir, options);
  }
}

export const at = (dataDir, options) => LinePool.at(LINE, dataDir, options);

export const mask = maskAccount;

export default LINE;
