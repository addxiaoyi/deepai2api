# DeepAI Code provider — Open WebUI fork notes

This checkout is **Open WebUI 0.13.4 upstream plus a DeepAI provider**. It is not
a plugin: the provider is first-class, so an administrator manages the whole
"subscription → API key" flow inside Open WebUI's normal admin settings.

Everything below is the complete diff against upstream. If you `git pull` a new
upstream release, re-apply these five files.

## What was added

| File | Status | Purpose |
| --- | --- | --- |
| `backend/open_webui/routers/deepai.py` | new | Authenticated proxy to the DeepAI gateway: status, OAuth start/exchange, account pool, API keys, and "connect to Open WebUI" |
| `src/lib/apis/deepai/index.ts` | new | Frontend API client |
| `src/lib/components/admin/Settings/DeepAI.svelte` | new | The settings page (gateway, account pool, keys, models) |
| `src/lib/components/admin/Settings/MoonAI.svelte` | new | MoonAI provider card (own pool / register / probe / export / coding-tool base URLs), mounted at the bottom of the same settings page |
| `backend/open_webui/main.py` | +2 lines | `deepai` in the routers import list; `app.include_router(deepai.router, prefix='/api/v1/deepai', tags=['deepai'])` |
| `src/lib/components/chat/SettingsModal.svelte` | +4 blocks | Import the page, add `admin:deepai` to the tab list and its group, render the panel |
| `src/lib/components/admin/Settings/AdminTabIcon.svelte` | +2 lines | Icon for the new tab (`GlobeAlt`) |

## Architecture

```
Open WebUI (this fork)              DeepAI gateway (../..)        api.deepai.org
┌────────────────────────┐         ┌────────────────────┐        ┌───────────────┐
│ 设置 → DeepAI          │─ HTTP ─▶│ /api/admin/*       │───────▶│ /deepai_code/*│
│  /api/v1/deepai/*      │         │ OAuth + keys + 调度 │        │  托管模型通道   │
│  OpenAI 连接 (自动写入) │◀───────────────────────────────┘        └───────────────┘
└────────────────────────┘         └────────────────────┘
```

The fork never speaks the DeepAI lane itself. It only:
1. proxies admin calls to the gateway (typed, admin-only),
2. and writes the gateway's `/v1` base URL + a minted key into Open WebUI's
   OpenAI connection config (`openai.api_base_urls` / `openai.api_keys`), so the
   models appear in the picker like any other OpenAI provider.

## Settings needed

Either set the environment variables on the Open WebUI container/process:

```bash
DEEPAI_GATEWAY_URL=http://host.docker.internal:8787   # gateway base, no /v1
DEEPAI_GATEWAY_TOKEN=<the token the gateway prints>    # gateway admin token
```

