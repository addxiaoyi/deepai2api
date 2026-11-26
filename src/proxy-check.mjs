// 代理存活检查：TCP 连一下 host:port。
//
// 只做"端口通不通"，不做 HTTP CONNECT 验证 —— 目的是把彻底死掉的代理筛掉，
// 免得导出给手机/路由器一堆连不上的节点。带并发上限和超时。
import { Socket } from "node:net";

/**
 * @param {{host: string, port: number}} proxy
 * @param {{timeoutMs?: number}} options
 */
export function probeTcp(proxy, { timeoutMs = 4000 } = {}) {
  return new Promise((resolve) => {
    const started = Date.now();
    const socket = new Socket();
    let settled = false;
    const done = (ok, error = null) => {
      if (settled) return;
      settled = true;
      try {
        socket.destroy();
      } catch {
        /* ignore */
      }
      resolve({ ok, ms: Date.now() - started, error: error ? String(error).slice(0, 120) : null });
    };
    socket.setTimeout(timeoutMs);
    socket.once("connect", () => done(true));
    socket.once("timeout", () => done(false, "timeout"));
    socket.once("error", (error) => done(false, error.code ?? error.message));
    try {
      socket.connect(Number(proxy.port), String(proxy.host));
    } catch (error) {
      done(false, error.message);
    }
  });
}

/**
 * 并发探测一组代理，返回 `{alive, dead}`。
 *
 * @param {Array<{host: string, port: number}>} proxies
 * @param {{timeoutMs?: number, concurrency?: number, onResult?: Function}} options
 */
export async function probeProxies(proxies = [], { timeoutMs = 4000, concurrency = 8, onResult } = {}) {
  const rows = Array.isArray(proxies) ? proxies : [];
  const queue = [...rows];
  const alive = [];
  const dead = [];
  const workers = Array.from({ length: Math.max(1, Math.min(concurrency, rows.length || 1)) }, async () => {
    for (;;) {
      const proxy = queue.shift();
      if (!proxy) return;
      const result = await probeTcp(proxy, { timeoutMs });
      (result.ok ? alive : dead).push({ proxy, ...result });
      try {
        onResult?.({ proxy, ...result });
      } catch {
        /* 回调坏了不影响探测 */
      }
    }
  });
  await Promise.all(workers);
  return { alive, dead, total: rows.length };
}
