// Gitee AI（模力方舟） 号池（薄壳：实现全在 src/openai-lines.mjs）。
import { LinePool, defineLine, maskAccount } from "./openai-lines.mjs";

export const POOL_SCHEMA = "dcg.pool/giteeai@1";

export const LINE = defineLine({
  id: "giteeai",
  name: "Gitee AI（模力方舟）",
  baseUrl: "https://ai.gitee.com",
  apiBase: "https://ai.gitee.com/v1",
  keyPrefix: "",
  freeSuffix: "",
  anonymous: false,
  anthropic: false,
  imagesOnly: true,
  models: ["z-image-turbo", "Qwen-Image-Edit"],
  envPrefix: "GITEEAI_",
  docs: ["docs/GITEEAI-POOL.md"],
});

export const at = (dataDir, options) => LinePool.at(LINE, dataDir, options);

export const mask = maskAccount;

export default LINE;
