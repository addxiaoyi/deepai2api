# 部署手册

三种部署形态，按你的环境选一种：

| 场景 | 用哪种 | 一条命令 |
| --- | --- | --- |
| 本机试用（Windows / macOS） | 直接 `node` + 可选 Docker 跑 WebUI | `npm install && node src/index.mjs` |
| 一台 Linux 服务器，要面板 | Docker Compose（网关 + Open WebUI fork） | `docker compose up -d --build` |
| 一台 Linux 服务器，无人值守 | `deploy/install.sh`（systemd + venv + 可选 Docker） | `sudo bash deploy/install.sh --with-docker` |

部署完的统一验收：`npm run loop` + `curl /health` + 面板「设置 → Admin → DeepAI」。

---

## 1. 本机（Windows / macOS）

```powershell
git clone https://github.com/addxiaoyi/deepai2api.git
cd deepai2api
npm install

# 网关（只提供 API，界面在 Open WebUI）
node src/index.mjs --port 8787            # 首次启动会打印管理口令，记下来
node src/index.mjs --show-admin-token     # 忘了就再打一次
```

想同时要面板（Open WebUI fork）：

```powershell
.\scripts\apply-fork.ps1      # 克隆 Open WebUI 0.13.4 + 打补丁 + 构建前端（首次几分钟）
docker compose up -d --build  # 面板 http://127.0.0.1:3000
```

注册机是 Python，装一次就够：

```powershell
python -m venv .venv
.\.venv\Scripts\pip install -r tools/deepai-reg/requirements.txt
.\.venv\Scripts\pip install -r tools/cleverai-reg/requirements.txt
# 指纹浏览器（可选，只有 browser 引擎 / App Check 取令牌要用）
.\.venv\Scripts\pip install "camoufox[geoip]"
.\.venv\Scripts\python -m camoufox fetch
```

---

## 2. Docker Compose（网关 + 面板）

`docker-compose.yml` 里两个服务：`gateway`（Node，8787）与 `webui`（Open WebUI fork，3000）。
网关的数据目录挂出来，否则重启就丢号池。

```bash
cp .env.example .env
# 必改：DEEPAI_GATEWAY_TOKEN（强口令）
# 面板在容器里，访问宿主机网关不能用 127.0.0.1：
#   DEEPAI_GATEWAY_URL=http://host.docker.internal:8787
docker compose up -d --build
docker compose logs -f gateway
```

要点：

- **面板连网关**：容器内的 `DEEPAI_GATEWAY_URL` 必须是 `host.docker.internal`（Docker Desktop）
  或 compose 服务名（Linux 下用 `http://gateway:8787`，compose 已配好网络别名时）。
- **端口**：`8787` 只绑 `127.0.0.1` 时宿主机外的工具连不上；要对外就 `--host 0.0.0.0`
  并且**必须**设管理口令，否则网关拒绝启动。
- **数据**：`./data` → 容器 `/data`，里面有 `gateway-state.json` 与所有 `*-pool.json`，
  都是明文凭据，目录权限保持 `700`。
- 面板首次打开：**设置 → Admin → DeepAI** 里填网关地址 + 管理口令，保存后
  「接入并启用模型」会把网关写成 Open WebUI 的 OpenAI 连接。

---

## 3. Linux systemd（install.sh）

```bash
# 直接装（从 GitHub 拉当前 main）
curl -fsSL https://raw.githubusercontent.com/addxiaoyi/deepai2api/main/deploy/install.sh | sudo bash

# 或指定目录 / 端口，并一起装面板
sudo bash deploy/install.sh --dir /opt/deepai2api --port 8787 --with-docker

# 顺便把 Camoufox 浏览器拉下来（指纹浏览器引擎 + Clever AI 的 App Check）
sudo bash deploy/install.sh --with-camoufox
```

它做的事：

1. 装 `git / python3(+venv) / nodejs`，按需装 `docker`；
2. 复制或 clone 仓库到 `/opt/deepai2api`；
3. `npm ci --omit=dev`，建 `/opt/deepai2api/.venv`，**逐个**安装 `tools/*-reg/requirements.txt`
   （某条线的依赖装不上只 warn，不拦整体）；
4. 写 `/etc/deepai2api/env`（`0600`，管理口令随机生成）；
5. 装 `deepai-gateway.service`，`--with-docker` 时再装 `deepai-webui.service`。

