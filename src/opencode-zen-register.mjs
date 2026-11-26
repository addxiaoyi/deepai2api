// OpenCode Zen 注册控制器（薄壳）。
import { LineRegistrations } from "./openai-lines.mjs";
import { LINE } from "./opencode-zen-pool.mjs";

export { LINE };

export class Registrations extends LineRegistrations {
  constructor(options) {
    super({ line: LINE, ...options });
  }
}

export const createRegistrations = (options) => new LineRegistrations({ line: LINE, ...options });
