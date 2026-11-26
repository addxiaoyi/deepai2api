// Pollinations 号池（薄壳：实现全在 src/openai-lines.mjs）。
import { LinePool, defineLine, maskAccount } from "./openai-lines.mjs";

export const POOL_SCHEMA = "dcg.pool/pollinations@1";

export const LINE = defineLine({
  id: "pollinations",
  name: "Pollinations",
  baseUrl: "https://gen.pollinations.ai",
  apiBase: "https://gen.pollinations.ai/v1",
  keyPrefix: "",
  freeSuffix: "",
  anonymous: true,
  anthropic: false,
  models: ["openai", "openai-large", "gemini"],
  envPrefix: "POLLINATIONS_",
  docs: ["docs/POLLINATIONS-POOL.md"],
});

export class Pool extends LinePool {
  static at(dataDir, options) {
    return new LinePool(LINE, undefined, options) && LinePool.at(LINE, dataDir, options);
  }
}

export const at = (dataDir, options) => LinePool.at(LINE, dataDir, options);

export const mask = maskAccount;

export default LINE;
