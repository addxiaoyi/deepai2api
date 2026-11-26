// 代理导出格式 + 订阅令牌测试（纯本地，不联网）。
import { test } from "node:test";
import assert from "node:assert/strict";
import { mkdtempSync, readFileSync, existsSync, statSync } from "node:fs";
import { tmpdir } from "node:os";
import { join } from "node:path";

import {
  DEFAULT_FORMAT,
  FORMATS,
  ShareStore,
  exportFileName,
  maskShare,
  renderProxies,
} from "../src/proxy-export.mjs";

const ROWS = [
  { scheme: "http", host: "1.2.3.4", port: 8080, username: "u", password: "p" },
  { scheme: "http", host: "5.6.7.8", port: 3128, username: "", password: "" },
];

test("default format is a URL list", () => {
  assert.equal(DEFAULT_FORMAT, "url");
  const body = renderProxies(ROWS);
  assert.equal(body.trim().split("\n").length, 2);
  assert.ok(body.includes("http://u:p@1.2.3.4:8080"));
  assert.ok(body.includes("http://5.6.7.8:3128"), "无认证的不带 @");
});

test("raw / at / json / csv shapes", () => {
  assert.ok(renderProxies(ROWS, "raw").includes("1.2.3.4:8080:u:p"));
  assert.ok(renderProxies(ROWS, "at").includes("u:p@1.2.3.4:8080"));
  assert.ok(renderProxies(ROWS, "at").includes("5.6.7.8:3128"));

  const json = JSON.parse(renderProxies(ROWS, "json"));
  assert.equal(json.length, 2);
  assert.equal(json[0].host, "1.2.3.4");
  assert.equal(json[0].url, "http://u:p@1.2.3.4:8080");
  assert.equal(json[1].username, "");

  const csv = renderProxies(ROWS, "csv").trim().split("\n");
  assert.equal(csv[0], "scheme,host,port,username,password");
  assert.equal(csv.length, 3);
  assert.ok(csv[1].includes('"1.2.3.4"'));
});

test("clash yaml has proxies, a group and a MATCH rule", () => {
  const yaml = renderProxies(ROWS, "clash", { name: "my pool" });
  assert.match(yaml, /^proxies:/m);
  assert.ok(yaml.includes('type: http'));
  assert.ok(yaml.includes("server: 1.2.3.4"));
  assert.ok(yaml.includes("port: 8080"));
  assert.ok(yaml.includes('username: "u"'));
  assert.ok(yaml.includes("proxy-groups:"));
  // 名称里的空格必须被清掉，否则 YAML 组名带空格会被客户端拒绝
  assert.ok(yaml.includes('name: "my-pool"'));
  assert.ok(yaml.includes("MATCH,my-pool"));
});

test("sing-box json is loadable and routes to the selector", () => {
  const config = JSON.parse(renderProxies(ROWS, "singbox", { name: "phone" }));
  const types = config.outbounds.map((o) => o.type);
  assert.ok(types.includes("http"));
  assert.ok(types.includes("selector"));
  assert.ok(types.includes("direct"));
  assert.equal(config.route.final, "phone");
  assert.equal(config.outbounds[0].server_port, 8080);
});

test("surge / env render usable snippets", () => {
  const surge = renderProxies(ROWS, "surge", { name: "ps" });
  assert.match(surge, /^ps-1 = http, 1\.2\.3\.4, 8080, username=u, password=p$/m);
  assert.match(surge, /^ps-2 = http, 5\.6\.7\.8, 3128$/m);

  const env = renderProxies(ROWS, "env");
  assert.ok(env.includes("export HTTP_PROXY="));
  assert.ok(env.includes("$env:HTTPS_PROXY"));
  assert.ok(env.includes("http://u:p@1.2.3.4:8080"));
});

test("empty pool renders empty strings instead of throwing", () => {
  for (const fmt of Object.keys(FORMATS)) {
    const body = renderProxies([], fmt);
    assert.equal(typeof body, "string", fmt);
    if (fmt === "json") assert.equal(JSON.parse(body).length, 0);
    if (fmt === "clash") assert.ok(body.includes("proxies:"));
    if (fmt === "singbox") assert.ok(JSON.parse(body).outbounds.some((o) => o.type === "direct"));
  }
});

test("unknown format falls back to url", () => {
  const body = renderProxies(ROWS, "nope");
  assert.ok(body.includes("http://u:p@1.2.3.4:8080"));
});

test("exportFileName carries format and date", () => {
  const name = exportFileName("clash");
  assert.match(name, /^proxyscrape-clash-\d{4}-\d{2}-\d{2}\.yaml$/);
  assert.match(exportFileName("nope"), /\.txt$/);
});

/* -------------------------------------------------------------- 订阅令牌 */

