// 代理导出 —— 给"其他设备"用的形状。
//
// 代理池里的串是 `http://user:pass@host:port`，但手机/路由器/客户端要的格式各不相同：
// Clash 要 YAML、sing-box 要 JSON、Surge/Shadowrocket 要一行一条、代理检测工具要
// `host:port:user:pass`。再加上"别的设备不方便填管理口令"，所以还有一层**订阅令牌**：
//
//   POST /api/admin/proxies/share  {name, format}  -> {token, url}
//   GET  /proxy/<token>                            -> 渲染好的订阅（无需管理口令）
//
// 令牌可撤销、可限格式、带命中计数（`data/proxies/shares.json`，0600）。
import { randomBytes } from "node:crypto";
import { existsSync, mkdirSync, readFileSync, renameSync, writeFileSync } from "node:fs";
import { resolve } from "node:path";

export const SHARE_SCHEMA = "dcg.proxy-shares@1";

/** 支持的导出格式：id -> {label, contentType, ext} */
export const FORMATS = {
  url: { label: "URL（http://user:pass@host:port）", contentType: "text/plain; charset=utf-8", ext: "txt" },
  raw: { label: "host:port:user:pass", contentType: "text/plain; charset=utf-8", ext: "txt" },
  at: { label: "user:pass@host:port", contentType: "text/plain; charset=utf-8", ext: "txt" },
  sub2api: { label: "sub2api JSON（proxies[] + proxy_key）", contentType: "application/json; charset=utf-8", ext: "json" },
  "sub2api-txt": { label: "sub2api / CPA 文本（一行一条 URL）", contentType: "text/plain; charset=utf-8", ext: "txt" },
  cpa: { label: "CPA（CLIProxyApi）文本（一行一条 URL）", contentType: "text/plain; charset=utf-8", ext: "txt" },
  json: { label: "JSON 数组", contentType: "application/json; charset=utf-8", ext: "json" },
  csv: { label: "CSV", contentType: "text/csv; charset=utf-8", ext: "csv" },
  clash: { label: "Clash / Mihomo YAML", contentType: "text/yaml; charset=utf-8", ext: "yaml" },
  singbox: { label: "sing-box JSON", contentType: "application/json; charset=utf-8", ext: "json" },
  surge: { label: "Surge / Shadowrocket", contentType: "text/plain; charset=utf-8", ext: "conf" },
  env: { label: "环境变量（bash / PowerShell）", contentType: "text/plain; charset=utf-8", ext: "txt" },
};

export const DEFAULT_FORMAT = "url";

const safeName = (value, fallback) => {
  const cleaned = String(value ?? "")
    .replace(/[^\w\u4e00-\u9fa5.-]+/g, "-")
    .replace(/^-+|-+$/g, "")
    .slice(0, 40);
  return cleaned || fallback;
};

/** 每行一条的通用渲染；`mapper` 决定单条形状。 */
const lines = (proxies, mapper) =>
  proxies
    .map((proxy, index) => mapper(proxy, index + 1))
    .filter(Boolean)
    .join("\n");

const auth = (proxy) => (proxy.username ? `${proxy.username}:${proxy.password ?? ""}` : "");

/**
 * sub2api 的代理对象。形状与 `cpa_to_sub2api` 的 `ProxyConfig.to_dict()` 一致：
 *
 *   {"proxy_key":"http|host|port|user|pass","name":…,"protocol":"http",
 *    "host":…,"port":…,"status":"active","username":…,"password":…}
 *
 * `proxy_key` 是 sub2api 的去重键（协议|地址|端口|用户|密码），一定要带。
 */
export function toSub2apiProxy(proxy, { name = "", status = "active", index = 0 } = {}) {
  const protocol = String(proxy.scheme ?? "http").toLowerCase() || "http";
  const host = String(proxy.host ?? "").trim();
  const port = Number(proxy.port);
  const username = String(proxy.username ?? "").trim();
  const password = String(proxy.password ?? "").trim();
  const out = {
    proxy_key: `${protocol}|${host}|${port}|${username}|${password}`,
    name: String(name || `${host}-${port}` || `proxy-${index + 1}`).trim(),
    protocol,
    host,
    port,
    status: status === "inactive" ? "inactive" : "active",
  };
  if (username) out.username = username;
  if (password) out.password = password;
  return out;
}

/** sub2api / CPA 通用的一行 URL（txt 形态）。 */
const proxyUrl = (proxy) =>
  `${proxy.scheme ?? "http"}://${auth(proxy) ? `${auth(proxy)}@` : ""}${proxy.host}:${proxy.port}`;

/**
 * 把一组代理渲染成指定格式。
 *
 * @param {Array<{scheme?:string,host:string,port:number,username?:string,password?:string}>} proxies
 * @param {string} format
 * @param {{name?: string, prefix?: string}} options
 */
