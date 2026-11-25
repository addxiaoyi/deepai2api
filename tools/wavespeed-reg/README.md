# WaveSpeedAI 注册 worker（`wavespeed-reg`）

把 WaveSpeedAI 账号做成可批量、可复现、可入池的流水线。

- 代码：`tools/wavespeed-reg/wavespeed_reg/`
- 数据：`out/accounts.sqlite3`（可用 `-o` 改）
- 号池推送：`POST /api/admin/providers/wavespeed/pool/import`

## 1. 站点事实（决定架构）

| 项 | 事实 | 影响 |
| --- | --- | --- |
| 注册方式 | **只有 OAuth**：Google / GitHub；AWS 需审核 | 没有邮箱密码表单，"注册"= 点 OAuth + 领 key |
| 注册入口 | `https://wavespeed.ai/sign-in`（`/sign-up` 不存在） | 协议腿无法从零建号 |
| API Key | `/accesskey` 页 UI 点 **Generate**，明文只显示一次 | 没有公开的"创建 key"接口 |
| LLM 推理 | `https://llm.wavespeed.ai/v1`，**OpenAI 兼容** | 反代层可直接转发 |
| 生成推理 | `https://api.wavespeed.ai/api/v3`，**自有异步任务** | 需独立的 async adapter |
| 鉴权 | `Authorization: Bearer <KEY>` | 统一 |
| 免费额度 | 约 $1 试用，按资格发放 | 不能当作必然 |

因此 worker 提供三条腿，产出的记录形状完全一致：

- `browser` —— 指纹浏览器点 OAuth → 落 `/dashboard` → 去 `/accesskey` 生成并读明文
- `session` —— 导入已登录的 cookie jar，用协议读 `/accesskey`
- `manual` —— 别人已生成的 key，只做校验（`import-keys` / `verify`）

## 2. 快速开始

```bash
cd tools/wavespeed-reg

# 环境自检（面板可达性、OAuth 按钮、浏览器内核、邮箱）
python -m wavespeed_reg doctor

# 看面板端点与 Turnstile sitekey（从页面挖，挖不到返回空）
python -m wavespeed_reg probe

# 只校验一把 key，不注册、不消耗额度
python -m wavespeed_reg verify --key sk_xxx

# 批量注册（OAuth 需要真人或已登录 profile）
python -m wavespeed_reg run -n 5 -c 1 \
  --cdp http://127.0.0.1:50325 \
  --mail-provider imap \
  --mail-domains your-domain.com \
  --key-policy '{"oauth_provider":"google"}' \
  --json

# 导出成网关能直接吃的形状
python -m wavespeed_reg export --format pool --file out/pool.json
```

### 两条 OAuth 接法

1. **接管已登录的指纹浏览器**（推荐）：

   ```bash
   python -m wavespeed_reg run -n 10 --cdp http://127.0.0.1:50325
   ```

   AdsPower / BitBrowser / Morelogin 都会开一个本地 CDP 端口；那边profile 已登录
   Google/GitHub，脚本只负责点按钮读文本，不碰凭据。

2. **脚本替人填第三方表单**：

   ```bash
   python -m wavespeed_reg run -n 3 --key-policy '{
     "oauth_provider": "github",
     "oauth_credentials": {"email": "you@example.com", "password": "..."}
   }'
   ```

   GitHub 相对宽松；Google 对自动化登录拦截很严，通常走不通。

## 3. 域名邮箱

四个 provider，按耐用程度排序：

| provider | 说明 |
| --- | --- |
| `imap` / `domain` | **自有 catch-all 域名**，IMAPS 轮询，最耐用 |
| `fixed` | 一个自己控制的地址，按 plus 分号给每个账号 |
| `mailtm` | 公共 API，盒子约 7 天 |
| `tempmail_lol` | 公共 API，随机域名 |

自有域名配置（`config.yaml` 或环境变量）：

```yaml
mail:
  provider: imap
  domain: your-domain.com
  domains: [your-domain.com, your-domain2.com]
  imap_host: imap.your-domain.com
  imap_user: catchall@your-domain.com
  imap_password: "********"
  verify_timeout: 180
```

```bash
NOVITA_IMAP_HOST / WAVESPEED_IMAP_HOST
WAVESPEED_IMAP_USER / WAVESPEED_IMAP_PASSWORD
```

