// Agnes AI 号池（薄壳：实现全在 src/openai-lines.mjs）。
import { LinePool, defineLine, maskAccount } from "./openai-lines.mjs";

export const POOL_SCHEMA = "dcg.pool/agnes@1";

export const LINE = defineLine({
  id: "agnes",
  name: "Agnes AI",
  baseUrl: "https://api.agnes-ai.cn",
  apiBase: "https://api.agnes-ai.cn/v1",
  keyPrefix: "sk-",
  freeSuffix: "",
  anonymous: false,
  anthropic: false,
  models: ["agnes-3.0-flash", "agnes-2.5-flash"],
  envPrefix: "AGNES_",
  docs: ["docs/AGNES-POOL.md"],
});

export class Pool extends LinePool {
  static at(dataDir, options) {
    return new LinePool(LINE, undefined, options) && LinePool.at(LINE, dataDir, options);
  }
}

export const at = (dataDir, options) => LinePool.at(LINE, dataDir, options);

export const mask = maskAccount;

export default LINE;
