// 本地 Turnstile 求解器客户端 —— 对应 `tools/captcha-solver`（vendor 自 Theyka/Turnstile-Solver）。
//
// 协议（Quart 服务）：
//   GET /turnstile?url=&sitekey=[&action=&cdata=]  -> 202 {"task_id": "…"}
//   GET /result?id=<task_id>                       -> 200 {"value": "<token>"}
//                                                     200 {"value": "CAPTCHA_NOT_READY"}
//                                                     422 {"value": "CAPTCHA_FAIL"}
//
// 为什么值得单独一层：proxyscrape / cleverai / 一堆国内站的注册页都是 Turnstile，
// 之前只能靠 Camoufox 现开浏览器或者买 capsolver；有了这个服务，任何线都能一元钱不花地过。
import { setTimeout as sleep } from "node:timers/promises";

export const DEFAULT_SOLVER_URL = "http://127.0.0.1:5000";

const baseOf = (url) => String(url || process.env.CAPTCHA_SOLVER_URL || DEFAULT_SOLVER_URL).replace(/\/+$/, "");

/**
 * 解一枚 Turnstile token。
 *
 * @param {{url: string, sitekey: string, action?: string, cdata?: string,
 *          solverUrl?: string, timeoutMs?: number, pollMs?: number, signal?: AbortSignal}} options
 * @returns {Promise<{token: string, attempts: number, ms: number}>}
 */
export async function solveTurnstile({
  url,
  sitekey,
  action = "",
  cdata = "",
  solverUrl = "",
  timeoutMs = 180_000,
  pollMs = 3000,
  signal = null,
} = {}) {
  if (!url || !sitekey) {
    const error = new Error("solveTurnstile 需要 url 与 sitekey");
    error.status = 400;
    throw error;
  }
  const base = baseOf(solverUrl);
  const deadline = Date.now() + timeoutMs;
  const query = new URLSearchParams({ url, sitekey });
  if (action) query.set("action", action);
  if (cdata) query.set("cdata", cdata);

  const created = await fetch(`${base}/turnstile?${query}`, { signal });
  if (!created.ok) {
    const text = await created.text().catch(() => "");
    const error = new Error(`solver 拒绝了任务（HTTP ${created.status}）${text.slice(0, 120)}`);
    error.status = 502;
    error.code = "solver_unavailable";
    throw error;
  }
  const body = await created.json().catch(() => ({}));
  const taskId = String(body.task_id ?? "");
  if (!taskId) {
    const error = new Error("solver 没返回 task_id");
    error.status = 502;
    error.code = "solver_unavailable";
    throw error;
  }

  const started = Date.now();
  let attempts = 0;
  while (Date.now() < deadline) {
    await (signal ? sleep(pollMs, undefined, { signal }) : sleep(pollMs));
    attempts++;
    const result = await fetch(`${base}/result?id=${encodeURIComponent(taskId)}`, { signal });
    const payload = await result.json().catch(() => ({}));
    const value = String(payload.value ?? "");
    if (value === "CAPTCHA_NOT_READY") continue;
    if (value && value !== "CAPTCHA_FAIL") {
      return { token: value, attempts, ms: Date.now() - started };
    }
    const error = new Error(`solver 解不出来：${value || `HTTP ${result.status}`}`);
    error.status = 502;
    error.code = "solver_failed";
    throw error;
  }
  const error = new Error("solver 超时");
  error.status = 504;
  error.code = "solver_timeout";
  throw error;
}

/** 探活：solver 首页 200 就算活着，顺带报告版本页文本长度。 */
export async function solverStatus({ solverUrl = "", timeoutMs = 4000 } = {}) {
  const base = baseOf(solverUrl);
  const started = Date.now();
  const controller = new AbortController();
  const timer = setTimeout(() => controller.abort(), timeoutMs);
  try {
    const response = await fetch(`${base}/`, { signal: controller.signal });
    const text = await response.text().catch(() => "");
    return {
      ok: response.ok,
      url: base,
      status: response.status,
      ms: Date.now() - started,
      hasDocs: /turnstile/i.test(text),
    };
  } catch (error) {
    return { ok: false, url: base, status: 0, ms: Date.now() - started, error: error.message };
  } finally {
    clearTimeout(timer);
  }
}
