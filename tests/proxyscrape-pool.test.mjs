// proxyscrape 号池 + 代理分发测试：解析、去重、轮询、分片、导出、分发。
import { test } from "node:test";
import assert from "node:assert/strict";
import { mkdtempSync, readFileSync, existsSync } from "node:fs";
import { tmpdir } from "node:os";
import { join } from "node:path";

import {
  POOL_SCHEMA,
  ProxyscrapePool,
  maskAccount,
  maskProxy,
  parseEndpoint,
} from "../src/proxyscrape-pool.mjs";
import {
  PROXY_LINES,
  applyProxies,
  lineProxyFile,
  proxyShareStatus,
  readLineProxies,
  sharedProxyFile,
  withSharedProxies,
} from "../src/proxy-share.mjs";

const sample = (overrides = {}) => ({
  email: "pool@example.com",
  password: "pw-123456",
  accessToken: "acc-1",
  refreshToken: "ref-1",
  trialClaimed: true,
  proxies: ["http://u:p@proxy.example.com:8080", "http://u:p@proxy.example.com:8081"],
  ...overrides,
});

test("parseEndpoint understands every shape the panel emits", () => {
  assert.deepEqual(parseEndpoint("http://u:p@h.example:8080"), {
    endpoint: "http://u:p@h.example:8080",
    scheme: "http",
    host: "h.example",
    port: 8080,
    username: "u",
    password: "p",
  });
  assert.equal(parseEndpoint("http://h.example:3128").username, "");
  assert.equal(parseEndpoint("socks5://h.example:1080").scheme, "socks5");
  assert.equal(parseEndpoint("h.example:8080").scheme, "http", "没有 scheme 时按 http 处理");
  assert.equal(parseEndpoint("http://h.example:0"), null);
  assert.equal(parseEndpoint(""), null);
});

test("pool writes schema and dedupes proxies across accounts", () => {
  const dir = mkdtempSync(join(tmpdir(), "ps-pool-"));
  const pool = new ProxyscrapePool(join(dir, "proxyscrape-pool.json"));
  const first = pool.add(sample());
  assert.equal(first.created, true);
  assert.equal(first.proxies, 2);

  const second = pool.add(sample({ email: "other@example.com", accessToken: "acc-2", proxies: [
    "http://u:p@proxy.example.com:8080",
    "http://u:p@proxy.example.com:9090",
  ] }));
  assert.equal(second.created, true);
  assert.equal(second.proxies, 1, "同一条代理只入池一次");
  assert.equal(pool.proxies.length, 3);

  const raw = JSON.parse(readFileSync(pool.path, "utf8"));
  assert.equal(raw.schema, POOL_SCHEMA);
  assert.equal(raw.provider, "proxyscrape");
});

test("re-import merges by email and keeps createdAt", () => {
  const dir = mkdtempSync(join(tmpdir(), "ps-pool-"));
  const pool = new ProxyscrapePool(join(dir, "pool.json"));
  const first = pool.add(sample());
  const created = first.account.createdAt;
  const again = pool.add(sample({ trialClaimed: false }));
  assert.equal(again.created, false);
  assert.equal(pool.size, 1);
  assert.equal(pool.accounts[0].createdAt, created);
  assert.equal(pool.accounts[0].trialClaimed, false, "凭据/状态会刷新");
});

test("pick rotates and honours cooldown", () => {
  const dir = mkdtempSync(join(tmpdir(), "ps-pool-"));
  const pool = new ProxyscrapePool(join(dir, "pool.json"));
  pool.add(sample());
  const a = pool.pick();
  const b = pool.pick();
  assert.notEqual(a.endpoint, b.endpoint);

  pool.recordProxy(a.endpoint, { ok: false });
  pool.recordProxy(a.endpoint, { ok: false });
  const cooling = pool.proxies.find((p) => p.endpoint === a.endpoint);
  assert.ok(cooling.cooldownUntil > Date.now(), "连续失败两次进冷却");

  for (let i = 0; i < 5; i++) {
    const picked = pool.pick();
    assert.equal(picked.endpoint, b.endpoint, "冷却中的不会再被选中");
  }

  pool.recordProxy(b.endpoint, { ok: true });
  assert.equal(pool.stats().failures, 2);
});

test("shard splits the usable proxies across lines", () => {
  const dir = mkdtempSync(join(tmpdir(), "ps-pool-"));
  const pool = new ProxyscrapePool(join(dir, "pool.json"));
  pool.add(sample({ proxies: [
    "http://u:p@h1:1",
    "http://u:p@h2:2",
    "http://u:p@h3:3",
    "http://u:p@h4:4",
  ] }));
  const buckets = pool.shard(2);
  assert.equal(buckets.length, 2);
  assert.equal(buckets[0].length, 2);
  assert.equal(buckets[1].length, 2);
  assert.notDeepEqual(buckets[0], buckets[1]);
});