export function renderProxies(proxies = [], format = DEFAULT_FORMAT, { name = "proxyscrape", prefix = "" } = {}) {
  const rows = (Array.isArray(proxies) ? proxies : []).filter((p) => p && p.host && p.port);
  const fmt = FORMATS[format] ? format : DEFAULT_FORMAT;

  switch (fmt) {
    case "raw":
      return rows.length ? lines(rows, (p) => `${p.host}:${p.port}:${p.username ?? ""}:${p.password ?? ""}`) + "\n" : "";
    case "at":
      return rows.length
        ? lines(rows, (p) => (auth(p) ? `${auth(p)}@${p.host}:${p.port}` : `${p.host}:${p.port}`)) + "\n"
        : "";
    case "sub2api": {
      // 顶层形状与 sub2api「导入数据」一致：exported_at / proxies / accounts
      const proxies = rows.map((p, i) =>
        toSub2apiProxy(p, {
          name: `${safeName(name, "proxyscrape")}-${i + 1}`,
          status: p.status === "inactive" || (p.cooldownUntil ?? 0) > Date.now() ? "inactive" : "active",
          index: i,
        }),
      );
      return (
        JSON.stringify(
          {
            exported_at: new Date().toISOString().replace(/\.\d{3}Z$/, "Z"),
            proxies,
            // 只导代理：账号留空数组，导入后不会动 sub2api 里已有的账号
            accounts: [],
          },
          null,
          2,
        ) + "\n"
      );
    }
    case "sub2api-txt":
    case "cpa":
      // 这两个工具都能直接吃"一行一个代理 URL"的文本
      return rows.length ? lines(rows, (p) => proxyUrl(p)) + "\n" : "";
    case "json":
      return (
        JSON.stringify(
          rows.map((p) => ({
            scheme: p.scheme ?? "http",
            host: p.host,
            port: Number(p.port),
            username: p.username ?? "",
            password: p.password ?? "",
            url: `${p.scheme ?? "http"}://${auth(p) ? `${auth(p)}@` : ""}${p.host}:${p.port}`,
          })),
          null,
          2,
        ) + "\n"
      );
    case "csv": {
      const header = "scheme,host,port,username,password";
      const body = rows.map((p) =>
        [p.scheme ?? "http", p.host, p.port, p.username ?? "", p.password ?? ""]
          .map((v) => `"${String(v).replaceAll('"', '""')}"`)
          .join(","),
      );
      return [header, ...body].join("\n") + "\n";
    }
    case "clash": {
      const names = rows.map((p, i) => `${safeName(name, "proxyscrape")}-${i + 1}`);
      const proxies = rows
        .map((p, i) => {
          const parts = [
            `  - name: "${names[i]}"`,
            `    type: http`,
            `    server: ${p.host}`,
            `    port: ${Number(p.port)}`,
          ];
          if (p.username) parts.push(`    username: "${p.username}"`);
          if (p.password) parts.push(`    password: "${p.password}"`);
          return parts.join("\n");
        })
        .join("\n");
      const group = names.map((n) => `      - "${n}"`).join("\n");
      return [
        "# 由 deepai2api 代理池导出",
        "mixed-port: 7890",
        "allow-lan: true",
        "mode: rule",
        "log-level: info",
        "proxies:",
        proxies || "  []",
        "proxy-groups:",
        `  - name: "${safeName(name, "proxyscrape")}"`,
        "    type: select",
        "    proxies:",
        group || "      - DIRECT",
        "rules:",
        `  - MATCH,${safeName(name, "proxyscrape")}`,
        "",
      ].join("\n");
    }
    case "singbox": {
      const outbounds = rows.map((p, i) => ({
        type: "http",
        tag: `${safeName(name, "proxyscrape")}-${i + 1}`,
        server: p.host,
        server_port: Number(p.port),
        ...(p.username ? { username: p.username, password: p.password ?? "" } : {}),
      }));
      return (
        JSON.stringify(
          {
            log: { level: "info" },
            outbounds: [
              ...outbounds,
              ...(outbounds.length
                ? [{ type: "selector", tag: safeName(name, "proxyscrape"), outbounds: outbounds.map((o) => o.tag) }]
                : []),
              { type: "direct", tag: "direct" },
            ],
            route: { final: outbounds.length ? safeName(name, "proxyscrape") : "direct" },
          },
          null,
          2,
        ) + "\n"
      );
    }
    case "surge":
      return rows.length
        ? lines(rows, (p, i) => {
            const tag = `${safeName(name, "ps")}-${i}`;
            const authPart = p.username ? `, username=${p.username}, password=${p.password ?? ""}` : "";
            return `${tag} = http, ${p.host}, ${Number(p.port)}${authPart}`;
          }) + "\n"
        : "";
    case "env":
      return rows.length
        ? [
            "# bash / zsh",
            `export HTTP_PROXY="${prefix || `${rows[0].scheme ?? "http"}://${auth(rows[0]) ? `${auth(rows[0])}@` : ""}${rows[0].host}:${rows[0].port}`}"`,
            `export HTTPS_PROXY="$HTTP_PROXY"`,
            "",
            "# PowerShell",
            `$env:HTTP_PROXY  = "$HTTP_PROXY"`,
            `$env:HTTPS_PROXY = "$HTTP_PROXY"`,
            "",
            "# 候选（按顺序试）",
            ...rows.map(
              (p) =>
                `${p.scheme ?? "http"}://${auth(p) ? `${auth(p)}@` : ""}${p.host}:${p.port}`,
            ),
            "",
          ].join("\n")
        : "";
    case "url":
    default:
      return rows.length
        ? lines(rows, (p) => `${p.scheme ?? "http"}://${auth(p) ? `${auth(p)}@` : ""}${p.host}:${p.port}`) + "\n"
        : "";
  }
}