or fill them in **Settings → Admin → DeepAI** (they are persisted through
Open WebUI's `Config`, so the env vars are only a fallback).

> The gateway must be reachable from the Open WebUI process. In Docker that means
> starting the gateway with `--host 0.0.0.0`; `127.0.0.1` inside the container is
> the container itself.

## Endpoints added by the fork

All require an Open WebUI **admin** session (`get_admin_user`).

| Route | Purpose |
| --- | --- |
| `GET /api/v1/deepai/status` | Gateway reachability + accounts + keys + catalog + allowance |
| `POST /api/v1/deepai/settings` | Save gateway URL / admin token |
| `POST /api/v1/deepai/login/start` | Get the DeepAI authorize URL (browser opens it) |
| `POST /api/v1/deepai/login/exchange` | Manual `code` paste fallback |
| `POST /api/v1/deepai/accounts/{id}/remove` | Drop an account from the pool |
| `GET /api/v1/deepai/keys` · `POST /api/v1/deepai/keys` | List / mint gateway keys (with `rpm`, `daily_requests`) |
| `POST /api/v1/deepai/keys/{id}/revoke` · `DELETE /api/v1/deepai/keys/{id}` | Revoke / delete |
| `POST /api/v1/deepai/connect` | Mint a key and register the gateway as an OpenAI connection |
| `GET /api/v1/deepai/models` | Catalog with per-request prices |
| `GET /api/v1/deepai/register` | Bulk-signup job: status, counters, **progress/ETA**, failure reasons, last 240 log lines |
| `POST /api/v1/deepai/register` | Start a job (`count`, `engine`, `concurrency`, `mail_provider`, `mail_domains`, `proxies`, `retry_rounds`) |
| `POST /api/v1/deepai/register/stop` | Kill the running job (whole process tree on Windows) |
| `GET /api/v1/deepai/register/log` | Log tail filtered by `level`, or `download=1` for a `.log` attachment |
| `GET /api/v1/deepai/register/domains` | Domain scorecard from the worker's SQLite (30 s cache) |
| `GET /api/v1/deepai/providers` | All providers (wavespeed / novita / moonai) with pool stats + job snapshot |
| `GET /api/v1/deepai/providers/{id}` | One provider: endpoints, python, worker presence, pool stats |
| `GET/POST /api/v1/deepai/providers/{id}/register` | Start / inspect that provider's bulk signup |
| `POST /api/v1/deepai/providers/{id}/register/stop` | Stop it (whole process tree on Windows) |
| `GET /api/v1/deepai/providers/{id}/pool` | Masked pool view (tokens report expiry only) |
| `POST /api/v1/deepai/providers/{id}/pool/import` | Worker push / manual import (idempotent by apiKey then email) |
| `POST /api/v1/deepai/providers/{id}/pool/{account}/health` | Report a call result (counters + cooldown) |
| `DELETE /api/v1/deepai/providers/{id}/pool/{account}` | Remove an account |
| `POST /api/v1/deepai/providers/{id}/pool/export` | Export `jsonl` / `csv` / `pool`, `download=1` for a file |
| `GET /api/v1/deepai/providers/{id}/keys` | Panel key + org + token expiry, no plaintext |
| `POST /api/v1/deepai/providers/{id}/probe` | One upstream call with a pooled token |
| `GET /api/v1/deepai/providers/{id}/connect` | OpenAI / Anthropic base URLs for coding tools |

## MoonAI card (separate provider line)

`Settings → Admin → DeepAI → MoonAI 号池` is a self-contained component
(`src/lib/components/admin/Settings/MoonAI.svelte`) mounted at the bottom of the
same settings page. It talks to the routes above with `id=moonai`.

Why a separate component rather than another block inside `DeepAI.svelte`:

* MoonAI is a **different provider shape** — upstream needs an OAuth
  `access_token` (not a panel key), has no `stream` and no `tools`, and its
  responses are `{content, finish_reason, usage}`. The translation lives in the
  gateway (`src/moonai-adapter.mjs`); the panel only shows state.
* Its pool file (`data/moonai-pool.json`), schema (`dcg.pool/moonai@1`), job file
  and lock are all `moonai`-prefixed, so the DeepAI / Allmodels / Poe pools keep
  their own writers. Starting a MoonAI batch never touches the other three.
* The card polls `GET /providers/moonai/register` every 3 s and stops polling
  once the job settles, so an idle settings page costs nothing.

What the card exposes: pool counters, python/worker status, the register form
(count / concurrency / engine / fingerprint stack / mailbox source / domains /
proxies / CDP / humanize / keys per account / export), start & stop, job progress
with failure-reason chips and a 30-line log tail, **manual import** (one JSON per
line or an array, optional gateway-key minting, the new key is shown once), the
pool table (panel key preview, token expiry, gateway key prefix, engine, call
counters, remove), pool export as a file, and the copy-paste **OpenAI Base URL**
(`/p/moonai/v1`) and **Anthropic Base** (`/p/moonai`) for Cline / Continue /
Claude Code.

### Verifying the components without vendor/

`vendor/open-webui` is not in the repo, so `npm run build` is unavailable until
`scripts/apply-fork.ps1` restores it. Two lighter checks run here:

```bash
npm run check:svelte    # 需要 svelte@4 + svelte-preprocess@5 + typescript@5
```

* `node --test tests/moonai-fork-surface.test.mjs` — route names, client exports,
  component mounting, block balance, "no plaintext token in the UI".
* `node --test tests/moonai-svelte-build.test.mjs` — compiles both `.svelte`
  files with the real compiler (skips when the toolchain is absent).

One Svelte gotcha worth remembering: an attribute written as
`placeholder='{"email":"…"}'` is parsed as an **interpolation** (`{` starts an
expression), and fails with `Expected token }`. The card keeps such JSON in a
script constant and passes `placeholder={importPlaceholder}`.



## Bulk signup panel

`Settings → Admin → DeepAI → 批量注册 · 自动入池` drives the gateway's
`tools/deepai-reg` worker:

```
WebUI panel ──POST /register──▶ deepai.py ──POST /api/admin/register──▶ gateway
                                                                        │ spawn
                                                                        ▼
                                              python -m deepai_reg run --json --push-url …
                                                                        │ NDJSON events
   panel ◀──GET /register (2.5 s poll)── deepai.py ◀──job state──────────┤
                                                                        │
        account pool ◀──POST /api/admin/accounts (one per new account)──┘
```

* The worker emits NDJSON on stdout (`start` / `attempt` / `account` / `log` /
  `done`); the gateway keeps the last 240 lines and hands them to the panel.
* Each `account` event is pushed straight back into `POST /api/admin/accounts`,
  which calls `store.upsertAccount` and `lane.resetCatalog()` — the scheduler
  and the model catalog refresh exactly like a manual OAuth link.
* `mail_domains` is a rotation pool. The worker also *learns*: a domain that
  produced an account is kept, one DeepAI refused (`Please use your own email
  address.`) is parked in `mail_domains` inside the worker's SQLite, and the
  next run starts from the survivors.
* The job snapshot is persisted to `data/register-job.json`; a gateway restart
  shows the last run as `interrupted` instead of pretending it is still going.
* Progress is `settled / requested` where a unit settles when it is created or
  has exhausted the retry rounds; ETA is `remaining / (settled / elapsed)`.
  The panel renders it as a bar plus "预计剩余 N 分 N 秒 · x 个/分".
* Failed units get one extra pass (`--retry-rounds`, default 1) with a fresh
  persona/domain/proxy, and every give-up is written to the `failures` table
  with a `kind` (`email_rejected` / `blocked` / `rate_limited` / `no_api_key` /
  `transport` / …), aggregated for the panel and the job summary.
* Log levels are `debug|info|warn|error`; the panel filters client-side for
  instant feedback and calls `/register/log?download=1` for the export so the
  operator gets the full buffer, not the 240-line ring.

## Poe pool (separate)

Poe accounts live in `data/poe-pool.json`, never in `accounts[]`: their credential
is a `p-b` cookie and their life cycle (`email_verified` -> `phone_required` ->
`active` -> `dead`) has nothing in common with an API key. The same settings page
renders a second card that talks to these routes (proxied to the gateway):

| Route | Purpose |
| --- | --- |
| `GET /api/v1/deepai/poe/accounts` | Pool list with cookie **names** only (values never leave the gateway) |
| `DELETE /api/v1/deepai/poe/accounts/{id}` | Drop an account |
| `POST /api/v1/deepai/poe/accounts/status` | Operator override (`active` / `dead`) |
| `GET/POST /api/v1/deepai/poe/register` | Status / start (`count`, `engine`, `mail_provider`, `sms_provider`) |
| `POST /api/v1/deepai/poe/register/stop` | Stop the worker |
| `GET /api/v1/deepai/poe/register/log` | Log tail, `download=1` for a file |

The worker (`tools/poe-reg`) pushes each account to `POST /api/poe/accounts`; the
gateway masks the cookie jar in every response.

## Build

```bash
cd vendor/open-webui
npm ci
npm run build          # SvelteKit frontend
python -m build        # optional: backend wheel
```

Docker (recommended, matches the compose file at the repo root):

```bash
docker compose up -d --build
```

## Notes

- `frontend/build` is what the Python app serves; a source-only checkout needs
  `npm run build` before the new page is visible.
- The page is admin-only, like every other provider setting.
- Upstream's own `OPENAI_API_BASE_URL` env var also works — the DeepAI page just
  automates it and adds account/key management.
