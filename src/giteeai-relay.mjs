// Gitee AI（模力方舟） 反代（薄壳）。
import { LineRelay, explainModelError } from "./openai-lines.mjs";
import { LINE } from "./giteeai-pool.mjs";

export { LINE };

export class Relay extends LineRelay {
  constructor(options) {
    super({ line: LINE, ...options });
  }
}

export const explainError = (status, text) => explainModelError(LINE, status, text);