/** 导出文件名：`proxyscrape-<format>-<date>.<ext>` */
export function exportFileName(format, { prefix = "proxyscrape" } = {}) {
  const fmt = FORMATS[format] ? format : DEFAULT_FORMAT;
  const stamp = new Date().toISOString().slice(0, 10);
  return `${prefix}-${fmt}-${stamp}.${FORMATS[fmt].ext}`;
}

/* ------------------------------------------------------------------ 订阅令牌 */

/**
 * 订阅令牌表：给不方便填管理口令的设备用。
 *
 * 一条令牌 = 一个 URL：`<gateway>/proxy/<token>`，可指定默认格式、可撤销、记命中数。
 * 令牌本身是 32 字节随机串，落盘 0600。
 */
export class ShareStore {
  constructor(dataDir) {
    this.path = resolve(dataDir, "proxies", "shares.json");
    this.state = this.#load();
  }

  #load() {
    const empty = { schema: SHARE_SCHEMA, version: 1, shares: [] };
    if (!existsSync(this.path)) return empty;
    try {
      const parsed = JSON.parse(readFileSync(this.path, "utf8"));
      const shares = Array.isArray(parsed.shares) ? parsed.shares : [];
      return { schema: SHARE_SCHEMA, version: 1, shares };
    } catch {
      return empty;
    }
  }

  save() {
    mkdirSync(resolve(this.path, ".."), { recursive: true });
    const tmp = `${this.path}.${randomBytes(4).toString("hex")}.tmp`;
    writeFileSync(tmp, JSON.stringify(this.state, null, 2), { mode: 0o600 });
    renameSync(tmp, this.path);
  }

  list() {
    return this.state.shares.map((share) => ({ ...share }));
  }

  /** 新建：`format` 缺省用 url；同名会覆盖（便于"重新生成链接"）。 */
  mint({ name = "", format = DEFAULT_FORMAT, note = "" } = {}) {
    const fmt = FORMATS[format] ? format : DEFAULT_FORMAT;
    const label = safeName(name, `share-${this.state.shares.length + 1}`);
    const existing = this.state.shares.find((s) => s.name === label && !s.revoked);
    if (existing) {
      existing.format = fmt;
      existing.note = String(note ?? "").slice(0, 200);
      existing.updatedAt = new Date().toISOString();
      this.save();
      return { ...existing, reused: true };
    }
    const share = {
      token: randomBytes(24).toString("base64url"),
      name: label,
      format: fmt,
      note: String(note ?? "").slice(0, 200),
      hits: 0,
      lastUsedAt: null,
      revoked: false,
      createdAt: new Date().toISOString(),
      updatedAt: new Date().toISOString(),
    };
    this.state.shares.push(share);
    this.save();
    return { ...share, reused: false };
  }

  find(token) {
    return this.state.shares.find((s) => s.token === String(token ?? "")) ?? null;
  }

  /** 命中计数（订阅客户端会定时拉，用来判断链接是否还活着）。 */
  touch(token, { format = "" } = {}) {
    const share = this.find(token);
    if (!share) return null;
    share.hits = (share.hits ?? 0) + 1;
    share.lastUsedAt = new Date().toISOString();
    if (format && FORMATS[format]) share.lastFormat = format;
    this.save();
    return share;
  }

  revoke(token) {
    const share = this.find(token);
    if (!share) return false;
    share.revoked = true;
    share.updatedAt = new Date().toISOString();
    this.save();
    return true;
  }

  remove(token) {
    const before = this.state.shares.length;
    this.state.shares = this.state.shares.filter((s) => s.token !== String(token ?? ""));
    if (this.state.shares.length === before) return false;
    this.save();
    return true;
  }
}

/** 对外展示用：token 打码。 */
export function maskShare(share) {
  return {
    name: share.name,
    token: `${String(share.token).slice(0, 6)}…${String(share.token).slice(-4)}`,
    format: share.format,
    note: share.note,
    hits: share.hits ?? 0,
    lastUsedAt: share.lastUsedAt ?? null,
    lastFormat: share.lastFormat ?? null,
    revoked: share.revoked === true,
    createdAt: share.createdAt,
  };
}
