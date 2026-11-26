// AMD 开发者云（GPU） 控制器（薄壳）：spawn `python -m amd_reg run --json`（cookie 导入）。
import { LineRegistrations } from "./openai-lines.mjs";
import { LINE } from "./amd-pool.mjs";

export { LINE };

export const createRegistrations = (options) => new LineRegistrations({ line: LINE, ...options });