test("exportProxies writes one endpoint per line", () => {
  const dir = mkdtempSync(join(tmpdir(), "ps-pool-"));
  const pool = new ProxyscrapePool(join(dir, "pool.json"));
  pool.add(sample());
  const result = pool.exportProxies(join(dir, "proxies.txt"));
  const lines = readFileSync(result.file, "utf8").trim().split("\n");
  assert.equal(lines.length, 2);
  assert.ok(lines.every((line) => line.startsWith("http://")));
});

test("mask views never leak credentials", () => {
  const dir = mkdtempSync(join(tmpdir(), "ps-pool-"));
  const pool = new ProxyscrapePool(join(dir, "pool.json"));
  pool.add(sample());
  const account = maskAccount(pool.accounts[0]);
  assert.ok(!JSON.stringify(account).includes("acc-1"));
  assert.ok(!JSON.stringify(account).includes("u:p@"));
  assert.equal(account.proxyHosts[0], "proxy.example.com:8080");

  const proxy = maskProxy(pool.proxies[0]);
  assert.ok(!proxy.endpoint.includes("u:p@"), "面板只报 host:port");
  assert.equal(proxy.hasAuth, true);
});

/* --------------------------------------------------------------- 分发部分 */

test("applyProxies writes shared + per-line files", () => {
  const dir = mkdtempSync(join(tmpdir(), "ps-share-"));
  const pool = new ProxyscrapePool(join(dir, "pool.json"));
  pool.add(sample({ proxies: ["http://u:p@h1:1", "http://u:p@h2:2", "http://u:p@h3:3"] }));

  const share = applyProxies({ pool, dataDir: dir });
  assert.equal(share.total, 3);
  assert.ok(existsSync(sharedProxyFile(dir)));
  assert.equal(share.lines.length, PROXY_LINES.length);

  const shared = readFileSync(sharedProxyFile(dir), "utf8").trim().split("\n");
  assert.equal(shared.length, 3);

  // 每条线各拿一份，且合起来正好是全部（轮转分片，不重复不遗漏）
  const perLine = share.lines.flatMap((row) => readLineProxies(dir, row.line));
  assert.equal(perLine.length, 3);
  assert.equal(new Set(perLine).size, 3);
});

test("withSharedProxies prefers the request and falls back to the file", () => {
  const dir = mkdtempSync(join(tmpdir(), "ps-share-"));
  const pool = new ProxyscrapePool(join(dir, "pool.json"));
  pool.add(sample({ proxies: ["http://u:p@h1:1", "http://u:p@h2:2"] }));
  applyProxies({ pool, dataDir: dir, lines: ["cleverai"] });

  const fromRequest = withSharedProxies({ dataDir: dir, line: "cleverai", proxies: ["http://mine:1"] });
  assert.deepEqual(fromRequest.proxies, ["http://mine:1"]);
  assert.equal(fromRequest.source, "request");

  const fromFile = withSharedProxies({ dataDir: dir, line: "cleverai" });
  assert.equal(fromFile.source, "shared");
  assert.equal(fromFile.proxies.length, 2);

  // 没被分片的线退回 shared.txt（还没分发过的场景）
  const fallback = withSharedProxies({ dataDir: dir, line: "poe" });
  assert.equal(fallback.source, "shared");
  assert.equal(fallback.proxies.length, 2);

  // 完全没有分发过：空
  const bare = mkdtempSync(join(tmpdir(), "ps-share-bare-"));
  const none = withSharedProxies({ dataDir: bare, line: "poe" });
  assert.equal(none.source, "none");
  assert.deepEqual(none.proxies, []);
});

test("proxyShareStatus counts what each line got", () => {
  const dir = mkdtempSync(join(tmpdir(), "ps-share-"));
  const pool = new ProxyscrapePool(join(dir, "pool.json"));
  pool.add(sample({ proxies: ["http://u:p@h1:1", "http://u:p@h2:2"] }));
  applyProxies({ pool, dataDir: dir, lines: ["cleverai", "gabai"] });

  const status = proxyShareStatus(dir, ["cleverai", "gabai"]);
  assert.equal(status.total, 2);
  assert.equal(status.lines.length, 2);
  assert.equal(status.lines[0].file, lineProxyFile(dir, "cleverai"));
  assert.equal(status.lines.reduce((sum, row) => sum + row.count, 0), 2);
});
