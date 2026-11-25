"""
DeepAI Code provider for Open WebUI (secondary development).

This router is the fork's addition: it lets an administrator link a DeepAI
account with OAuth, mint gateway API keys, and wire this gateway into Open
WebUI's OpenAI connection list — all from Open WebUI's own admin settings, with
no separate dashboard.

The gateway (../, a Node service) owns the OAuth flow and the keys; this router
is a thin, authenticated proxy in front of it plus the Open WebUI side effects.
"""

from __future__ import annotations

import logging
from typing import Optional

import aiohttp
from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel

from open_webui.models.config import Config
from open_webui.routers.openai import clear_models_cache
from open_webui.utils.auth import get_admin_user

log = logging.getLogger(__name__)

router = APIRouter()

DEEPAI_CONFIG_KEY = "deepai.gateway_url"
DEFAULT_GATEWAY_URL = "http://127.0.0.1:8787"
DEEPAI_PROVIDER_TAG = "deepai-code"


# ---------------------------------------------------------------- gateway i/o


async def get_gateway_url() -> str:
    """Gateway base URL: admin setting first, then env, then loopback default."""
    import os

    stored = await Config.get(DEEPAI_CONFIG_KEY)
    if isinstance(stored, str) and stored.strip():
        return stored.rstrip("/")
    return os.environ.get("DEEPAI_GATEWAY_URL", DEFAULT_GATEWAY_URL).rstrip("/")


class GatewayError(HTTPException):
    def __init__(self, status_code: int, detail: str):
        super().__init__(status_code=status_code, detail=detail)


async def gateway_request(method: str, path: str, *, json_body=None, token: Optional[str] = None, timeout: int = 60):
    base = await get_gateway_url()
    url = f"{base}{path}"
    headers = {"Accept": "application/json", "X-Admin": "1"}
    if json_body is not None:
        headers["Content-Type"] = "application/json"
    if token:
        headers["Authorization"] = f"Bearer {token}"

    try:
        async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=timeout)) as session:
            async with session.request(method, url, json=json_body, headers=headers) as response:
                text = await response.text()
                if response.status >= 500:
                    raise GatewayError(502, f"网关返回 {response.status}：{text[:300]}")
                if response.status == 401:
                    raise GatewayError(401, "网关管理口令未通过；请检查 DEEPAI_GATEWAY_TOKEN。")
                if response.status >= 400:
                    raise GatewayError(response.status, text[:300] or f"HTTP {response.status}")
                try:
                    return await response.json(content_type=None)
                except Exception:
                    return {"raw": text}
    except aiohttp.ClientConnectorError as exc:
        raise GatewayError(
            503,
            f"连不上 DeepAI 网关（{base}）。先启动它：node src/index.mjs —— {exc}",
        ) from exc
    except aiohttp.ServerTimeoutError as exc:
        raise GatewayError(504, f"网关超时：{exc}") from exc


# ------------------------------------------------------------------- admin auth
#
# The gateway itself is protected by an admin token. Open WebUI stores it so the
# operator only types it once, on this page.
ADMIN_TOKEN_KEY = "deepai.admin_token"


async def admin_headers_token() -> Optional[str]:
    import os

    stored = await Config.get(ADMIN_TOKEN_KEY)
    if isinstance(stored, str) and stored.strip():
        return stored.strip()
    return os.environ.get("DEEPAI_GATEWAY_TOKEN")


async def gw(path: str, method: str = "GET", json_body=None, timeout: int = 60):
    token = await admin_headers_token()
    if not token:
        raise GatewayError(
            400,
            "还没配置网关管理口令。在下方填入（网关启动时打印的那个 token），或设置环境变量 DEEPAI_GATEWAY_TOKEN。",
        )
    return await gateway_request(method, path, json_body=json_body, token=token, timeout=timeout)


# --------------------------------------------------------------------- models


class SettingsForm(BaseModel):
    gateway_url: Optional[str] = None
    admin_token: Optional[str] = None


class KeyForm(BaseModel):
    label: Optional[str] = None
    rpm: Optional[int] = 0
    daily_requests: Optional[int] = 0


class ExchangeForm(BaseModel):
    code: str
    state: Optional[str] = None


class ConnectForm(BaseModel):
    key: Optional[str] = None


class RegisterForm(BaseModel):
    """Bulk signup request; mirrors the gateway's POST /api/admin/register."""

    count: int = 2
    engine: str = "auto"
    concurrency: int = 1
    mail_provider: str = "tempmail_lol"
    mail_domains: list[str] = []
    proxies: list[str] = []
    verify_email: bool = False
    headless: bool = True


# --------------------------------------------------------------------- routes


async def ensure_connection(request: Request, api_key: Optional[str] = None) -> Optional[str]:
    """
    Make sure the gateway is registered as an [OI] connection, minting a key if
    needed. Called by /status so the models appear without the operator having to
    press "connect" first — that button only re-syncs after a manual change.
    """
    base = await get_gateway_url()
    api_base = f"{base}/v1"

    values = await Config.get_many("openai.api_base_urls", "openai.api_keys", "openai.api_configs")
    urls = list(values.get("openai.api_base_urls") or [])
    keys = list(values.get("openai.api_keys") or [])
    configs = dict(values.get("openai.api_configs") or {})

    if api_base in urls:
        index = urls.index(api_base)
        current = keys[index] if index < len(keys) else ""
        if current and not api_key:
            return None  # already wired and working
    else:
        index = len(urls)

    if not api_key:
        created = await gw("/api/admin/keys", method="POST", json_body={"label": "openwebui"})
        api_key = created.get("key")
        if not api_key:
            return None

    while len(keys) <= index:
        keys.append("")
    keys[index] = api_key
    if api_base not in urls:
        urls.append(api_base)
    configs[str(index)] = {"enable": True, "tags": [DEEPAI_PROVIDER_TAG]}

    await Config.upsert(
        {
            "openai.enable": True,
            "openai.api_base_urls": urls,
            "openai.api_keys": keys,
            "openai.api_configs": configs,
        }
    )
    await clear_models_cache(request)
    log.info("DeepAI gateway auto-connected to Open WebUI at %s", api_base)
    return api_base


