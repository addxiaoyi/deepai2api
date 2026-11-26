// Gitee AI（模力方舟） 令牌导入控制器（薄壳）。
import { LineRegistrations } from "./openai-lines.mjs";
import { LINE } from "./giteeai-pool.mjs";

export { LINE };

export const createRegistrations = (options) => new LineRegistrations({ line: LINE, ...options });
