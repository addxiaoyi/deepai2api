// AMD 开发者云（GPU） 号池（薄壳：池语义全在 src/openai-lines.mjs，这里只是描述 + 实例字段）。
import { LinePool, defineLine, maskAccount } from "./openai-lines.mjs";

export const POOL_SCHEMA = "dcg.pool/amd@1";

export const LINE = defineLine({
  id: "amd",
  kind: "gpu-pool",
  name: "AMD 开发者云（GPU）",
  baseUrl: "https://developer.amd.com.cn",
  apiBase: "https://developer.amd.com.cn/api",
  keyPrefix: "",
  anonymous: false,
  anthropic: false,
  imagesOnly: false,
  // GPU 资源不是 OpenAI 兼容面：没有模型列表，也没有 /v1 反代
  models: [],
  envPrefix: "AMD_",
  docs: ["docs/AMD-POOL.md"],
});

export const at = (dataDir, options) => LinePool.at(LINE, dataDir, options);

export const mask = maskAccount;

/** 池概览里额外关心的东西：积分、是否有实例。 */
export function instanceSummary(pool) {
  const rows = pool.accounts.map((account) => ({
    email: account.email,
    points: account.credits?.points ?? null,
    hasInstance: account.credits?.hasInstance ?? null,
    cookiePreview: account.apiKey ? `${String(account.apiKey).slice(0, 8)}…` : "",
  }));
  return {
    total: rows.length,
    withInstance: rows.filter((row) => row.hasInstance === true).length,
    totalPoints: rows.reduce((sum, row) => sum + (Number(row.points) || 0), 0),
    rows,
  };
}

export default LINE;