## 4. 防封策略（`anti_ban`）

全部参数可调，默认值偏向"慢但稳"。命令行用 JSON 覆盖：

```bash
python -m wavespeed_reg run -n 20 --anti-ban '{
  "domain_rpm": 2,
  "ip_concurrency": 1,
  "jitter_ms": [2000, 6000],
  "domain_health_floor": 0.4,
  "reuse_session": true
}'
```

| 参数 | 默认 | 作用 |
| --- | --- | --- |
| `domain_rpm` / `domain_rph` | 3 / 40 | 单域名每分钟 / 每小时建号上限 |
| `domain_health_floor` | 0.25 | 健康分低于此值暂停该域名 |
| `ip_concurrency` | 1 | **同一出口 IP 同时只跑 1 个**（最有效的一条） |
| `proxy_rotate` | true | 每账号轮换代理 |
| `jitter_ms` | [1200, 4200] | 全局抖动，避免固定节奏 |
| `geo_align` | true | 时区/语言跟随代理出口国家 |
| `fresh_profile` | true | 每账号全新浏览器上下文 |
| `reuse_session` | true | 复用 cookie jar，减少重复 OAuth |
| `backoff` | 见下 | 按失败类型退避（秒） |

失败退避表：

```python
{
  "rate_limited": [20, 45],
  "blocked": [20, 40],
  "captcha_required": [15, 30],
  "verification_pending": [5, 10],
  "key_not_obtainable": [0, 0],   # 换账号，不等待
  "email_taken": [0, 0],
  "email_rejected": [0, 0],
  "oauth_unavailable": [0, 0],
}
```

## 5. 失败分类

| kind | 含义 | 处置 |
| --- | --- | --- |
| `email_taken` | 地址已注册 | 换地址 |
| `email_rejected` | 地址被拒 | 换域名（降权该域名） |
| `blocked` | Cloudflare / WAF | 换引擎 + 退避 |
| `captcha_required` | Turnstile 拒绝 | 换引擎 + 退避 |
| `key_not_obtainable` | 拿不到 key 明文 | **换账号**（同会话再点更可疑） |
| `oauth_unavailable` | OAuth 走不通 | 换账号 |
| `verification_pending` | 没收到验证信 | 短退避 |
| `rate_limited` | 限流 | 退避 + 换代理 |
| `no_api_key` | 账号有、key 没有 | 换引擎 |

## 6. 推理接口用法

### LLM（OpenAI 兼容）

```python
from wavespeed_reg.protocol import WaveSpeed

with WaveSpeed() as ws:
    response = ws.chat(key, {
        "model": "deepseek/deepseek-v4-flash",
        "messages": [{"role": "user", "content": "hi"}],
        "stream": False,
    })
    print(response.json())
```

### 生成（异步任务）

```python
with WaveSpeed() as ws:
    result = ws.generate(
        key,
        "wavespeed-ai/z-image/turbo",
        {"prompt": "a cat", "num_images": 1},
        poll=2.0,     # 起步 2s，逐步放宽到 10s
        timeout=300,
    )
    print(result["data"]["outputs"])
```

三条操作铁律（代码里已强制）：

1. **POST 不自动重试** —— 提交即计费，重试会产生重复任务；
2. **body 里的 `code` 可能与 HTTP 200 不一致** —— 必须解析 body；
3. **轮询地址由服务端下发**（`data.urls.get`），不要硬编码拼接。

## 7. 测试

```bash
cd tools/wavespeed-reg
python tests/test_offline.py     # 无 pytest 也能跑
# 或
python -m pytest tests/ -q
```

覆盖：错误分类映射、退避表、域名速率窗与健康分、异步任务 poll 与终态、
POST 不重试、OAuth-only 语义、key 校验、指纹自洽性、出口 IP 串行化。

## 8. 限制

- **OAuth 是硬门槛**：没有 Google/GitHub 的可用登录态就无法建号。这不是实现缺陷，
  是站点事实。
- **key 明文只显示一次**：读到后立刻入库；重复点 Generate 只会让风控更敏感。
- **没有创建 key 的 HTTP 接口**：协议腿只能读元信息，拿明文必须走浏览器。
