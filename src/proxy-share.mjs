// 代理分发：proxyscrape 号池里的代理 → 其它注册机的出口。
//
// 落盘约定（都在 `data/proxies/` 下，一行一条 `http://user:pass@host:port`）：
//
//   shared.txt            全部可用代理
//   cleverai.txt          每条线一份，按轮转分片，避免多条线同时压同一条出口
//   gabai.txt  allmodels.txt  poe.txt  providers.txt  deepai.txt
//
// 谁读它们：`src/server.mjs` 在处理 `/api/admin/<line>/register` 时，如果请求里
// 没带 `proxies`，就从这里补 —— 也就是"注册机自动被配上出口"，不需要手工再贴一遍。
import { existsSync, mkdirSync, readFileSync, writeFileSync } from "node:fs";
import { join, resolve } from "node:path";

/** 会被自动配出口的线；顺序决定分片顺序。 */
export const PROXY_LINES = ["cleverai", "gabai", "allmodels", "poe", "providers", "deepai"];

export const proxiesDir = (dataDir) => resolve(dataDir, "proxies");

export function lineProxyFile(dataDir, line) {
  return join(proxiesDir(dataDir), `${line}.txt`);
}

export function sharedProxyFile(dataDir) {
  return join(proxiesDir(dataDir), "shared.txt");
}

const writeList = (file, rows) => {
  mkdirSync(resolve(file, ".."), { recursive: true });
  writeFileSync(file, rows.length ? rows.join("\n") + "\n" : "", { mode: 0o600 });
};

/**
 * 把号池里的可用代理写成 `shared.txt` + 每线一份分片。
 *
 * @param {{pool: object, dataDir: string, lines?: string[], limitPerLine?: number}} options
 */
export function applyProxies({ pool, dataDir, lines = PROXY_LINES, limitPerLine = 0 }) {
  const usable = (pool?.usable?.() ?? []).map((p) => p.endpoint).filter(Boolean);
  const shared = sharedProxyFile(dataDir);
  writeList(shared, usable);

  const buckets = pool?.shard?.(Math.max(1, lines.length)) ?? lines.map(() => []);
  const written = [];
  lines.forEach((line, index) => {
    let rows = buckets[index % Math.max(1, buckets.length)] ?? [];
    if (limitPerLine > 0) rows = rows.slice(0, limitPerLine);
    const file = lineProxyFile(dataDir, line);
    writeList(file, rows);
    written.push({ line, file, rows: rows.length });
  });

  return { total: usable.length, shared, lines: written };
}

/** 读某条线分到的代理；该线**没有**分片文件时才退回 shared.txt。 */
export function readLineProxies(dataDir, line, { limit = 0 } = {}) {
  const own = lineProxyFile(dataDir, line);
  const candidates = existsSync(own) ? [own] : [sharedProxyFile(dataDir)];
  for (const file of candidates) {
    if (!existsSync(file)) continue;
    const rows = readFileSync(file, "utf8")
      .split(/\r?\n/)
      .map((row) => row.trim())
      .filter(Boolean);
    return limit > 0 ? rows.slice(0, limit) : rows;
  }
  return [];
}

/**
 * 注册控制器用的一行封装：请求里给了就用请求的，没给就自动补齐。
 *
 * @param {{dataDir: string, line: string, proxies?: string[], limit?: number}} options
 */
export function withSharedProxies({ dataDir, line, proxies = [], limit = 0 }) {
  const provided = Array.isArray(proxies) ? proxies.filter(Boolean) : [];
  if (provided.length) return { proxies: provided, source: "request" };
  const shared = readLineProxies(dataDir, line, { limit });
  return { proxies: shared, source: shared.length ? "shared" : "none" };
}

/** 概览：每条线现在分到几条、shared 里总共有几条。 */
export function proxyShareStatus(dataDir, lines = PROXY_LINES) {
  const rows = lines.map((line) => ({
    line,
    file: lineProxyFile(dataDir, line),
    count: readLineProxies(dataDir, line).length,
  }));
  const shared = sharedProxyFile(dataDir);
  const total = existsSync(shared)
    ? readFileSync(shared, "utf8").split(/\r?\n/).filter(Boolean).length
    : 0;
  return { dir: proxiesDir(dataDir), total, lines: rows };
}
