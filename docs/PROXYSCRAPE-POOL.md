# proxyscrape 代理池（`dcg.pool/proxyscrape@1`）

这是唯一一条**产物是出口**的线：注册 proxyscrape.com、领 premium 试用、把试用给出的
代理写成池子，然后分发给其它注册机。

- 文件：`$DEEPAI_GATEWAY_DATA/proxyscrape-pool.json`（0600，原子写）
- 内容：`accounts[]`（账号）+ `proxies[]`（代理，去重后的全局池）
- worker：`tools/proxyscrape-reg`（`-m proxyscrape_reg`）
- job：`data/proxyscrape-register-job.json`
- 路由：`/api/admin/proxyscrape/*`、`/api/admin/proxies*`
- **没有反代面**：代理不是模型通道，产物直接喂给别的 worker

## 1. 上游协议

见 [`tools/proxyscrape-reg/README.md`](../tools/proxyscrape-reg/README.md)。三条硬事实：

1. 注册/登录都要 **Cloudflare Turnstile**（sitekey `0x4AAAAAAAFWUVCKyusT9T8r`，字段
   `cf_turnstile_token`），不带就是 400。
2. 试用是**按账号**领的：`GET /v2/v4/account/premium/trial-eligibility` → `{claimed}`，
   `POST /v2/v4/account/premium/claim-trial` 领；重复领回 400。
3. 代理凭据藏在 `accounts-summary` 的 `services.<product>` 下，字段名不统一，所以用
   **深扫描** + `proxy-setup` 文本双路兜底。

## 2. 顶层结构

```json
{
  "schema": "dcg.pool/proxyscrape@1",
  "version": 1,
  "provider": "proxyscrape",
  "baseUrl": "https://dashboard.proxyscrape.com",
  "updatedAt": "2026-11-26T12:00:00.000Z",
  "accounts": [],
  "proxies": []
}
```

### accounts[]

```json
{
  "id": "…",
  "email": "ada@example.com",
  "password": "Xk9#mQ2vLp7!aB",
  "accessToken": "…",
  "refreshToken": "…",
  "trialClaimed": true,
  "proxies": ["http://user-abc:pass-def@proxy.proxyscrape.com:8080"],
  "status": "active",
  "engine": "protocol",
  "createdAt": "…",
  "updatedAt": "…",
  "requests": 0
}
```

### proxies[]

```json
{
  "endpoint": "http://user-abc:pass-def@proxy.proxyscrape.com:8080",
  "scheme": "http",
  "host": "proxy.proxyscrape.com",
  "port": 8080,
  "username": "user-abc",
  "password": "pass-def",
  "sources": ["ada@example.com"],
  "status": "active",
  "ok": 0,
  "failures": 0,
  "cooldownUntil": 0,
  "lastError": null
}
```

约定：

- `endpoint` 是唯一键：多个账号带来同一条代理只存一份，`sources[]` 记来源。
- `pick()` 只给 `status=active && cooldownUntil<=now` 的；连续失败 2 次冷却 10 分钟。
- 面板视图只报 `host:port`（`maskProxy`），**明文串只在导出文件和分享文件里**。

## 3. 出口分发（"自动给其他注册机配置上"）

`src/proxy-share.mjs` 把代理写成文件，别的 worker 直接吃：

```
data/proxies/shared.txt      全部可用代理（一行一条 http://u:p@h:p）
data/proxies/cleverai.txt    每条线一份，按轮转分片（避免多条线压同一条出口）
data/proxies/gabai.txt
data/proxies/allmodels.txt
data/proxies/poe.txt
data/proxies/providers.txt
data/proxies/deepai.txt
```

触发点：

| 触发 | 行为 |
| --- | --- |
| `proxyscrape` 注册任务 `done` | 自动 `applyProxies()`，写全部分片 |
| `POST /api/admin/proxyscrape/pool/import` | 入池后立刻分发 |
| `POST /api/admin/proxies/apply` | 手动分发（可指定 `lines` / `limitPerLine`） |
| `node src/proxyscrape-cli.mjs apply --line cleverai` | 同上，命令行版 |