幂等：重跑保留原口令；要换口令加 `--rotate-token`。

常用运维：

```bash
systemctl status deepai-gateway
journalctl -u deepai-gateway -f
sudo systemctl restart deepai-gateway
cat /etc/deepai2api/env            # 管理口令在这里
```

---

## 4. 环境变量矩阵（按线）

`deploy/deepai.env.example` 与仓库根的 `.env.example` 是同一套变量，前者给 systemd 用。

| 变量 | 作用 | 默认 |
| --- | --- | --- |
| `DEEPAI_GATEWAY_HOST` / `_PORT` | 监听地址 | `127.0.0.1` / `8787` |
| `DEEPAI_GATEWAY_TOKEN` | 管理口令（admin API / 面板） | 首次启动生成 |
| `DEEPAI_GATEWAY_DATA` | 数据目录（所有号池文件） | `./data` |
| `DEEPAI_WEBUI_URL` | 面板地址，`/` 会跳过去 | 空 |
| `DEEPAI_PYTHON` / `DEEPAI_REGISTER_DIR` | DeepAI worker | `python` / `tools/deepai-reg` |
| `ALLMODELS_*` | Allmodels 线（`_PYTHON` / `_REGISTER_DIR` / `_IMAP_*` / `_CAPTCHA_*`） | — |
| `POE_*` | Poe 线（`_PYTHON` / `_REGISTER_DIR` / `_SMS_PROVIDER` / `_SMS_API_KEY`） | — |
| `DCG_PROVIDERS` | 只装哪几个 provider（逗号分隔） | 全装 |
| `DCG_PROVIDER_ENDPOINTS` | 覆盖 provider 上游地址（JSON，测试/自建反代用） | 空 |
| `CLEVERAI_*` | Clever AI 线（`_PYTHON` / `_REGISTER_DIR` / `_APPCHECK_PROVIDER` / `_CAPTCHA_KEY` / `_IMAP_*`） | — |
| `GABAI_*` | Gab AI 线（`_PYTHON` / `_REGISTER_DIR` / `_CAPTCHA_*`） | — |
| `HTTPS_PROXY` / `NO_PROXY` | 出口代理 | 空 |

> 注意：每台 worker 只认自己前缀的变量。`DEEPAI_PYTHON` 不会重定向 Poe / Clever AI 的
> worker，反之亦然 —— 这是刻意的，避免一条线配错把别的线带歪。

### 各线的额外依赖

| 线 | 必须 | 可选（决定能力上限） |
| --- | --- | --- |
| DeepAI | 已登录的桌面端凭据 | — |
| Allmodels | `httpx` | `playwright`（Turnstile 浏览器腿）、capsolver key |
| Poe | `httpx` | 接码平台（过 `phone_required`） |
| Clever AI | `httpx` + **一枚 App Check 令牌**（浏览器或 capsolver） | `camoufox`（取令牌 + 浏览器腿）、catch-all IMAP |
| Gab AI | `httpx` | capsolver（Turnstile） |
| Provider 运行时 | `httpx` | `camoufox`（moonai/zo 的浏览器腿） |

---

## 5. 面板（Open WebUI fork）里能看到什么

装好 fork 后，**设置 → Admin → DeepAI** 一个页面里有：

| 区块 | 数据来源 |
| --- | --- |
| 网关地址 / 管理口令 / 模型 | `/api/v1/deepai/status`、`/models` |
| DeepAI 账号池 + OAuth 登录 | `/accounts`、`/login/*` |
| 批量注册（DeepAI） | `/register`、`/register/log` |
| Allmodels 号池 + 注册机 | `/api/admin/allmodels/*` |
| Poe 号池 | `/poe/*` |
| Provider 运行时（wavespeed/novita/moonai/zo/blackbox） | `/providers/*` |
| **号池总览（六条线一张表）** | `/pools` |
| **Clever AI Hub**（注册 / App Check 续期 / 号池 / 导入导出） | `/cleverai/*` |
| **Gab AI**（注册 / 补建 key / 号池 / 导入导出） | `/gabai/*` |

前四块由 `DeepAI.svelte` 与 `MoonAI.svelte` 提供；总览 + 两条新线由
`Pools.svelte` 提供（`patches/files/src/lib/components/admin/Settings/Pools.svelte`），
挂在同一个页面底部，不需要改 Open WebUI 的导航注册表。