test("share store mints, reuses by name, and persists 0600", () => {
  const dir = mkdtempSync(join(tmpdir(), "px-share-"));
  const store = new ShareStore(dir);

  const first = store.mint({ name: "phone", format: "clash" });
  assert.ok(first.token.length >= 20);
  assert.equal(first.format, "clash");
  assert.equal(first.reused, false);
  assert.equal(first.hits, 0);

  const file = join(dir, "proxies", "shares.json");
  assert.ok(existsSync(file));
  // Windows 不实现 POSIX 权限位（chmod 是空操作），只在类 Unix 上校验
  if (process.platform !== "win32") {
    const mode = statSync(file).mode & 0o777;
    assert.equal(mode, 0o600, "订阅表里有令牌，必须 0600");
  }

  const again = store.mint({ name: "phone", format: "singbox" });
  assert.equal(again.reused, true);
  assert.equal(again.token, first.token, "同名覆盖时沿用原令牌");
  assert.equal(again.format, "singbox");

  const persisted = JSON.parse(readFileSync(file, "utf8"));
  assert.equal(persisted.schema, "dcg.proxy-shares@1");
  assert.equal(persisted.shares.length, 1);
});

test("share store tracks hits and revokes", () => {
  const dir = mkdtempSync(join(tmpdir(), "px-share-"));
  const store = new ShareStore(dir);
  const share = store.mint({ name: "router", format: "surge" });

  store.touch(share.token, { format: "clash" });
  store.touch(share.token);
  const row = store.find(share.token);
  assert.equal(row.hits, 2);
  assert.equal(row.lastFormat, "clash");
  assert.ok(row.lastUsedAt);

  assert.equal(store.revoke(share.token), true);
  assert.equal(store.find(share.token).revoked, true);
  assert.equal(store.revoke("nope"), false);

  const masked = maskShare(store.find(share.token));
  assert.ok(!masked.token.includes(share.token));
  assert.ok(masked.token.includes("…"));
  assert.equal(masked.revoked, true);
});

test("share store separates entries by name", () => {
  const dir = mkdtempSync(join(tmpdir(), "px-share-"));
  const store = new ShareStore(dir);
  const a = store.mint({ name: "phone" });
  const b = store.mint({ name: "laptop" });
  assert.notEqual(a.token, b.token);
  assert.equal(store.list().length, 2);
  assert.equal(store.remove(a.token), true);
  assert.equal(store.list().length, 1);
});

test("sub2api export matches the import payload shape", () => {
  const body = renderProxies(ROWS, "sub2api", { name: "phone" });
  const payload = JSON.parse(body);
  assert.deepEqual(Object.keys(payload).sort(), ["accounts", "exported_at", "proxies"]);
  assert.equal(payload.accounts.length, 0, "只导代理，不能带账号");
  assert.match(payload.exported_at, /^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z$/);

  const [first, second] = payload.proxies;
  assert.equal(first.proxy_key, "http|1.2.3.4|8080|u|p");
  assert.equal(first.name, "phone-1");
  assert.equal(first.protocol, "http");
  assert.equal(first.host, "1.2.3.4");
  assert.equal(first.port, 8080);
  assert.equal(first.status, "active");
  assert.equal(first.username, "u");
  assert.equal(first.password, "p");

  // 无认证的不能写 username/password 字段（sub2api 会当成"要认证"）
  assert.equal(second.proxy_key, "http|5.6.7.8|3128||");
  assert.ok(!("username" in second));
  assert.ok(!("password" in second));
});

test("sub2api export marks cooling proxies inactive", () => {
  const rows = [{ ...ROWS[0], status: "active", cooldownUntil: Date.now() + 60_000 }];
  const payload = JSON.parse(renderProxies(rows, "sub2api"));
  assert.equal(payload.proxies[0].status, "inactive");

  const ok = JSON.parse(renderProxies([{ ...ROWS[0], status: "active", cooldownUntil: 0 }], "sub2api"));
  assert.equal(ok.proxies[0].status, "active");
});

test("sub2api-txt / cpa are one URL per line", () => {
  for (const format of ["sub2api-txt", "cpa"]) {
    const body = renderProxies(ROWS, format);
    const lines = body.trim().split("\n");
    assert.equal(lines.length, 2, format);
    assert.equal(lines[0], "http://u:p@1.2.3.4:8080", format);
    assert.equal(lines[1], "http://5.6.7.8:3128", format);
  }
});

test("new formats are registered with sane metadata", () => {
  for (const [id, expectExt] of [["sub2api", "json"], ["sub2api-txt", "txt"], ["cpa", "txt"]]) {
    assert.ok(FORMATS[id], `缺格式 ${id}`);
    assert.equal(FORMATS[id].ext, expectExt);
    assert.ok(FORMATS[id].label);
    assert.ok(exportFileName(id).endsWith(`.${expectExt}`));
  }
  assert.equal(FORMATS.sub2api.contentType.startsWith("application/json"), true);
});