消费点：**注册控制器在请求里没带 `proxies` 时自动补**。
`/api/admin/cleverai/register` 与 `/api/admin/gabai/register` 已经接上
（响应里回 `proxiesSource: "request" | "shared" | "none"`）。其它线按同样一行接即可：

```js
const shared = withSharedProxies({ dataDir, line: "poe", proxies: body.proxies });
registrations.start({ ...body, proxies: shared.proxies });
```

读文件时的语义：该线**有**自己的分片文件就用它（哪怕是空文件）；**没有**才退回
`shared.txt`。所以"分发过但这条线分到 0 条"不会被别人的代理污染。

## 4. 接口

```
GET    /api/admin/proxyscrape/status
POST   /api/admin/proxyscrape/register            {count, concurrency, mailProvider, mailDomains[], captcha, captchaKey, engine, proxies[]}
POST   /api/admin/proxyscrape/register/stop
GET    /api/admin/proxyscrape/pool?limit=200      账号（脱敏）+ 代理（脱敏）
POST   /api/admin/proxyscrape/pool/import         {email, password, proxies[], trialClaimed}
POST   /api/admin/proxyscrape/pool/export?format=proxies|jsonl|csv|pool[&download=1]
GET    /api/admin/proxyscrape/proxies             代理池（只报 host:port）
DELETE /api/admin/proxyscrape/proxies/{endpoint}  按完整 endpoint 删（面板拿不到明文，故用 CLI/API）
DELETE /api/admin/proxyscrape/pool/{id}
GET    /api/admin/proxies                         分发状态（每条线几条）
POST   /api/admin/proxies/apply                   {lines?, limitPerLine?}

# 导出 / 订阅（给别的设备）
GET    /api/admin/proxies/formats                 支持的格式清单
GET    /api/admin/proxies/export?format=&alive=1&limit=[&download=1]
POST   /api/admin/proxies/check                   {timeoutMs?, concurrency?}  TCP 探测并回写健康
GET    /api/admin/proxies/shares                  订阅列表（令牌打码 + url）
POST   /api/admin/proxies/share                   {name, format} 生成/复用订阅令牌
DELETE /api/admin/proxies/shares/{token|name}[?purge=1]
GET    /proxy/{token}?format=                     公开面：设备端拉订阅（无需管理口令）
```

## 5. 命令

```bash
node src/proxyscrape-cli.mjs register -n 3 --mail-provider imap --mail-domain example.com
node src/proxyscrape-cli.mjs proxies                  # 一行一条，可直接喂给别的 worker
node src/proxyscrape-cli.mjs apply --line cleverai    # 只给一条线写分片
node src/proxyscrape-cli.mjs share                    # 每条线现在分到几条
node src/proxyscrape-cli.mjs export --format proxies
```

## 5.5 导出给其他设备（手机 / 路由器 / 另一台电脑）

代理池里的原始串是 `http://user:pass@host:port`，但设备端要的形状各不相同。所以有两层：

**A. 一次性导出（文件/文本）**

| `format` | 产物 | 给谁 |
| --- | --- | --- |
| `url`（默认） | `http://u:p@h:p` 一行一条 | 任何 HTTP 客户端、env |
| `raw` | `h:p:u:p` | 代理检测工具、部分采集面板 |
| `sub2api` | sub2api「导入数据」JSON（`exported_at` / `proxies[]` / `accounts:[]`） | sub2api 后台直接上传 |
| `sub2api-txt` | 一行一条 `protocol://u:p@h:p` | 粘进 sub2api 代理框 |
| `cpa` | 一行一条 `protocol://u:p@h:p` | CPA / CLIProxyApi 的 `proxy` 字段 |
| `at` | `u:p@h:p` | curl / requests 的 `proxies=` |
| `json` / `csv` | 结构化 | 自建调度、表格 |
| `clash` | Clash / Mihomo YAML（含 group + MATCH 规则） | Clash Verge / 手机 Mihomo |
| `singbox` | sing-box JSON（outbounds + selector） | sing-box 手机/路由 |
| `surge` | `name = http, host, port, username=…` | Surge / Shadowrocket |
| `env` | `export HTTP_PROXY=…` + PowerShell | 临时给某台机器 |