---

## 6. 首次部署验收清单

```bash
# 1) 网关活着，六条线都在
curl -s localhost:8787/health | jq '.ok, .allmodels, .cleverai, .gabai'

# 2) 闭环没有断环（静态：文件/路由/schema/文档/离线测试/环境变量）
npm run loop

# 3) 接口层（不联网）
npm run test:loop && node tests/e2e.mjs

# 4) worker 都装好了
node -e "console.log(require('./package.json').scripts['test:workers'])"
npm run test:workers

# 5) 面板能连上网关：设置页填地址 + 口令 → 保存 → 刷新显示账号数
```

---

## 7. 安全与反向代理

- 网关**默认只绑 `127.0.0.1`**。对外必须先设 `DEEPAI_GATEWAY_TOKEN`（网关会拒绝无口令的外网绑定）。
- 下游只拿 `sk-dcg-…`；上游凭据（OAuth、面板 Key、cookie、refreshToken）永远不出网关。
- 建议前面挂一层 Nginx，只暴露 `/v1`、`/p/*`、`/cleverai/v1`、`/gab/v1`，
  `/api/admin/*` 与 `/api/poe/*` 只允许内网：

```nginx
location /v1/        { proxy_pass http://127.0.0.1:8787; proxy_buffering off; proxy_read_timeout 600s; }
location /p/         { proxy_pass http://127.0.0.1:8787; proxy_buffering off; proxy_read_timeout 600s; }
location /cleverai/  { proxy_pass http://127.0.0.1:8787; proxy_buffering off; proxy_read_timeout 600s; }
location /gab/       { proxy_pass http://127.0.0.1:8787; proxy_buffering off; proxy_read_timeout 600s; }
location /api/       { deny all; }
```

`proxy_buffering off` 是必须的：这几条线都是 SSE，缓冲会把流式回答攒着不发。

---

## 8. 备份 / 升级 / 回滚

```bash
# 备份（号池 = 明文凭据，别进 git）
tar czf deepai-backup-$(date +%F).tgz -C /var/lib/deepai2api data

# 升级
sudo git -C /opt/deepai2api pull --ff-only
sudo -u deepai npm --prefix /opt/deepai2api ci --omit=dev
sudo -u deepai /opt/deepai2api/.venv/bin/pip install -r /opt/deepai2api/tools/cleverai-reg/requirements.txt
sudo systemctl restart deepai-gateway

# 回滚：切回上一个 commit 再重启（数据目录不动）
sudo git -C /opt/deepai2api checkout <old-sha>
sudo systemctl restart deepai-gateway
```

升级不会动 `data/`。新增号池线时只会多一个新文件（`<line>-pool.json`），旧的读取方按
`version` 兼容。

---

## 9. 排障

| 现象 | 原因 / 处理 |
| --- | --- |
| 面板设置页保存后仍显示 0 个账号 | 容器里的 `DEEPAI_GATEWAY_URL` 用了 `127.0.0.1`；容器访问宿主机要用 `host.docker.internal` 或服务名 |
| 面板提示「还没配置网关管理口令」 | 设置页没填或环境变量 `DEEPAI_GATEWAY_TOKEN` 缺失；口令在网关首次启动日志里 |
| `/cleverai/v1/*` 一律 401 `APP_CHECK_REQUIRED` | App Check 令牌过期：面板点「续期 App Check」，或 `POST /api/admin/cleverai/appcheck`；需要 camoufox |
| Clever AI 注册机说「找不到 camoufox」 | `pip install "camoufox[geoip]" && python -m camoufox fetch`，或改用 `--captcha capsolver` |
| Gab AI 注册机一直 `captcha_required` | Turnstile 需要 token：`--captcha capsolver --captcha-key …`，或换干净住宅出口 |
| 流式回答卡住不动 | 反代没关缓冲：Nginx 加 `proxy_buffering off` |
| `npm run loop` 报某条线缺 worker | 该线 `tools/<line>-reg/<line>_reg/cli.py` 不存在 → 仓库没拉全，或自定义了 `*_REGISTER_DIR` 指错 |
| `docker compose` 连不上 | Docker Desktop / docker 服务没起；`docker compose logs gateway` 看具体 |
| `npm ci` 报 EBADENGINE | Node 版本过高/过低，按 `package.json` 的 `engines` 装 Node ≥20 |
