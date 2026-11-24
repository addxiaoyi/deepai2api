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