`?alive=1` 会先做 TCP 探测，只导出连得上的（探测结果同时写回代理池的 `ok/failures`）。

**sub2api 的 `proxies[]` 形状**（与 `cpa_to_sub2api` 的 `ProxyConfig.to_dict()` 对齐，
`proxy_key` 是它的去重键，必须带）：

```json
{
  "exported_at": "2026-11-26T13:15:26Z",
  "proxies": [
    {
      "proxy_key": "http|1.2.3.4|8080|u|p",
      "name": "proxyscrape-1",
      "protocol": "http",
      "host": "1.2.3.4",
      "port": 8080,
      "status": "active",
      "username": "u",
      "password": "p"
    }
  ],
  "accounts": []
}
```

- `accounts` 恒为空数组：我们只导代理，导入后不会动 sub2api 里已有的账号。
- 无认证的代理**不写** `username` / `password` 字段（写了会被当成"要认证"）。
- 冷却中的代理导成 `status: "inactive"`，sub2api 会先跳过它，等我们下一次导出再恢复。
- `sub2api-txt` 与 `cpa` 就是纯 URL 行，直接粘进对应工具的代理输入框。

```bash
# 预览 / 存文件 / 只导活的
node src/proxyscrape-cli.mjs export-format --format clash
node src/proxyscrape-cli.mjs export-format --format singbox --out out/singbox.json
node src/proxyscrape-cli.mjs export-format --format surge --alive --out out/surge.conf
```

**B. 订阅链接（设备端定时回拉）**

设备上不方便填管理口令，所以给一条只读令牌 URL：

```bash
node src/proxyscrape-cli.mjs subscription add --name phone --format clash
# {
#   "url": "http://<网关公网地址>/proxy/64oItZI1mEqR…",
#   "format": "clash"
# }
node src/proxyscrape-cli.mjs subscription list
node src/proxyscrape-cli.mjs subscription revoke 64oItZI1mEqR…
```

- `GET /proxy/<token>` —— **不需要** `sk-dcg-` 也不需要管理口令；只能读到代理列表，
  读不到账号、Key 或任何管理数据。
- `?format=clash|singbox|surge|url|raw|at|json|csv|env` 可以在同一令牌上换形状
  （手机用 clash、路由用 singbox，一条令牌就够）。
- 响应带 `Profile-Update-Interval: 6`（小时）和 `X-Proxy-Count`，Clash / sing-box
  的订阅会按这个间隔回拉。
- 令牌可撤销（`DELETE /api/admin/proxies/shares/<token|name>`）；同名再次 `add`
  会**复用**原令牌，方便"重新生成链接"而不惊动已配置的设备。
- 令牌表落盘 `data/proxies/shares.json`（0600），记录 `hits` / `lastUsedAt` /
  `lastFormat`，用来判断某台设备还在不在拉。

> 安全边界：`/proxy/<token>` 是**公开面**。令牌本身是 24 字节随机串，但一旦泄露
> 等于把代理出口给了别人。要对外暴露就把网关放到 HTTPS 后面，并给每条设备单独
> 一条令牌（撤销时只影响那一台）。


## 6. 闭环位置

```
注册(tools/proxyscrape-reg) ──▶ /api/admin/proxyscrape/pool/import ──▶ proxyscrape-pool.json
                                             │                                 │
                                             │                                 ├─ proxies[]（去重）
                                             │                                 │
                                             └─ applyProxies() ──▶ data/proxies/<line>.txt
                                                                                   │
其它注册机启动时（未显式给 --proxy）◀──────────────────────────────────────────────┘
```

## 7. 验证

```bash
node scripts/check-loop.mjs                              # 这条线在矩阵里应当全 ✅（反代列 —）
node --test tests/proxyscrape-pool.test.mjs              # 池 + 解析 + 分片 + 分发
node --test tests/proxyscrape-routes.test.mjs            # 管理面 + 分发 + 不冲突
python -m pytest tools/proxyscrape-reg/tests -q          # 协议/代理解析/号池
```