@router.get("/status")
async def get_status(request: Request, user=Depends(get_admin_user)):
    """Everything the settings page needs, in one round trip."""
    base = await get_gateway_url()
    has_token = bool(await admin_headers_token())

    health = None
    state = None
    error = None
    try:
        health = await gateway_request("GET", "/health")
    except HTTPException as exc:
        error = exc.detail

    connected = False
    if has_token and health:
        try:
            state = await gw("/api/admin/state", timeout=90)
        except HTTPException as exc:
            state = None
            if not error:
                error = exc.detail

    if state and (state.get("accounts") or []):
        # Auto-wire on first view so models show up without a manual click.
        try:
            connected = (await ensure_connection(request)) is not None
        except HTTPException as exc:
            log.warning("auto-connect failed: %s", exc.detail)

    return {
        "gateway_url": base,
        "has_admin_token": has_token,
        "reachable": health is not None,
        "connected": connected,
        "error": error,
        "health": health,
        "state": state,
    }


@router.post("/settings")
async def update_settings(form_data: SettingsForm, user=Depends(get_admin_user)):
    updates = {}
    if form_data.gateway_url is not None:
        updates[DEEPAI_CONFIG_KEY] = form_data.gateway_url.rstrip("/")
    if form_data.admin_token is not None:
        updates[ADMIN_TOKEN_KEY] = form_data.admin_token.strip()
    if updates:
        await Config.upsert(updates)
    return {
        "gateway_url": await get_gateway_url(),
        "has_admin_token": bool(await admin_headers_token()),
    }


@router.post("/login/start")
async def start_login(user=Depends(get_admin_user)):
    """Open the DeepAI authorize URL; the gateway finishes the callback itself."""
    return await gw("/api/admin/login-start", method="POST")


@router.post("/login/exchange")
async def exchange_login(form_data: ExchangeForm, user=Depends(get_admin_user)):
    """Fallback for operators whose browser cannot reach the gateway's loopback."""
    result = await gw("/api/admin/exchange", method="POST", json_body={"code": form_data.code, "state": form_data.state})
    return result


@router.post("/accounts/{account_id}/remove")
async def remove_account(account_id: str, user=Depends(get_admin_user)):
    return await gw(f"/api/admin/accounts/{account_id}", method="DELETE")


# ------------------------------------------------------------------- signup
#
# Bulk registration is a Python worker next to the gateway (tools/deepai-reg).
# The gateway owns the child process; this router only starts it, reports on it
# and can stop it. Created accounts land in the pool through /api/admin/accounts.


@router.get("/register")
async def register_status(user=Depends(get_admin_user)):
    return await gw("/api/admin/register", timeout=30)


@router.post("/register")
async def register_start(form_data: RegisterForm, user=Depends(get_admin_user)):
    body = {
        "count": form_data.count,
        "engine": form_data.engine,
        "concurrency": form_data.concurrency,
        "mailProvider": form_data.mail_provider,
        "mailDomains": form_data.mail_domains,
        "proxies": form_data.proxies,
        "verifyEmail": form_data.verify_email,
        "headless": form_data.headless,
    }
    return await gw("/api/admin/register", method="POST", json_body=body, timeout=30)


@router.post("/register/stop")
async def register_stop(user=Depends(get_admin_user)):
    return await gw("/api/admin/register/stop", method="POST", timeout=30)


# ------------------------------------------------------------------ api keys


@router.get("/keys")
async def list_keys(user=Depends(get_admin_user)):
    return await gw("/api/admin/keys")


@router.post("/keys")
async def create_key(form_data: KeyForm, user=Depends(get_admin_user)):
    body = {"label": form_data.label or "openwebui", "limits": {"rpm": form_data.rpm or 0, "dailyRequests": form_data.daily_requests or 0}}
    return await gw("/api/admin/keys", method="POST", json_body=body)


@router.post("/keys/{key_id}/revoke")
async def revoke_key(key_id: str, user=Depends(get_admin_user)):
    return await gw(f"/api/admin/keys/{key_id}/revoke", method="POST")


@router.delete("/keys/{key_id}")
async def delete_key(key_id: str, user=Depends(get_admin_user)):
    return await gw(f"/api/admin/keys/{key_id}/delete", method="POST")


# ------------------------------------------------------------------ open webui


@router.post("/connect")
async def connect_to_openwebui(request: Request, form_data: ConnectForm, user=Depends(get_admin_user)):
    """
    Register the gateway as an OpenAI-compatible connection in Open WebUI so the
    DeepAI models show up in the model picker without any manual setup.
    """
    api_key = form_data.key
    if not api_key:
        created = await gw("/api/admin/keys", method="POST", json_body={"label": "openwebui"})
        api_key = created.get("key")
        if not api_key:
            raise GatewayError(502, "网关没有返回 API Key。")

    api_base = await ensure_connection(request, api_key=api_key)
    values = await Config.get_many("openai.api_base_urls")
    urls = list(values.get("openai.api_base_urls") or [])
    return {"api_base": api_base or f"{await get_gateway_url()}/v1", "key_prefix": api_key[:12] + "…", "connections": len(urls)}


@router.get("/models")
async def list_models(user=Depends(get_admin_user)):
    """Model catalog straight from the gateway (price info included)."""
    state = await gw("/api/admin/state", timeout=90)
    return {"models": state.get("models") or []}
