// UnoRouter 号池（薄壳：实现全在 src/openai-lines.mjs）。
import { LinePool, defineLine, maskAccount } from "./openai-lines.mjs";

export const POOL_SCHEMA = "dcg.pool/unorouter@1";

export const LINE = defineLine({
  id: "unorouter",
  name: "UnoRouter",
  baseUrl: "https://unorouter.com",
  apiBase: "https://api.unorouter.com/v1",
  keyPrefix: "sk-",
  freeSuffix: ":free",
  anonymous: false,
  anthropic: true,
  models: ["deepseek-v4-flash:free", "glm-5.2:free"],
  envPrefix: "UNOROUTER_",
  docs: ["docs/UNOROUTER-POOL.md"],
});

export class Pool extends LinePool {
  static at(dataDir, options) {
    return new LinePool(LINE, undefined, options) && LinePool.at(LINE, dataDir, options);
  }
}

export const at = (dataDir, options) => LinePool.at(LINE, dataDir, options);

export const mask = maskAccount;

export default LINE;
