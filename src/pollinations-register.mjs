// Pollinations 注册控制器（薄壳）。
import { LineRegistrations } from "./openai-lines.mjs";
import { LINE } from "./pollinations-pool.mjs";

export { LINE };

export class Registrations extends LineRegistrations {
  constructor(options) {
    super({ line: LINE, ...options });
  }
}

export const createRegistrations = (options) => new LineRegistrations({ line: LINE, ...options });
