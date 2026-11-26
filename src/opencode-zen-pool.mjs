// OpenCode Zen 号池（薄壳：实现全在 src/openai-lines.mjs）。
import { LinePool, defineLine, maskAccount } from "./openai-lines.mjs";

export const POOL_SCHEMA = "dcg.pool/opencode-zen@1";

export const LINE = defineLine({
  id: "opencode-zen",
  name: "OpenCode Zen",
  baseUrl: "https://opencode.ai",
  apiBase: "https://opencode.ai/zen/v1",
  keyPrefix: "sk-",
  freeSuffix: "-free",
  anonymous: false,
  anthropic: false,
  models: ["mimo-v2.5-free", "nemotron-3-ultra-free"],
  envPrefix: "OPENCODE_ZEN_",
  docs: ["docs/OPENCODE-ZEN-POOL.md"],
});

export class Pool extends LinePool {
  static at(dataDir, options) {
    return new LinePool(LINE, undefined, options) && LinePool.at(LINE, dataDir, options);
  }
}

export const at = (dataDir, options) => LinePool.at(LINE, dataDir, options);

export const mask = maskAccount;

export default LINE;
