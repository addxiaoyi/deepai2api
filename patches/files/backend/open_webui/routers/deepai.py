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
from fastapi import APIRouter, Depends, HTTPException, Request, Response
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


async def gw_raw(path: str, timeout: int = 60):
    """Binary/text passthrough (log downloads)."""
    base = await get_gateway_url()
    token = await admin_headers_token()
    if not token:
        raise GatewayError(400, "还没配置网关管理口令。")
    try:
        async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=timeout)) as session:
            async with session.get(
                f"{base}{path}", headers={"Authorization": f"Bearer {token}", "Accept": "*/*"}
            ) as response:
                body = await response.read()
                if response.status >= 400:
                    raise GatewayError(response.status, body.decode("utf-8", "replace")[:300])
                return body, response.headers.get("content-type", "text/plain; charset=utf-8")
    except aiohttp.ClientConnectorError as exc:
        raise GatewayError(503, f"连不上 DeepAI 网关（{base}）。") from exc


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


@router.get("/register/log")
async def register_log(
    level: str = "",
    limit: int = 120,
    download: int = 0,
    user=Depends(get_admin_user),
):
    """Filtered log tail; `download=1` returns a text file instead of JSON."""
    query = f"?level={level}&limit={limit}" + ("&download=1" if download else "")
    if download:
        body, content_type = await gw_raw(f"/api/admin/register/log{query}")
        filename = f"deepai-register-{level or 'all'}.log"
        return Response(
            content=body,
            media_type=content_type,
            headers={"Content-Disposition": f'attachment; filename="{filename}"'},
        )
    return await gw(f"/api/admin/register/log{query}")


@router.get("/register/domains")
async def register_domains(force: int = 0, user=Depends(get_admin_user)):
    return await gw(f"/api/admin/register/domains?force={1 if force else 0}", timeout=40)


# ------------------------------------------------------------------- poe pool
#
# Poe accounts are cookie jars with their own life cycle, kept in a pool file
# next to (not inside) the DeepAI account list. These routes are a thin proxy.


class PoeRegisterForm(BaseModel):
    count: int = 1
    engine: str = "auto"
    concurrency: int = 1
    mail_provider: str = "tempmail_lol"
    mail_domains: list[str] = []
    sms_provider: str = "off"
    headless: bool = True


@router.get("/poe/accounts")
async def poe_accounts(limit: int = 100, user=Depends(get_admin_user)):
    return await gw(f"/api/poe/accounts?limit={limit}", timeout=30)


@router.delete("/poe/accounts/{account_id}")
async def poe_account_delete(account_id: str, user=Depends(get_admin_user)):
    return await gw(f"/api/poe/accounts/{account_id}", method="DELETE", timeout=30)


@router.post("/poe/accounts/status")
async def poe_account_status(form_data: dict, user=Depends(get_admin_user)):
    return await gw("/api/poe/accounts/status", method="POST", json_body=form_data, timeout=30)


@router.get("/poe/register")
async def poe_register_status(user=Depends(get_admin_user)):
    return await gw("/api/poe/register", timeout=30)


@router.post("/poe/register")
async def poe_register_start(form_data: PoeRegisterForm, user=Depends(get_admin_user)):
    body = {
        "count": form_data.count,
        "engine": form_data.engine,
        "concurrency": form_data.concurrency,
        "mailProvider": form_data.mail_provider,
        "mailDomains": form_data.mail_domains,
        "smsProvider": form_data.sms_provider,
        "headless": form_data.headless,
    }
    return await gw("/api/poe/register", method="POST", json_body=body, timeout=30)


@router.post("/poe/register/stop")
async def poe_register_stop(user=Depends(get_admin_user)):
    return await gw("/api/poe/register/stop", method="POST", timeout=30)


@router.get("/poe/register/log")
async def poe_register_log(level: str = "", limit: int = 120, download: int = 0, user=Depends(get_admin_user)):
    query = f"?level={level}&limit={limit}" + ("&download=1" if download else "")
    if download:
        body, content_type = await gw_raw(f"/api/poe/register/log{query}")
        return Response(
            content=body,
            media_type=content_type,
            headers={"Content-Disposition": f'attachment; filename="poe-register-{level or "all"}.log"'},
        )
    return await gw(f"/api/poe/register/log{query}")


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


# --------------------------------------------------------------- providers
#
# 通用 provider 面（网关的 /api/admin/providers/*）。目前面板只用到 moonai，
# 但接口按 provider id 透传，加新 provider 不用改这里。
#
# 与上面的 DeepAI / Poe 路由互不影响：前缀是 /providers，状态在各自的号池文件里。


class ProviderRegisterForm(BaseModel):
    """批量注册参数；字段名对齐网关的 ProviderRegistrations.start()。"""

    count: int = 2
    concurrency: int = 2
    engine: str = "auto"               # auto | protocol | browser | hybrid
    browser_stack: str = "auto"        # auto | camoufox | patchright | playwright
    mail_provider: str = "imap"        # imap | domain | mailtm | fixed
    mail_domains: list[str] = []
    email: str = ""
    proxies: list[str] = []
    cdp: str = ""
    headful: bool = False
    humanize: float = 0.0
    keys_per_account: int = 1
    export: str = ""                   # "" | jsonl | csv | pool
    mint_keys: bool = True
    push: bool = True
    verify_email: bool = True
    anti_ban: dict = {}


class ProviderImportForm(BaseModel):
    account: dict
    mint_key: bool = True


class ProviderHealthForm(BaseModel):
    ok: bool = True
    error: Optional[str] = None
    cooldown_ms: int = 0


@router.get("/providers")
async def providers_list(user=Depends(get_admin_user)):
    return await gw("/api/admin/providers", timeout=30)


@router.get("/providers/{provider_id}")
async def provider_detail(provider_id: str, user=Depends(get_admin_user)):
    return await gw(f"/api/admin/providers/{provider_id}", timeout=30)


@router.get("/providers/{provider_id}/register")
async def provider_register_status(provider_id: str, user=Depends(get_admin_user)):
    return await gw(f"/api/admin/providers/{provider_id}/register", timeout=30)


@router.post("/providers/{provider_id}/register")
async def provider_register_start(
    provider_id: str, form_data: ProviderRegisterForm, user=Depends(get_admin_user)
):
    body = {
        "count": form_data.count,
        "concurrency": form_data.concurrency,
        "engine": form_data.engine,
        "browserStack": form_data.browser_stack,
        "mailProvider": form_data.mail_provider,
        "mailDomains": form_data.mail_domains,
        "email": form_data.email,
        "proxies": form_data.proxies,
        "cdp": form_data.cdp,
        "headful": form_data.headful,
        "humanize": form_data.humanize,
        "keysPerAccount": form_data.keys_per_account,
        "export": form_data.export,
        "mintKeys": form_data.mint_keys,
        "push": form_data.push,
        "verifyEmail": form_data.verify_email,
        "antiBan": form_data.anti_ban,
    }
    return await gw(f"/api/admin/providers/{provider_id}/register", method="POST", json_body=body, timeout=30)


@router.post("/providers/{provider_id}/register/stop")
async def provider_register_stop(provider_id: str, user=Depends(get_admin_user)):
    return await gw(f"/api/admin/providers/{provider_id}/register/stop", method="POST", timeout=30)


@router.get("/providers/{provider_id}/pool")
async def provider_pool(provider_id: str, limit: int = 200, user=Depends(get_admin_user)):
    return await gw(f"/api/admin/providers/{provider_id}/pool?limit={limit}", timeout=30)


@router.post("/providers/{provider_id}/pool/import")
async def provider_pool_import(
    provider_id: str, form_data: ProviderImportForm, user=Depends(get_admin_user)
):
    return await gw(
        f"/api/admin/providers/{provider_id}/pool/import",
        method="POST",
        json_body={"account": form_data.account, "mintKey": form_data.mint_key},
        timeout=30,
    )


@router.post("/providers/{provider_id}/pool/{account_id}/health")
async def provider_pool_health(
    provider_id: str, account_id: str, form_data: ProviderHealthForm, user=Depends(get_admin_user)
):
    return await gw(
        f"/api/admin/providers/{provider_id}/pool/{account_id}/health",
        method="POST",
        json_body={"ok": form_data.ok, "error": form_data.error, "cooldownMs": form_data.cooldown_ms},
        timeout=30,
    )


@router.delete("/providers/{provider_id}/pool/{account_id}")
async def provider_pool_delete(provider_id: str, account_id: str, user=Depends(get_admin_user)):
    return await gw(f"/api/admin/providers/{provider_id}/pool/{account_id}", method="DELETE", timeout=30)


@router.post("/providers/{provider_id}/pool/export")
async def provider_pool_export(
    provider_id: str, format: str = "jsonl", download: int = 0, user=Depends(get_admin_user)
):
    """导出账号 + 面板 Key + 网关 Key。`download=1` 直接下发文件。"""
    query = f"?format={format}" + ("&download=1" if download else "")
    if download:
        body, content_type = await gw_raw(f"/api/admin/providers/{provider_id}/pool/export{query}")
        filename = f"{provider_id}-accounts.{'csv' if format == 'csv' else 'jsonl' if format == 'jsonl' else 'json'}"
        return Response(
            content=body,
            media_type=content_type,
            headers={"Content-Disposition": f'attachment; filename="{filename}"'},
        )
    return await gw(f"/api/admin/providers/{provider_id}/pool/export{query}", method="POST", timeout=30)


@router.get("/providers/{provider_id}/keys")
async def provider_keys(provider_id: str, user=Depends(get_admin_user)):
    return await gw(f"/api/admin/providers/{provider_id}/keys", timeout=30)


@router.post("/providers/{provider_id}/probe")
async def provider_probe(provider_id: str, user=Depends(get_admin_user)):
    return await gw(f"/api/admin/providers/{provider_id}/probe", method="POST", timeout=90)


@router.get("/providers/{provider_id}/connect")
async def provider_connect_info(provider_id: str, user=Depends(get_admin_user)):
    """
    面板"接入编程工具"用的只读信息：OpenAI 兼容前缀 + 一把可用网关 Key 的前缀。
    明文 Key 不在这里返回（只在创建/导入时一次性可见）。
    """
    base = await get_gateway_url()
    keys = await gw("/api/admin/keys", timeout=30)
    rows = keys.get("keys") if isinstance(keys, dict) else None
    usable = [k for k in (rows or []) if not k.get("revoked")]
    return {
        "provider": provider_id,
        "openai_base": f"{base}/p/{provider_id}/v1",
        "anthropic_base": f"{base}/p/{provider_id}",
        "key_prefix": (usable[0].get("prefix") if usable else "") or "",
        "keys": len(usable),
    }


# ------------------------------------------------------- 号池总览 + 两条新线
#
# 网关现在有六条线：DeepAI（state）、Allmodels、Poe、Provider 运行时
# （wavespeed/novita/moonai/zo/blackbox）、Clever AI Hub、Gab AI。
# 下面三组路由把它们接进面板：
#   GET /pools                   一次性拿到所有线的号池统计（总览卡片）
#   /cleverai/*                  Clever AI Hub（Firebase + App Check）
#   /gabai/*                     Gab AI（gab_ key，直通反代）
#
# 全部只是转发：号池文件、注册锁、job 状态都在网关侧，面板不持有任何凭据。


class CleveraiRegisterForm(BaseModel):
    count: int = 1
    concurrency: int = 1
    engine: str = "auto"  # auto | protocol | browser
    browser_stack: str = "auto"
    mail_provider: str = "imap"
    mail_domains: list[str] = []
    appcheck: str = "browser"  # browser | capsolver | manual
    captcha_key: Optional[str] = None
    appcheck_token: Optional[str] = None
    chat_engine: Optional[str] = None
    proxies: list[str] = []
    cdp: Optional[str] = None
    headful: bool = False
    humanize: float = 0
    verify_email: bool = True
    warmup: bool = True
    export: Optional[str] = None


class GabaiRegisterForm(BaseModel):
    count: int = 1
    concurrency: int = 1
    engine: str = "auto"
    browser_stack: str = "auto"
    mail_provider: str = "imap"
    mail_domains: list[str] = []
    email: Optional[str] = None
    captcha: str = "none"
    captcha_key: Optional[str] = None
    proxies: list[str] = []
    cdp: Optional[str] = None
    headful: bool = False
    humanize: float = 0
    verify_email: bool = False
    create_api_key: bool = True
    export: Optional[str] = None


class PoolImportForm(BaseModel):
    account: dict
    mint_key: bool = True


@router.get("/pools")
async def pools_overview(user=Depends(get_admin_user)):
    """六条线的号池总览：一次请求，面板只画一张表。"""
    health = await gw("/health", timeout=20)
    lines = [
        {
            "id": "deepai",
            "label": "DeepAI Code",
            "accounts": health.get("accounts", 0),
            "healthy": health.get("healthy_accounts", 0),
            "keys": health.get("keys", 0),
            "proxy_base": "/v1",
            "extra": {"is_pro": (health.get("account") or {}).get("isPro")},
        }
    ]
    allmodels = health.get("allmodels") or {}
    lines.append(
        {
            "id": "allmodels",
            "label": "Allmodels",
            "accounts": allmodels.get("accounts", 0),
            "healthy": allmodels.get("healthy", 0),
            "keys": allmodels.get("keys", 0),
            "proxy_base": "",
        }
    )
    poe = health.get("poe") or {}
    lines.append(
        {
            "id": "poe",
            "label": "Poe",
            "accounts": poe.get("total", 0),
            "healthy": poe.get("active", 0),
            "keys": 0,
            "proxy_base": "",
            "extra": {
                "email_verified": poe.get("email_verified", 0),
                "phone_required": poe.get("phone_required", 0),
                "dead": poe.get("dead", 0),
            },
        }
    )
    for pid, stat in (health.get("providers") or {}).items():
        lines.append(
            {
                "id": pid,
                "label": pid,
                "accounts": stat.get("accounts", 0),
                "healthy": stat.get("healthy", 0),
                "keys": stat.get("keys", 0),
                "proxy_base": f"/p/{pid}/v1",
                "running": stat.get("running", False),
            }
        )
    cleverai = health.get("cleverai") or {}
    lines.append(
        {
            "id": "cleverai",
            "label": "Clever AI Hub",
            "accounts": cleverai.get("accounts", 0),
            "healthy": cleverai.get("healthy", 0),
            "keys": cleverai.get("keys", 0),
            "proxy_base": "/cleverai/v1",
            "extra": {"app_check": cleverai.get("appCheck"), "credits": cleverai.get("credits", 0)},
        }
    )
    gabai = health.get("gabai") or {}
    lines.append(
        {
            "id": "gabai",
            "label": "Gab AI",
            "accounts": gabai.get("accounts", 0),
            "healthy": gabai.get("healthy", 0),
            "keys": gabai.get("keys", 0),
            "proxy_base": "/gab/v1",
        }
    )
    jevai = health.get("jevai") or {}
    lines.append(
        {
            "id": "jevai",
            "label": "JEV AI",
            "accounts": jevai.get("accounts", 0),
            "healthy": jevai.get("healthy", 0),
            "keys": jevai.get("keys", 0),
            "proxy_base": "/jevai/v1",
        }
    )
    proxyscrape = health.get("proxyscrape") or {}
    lines.append(
        {
            "id": "proxyscrape",
            "label": "ProxyScrape 代理池",
            "accounts": proxyscrape.get("accounts", 0),
            # 这条线的"健康"就是可用代理数
            "healthy": proxyscrape.get("activeProxies", 0),
            "keys": 0,
            # 产物是出口，没有反代面
            "proxy_base": "",
            "extra": {"proxies": proxyscrape.get("proxies", 0)},
        }
    )
    return {"gateway": await get_gateway_url(), "lines": lines}


# ---------------------------------------------------------------- Clever AI


@router.get("/cleverai/status")
async def cleverai_status(user=Depends(get_admin_user)):
    return await gw("/api/admin/cleverai/status", timeout=30)


@router.post("/cleverai/register")
async def cleverai_register(form_data: CleveraiRegisterForm, user=Depends(get_admin_user)):
    body = {
        "count": form_data.count,
        "concurrency": form_data.concurrency,
        "engine": form_data.engine,
        "browserStack": form_data.browser_stack,
        "mailProvider": form_data.mail_provider,
        "mailDomains": form_data.mail_domains,
        "appcheck": form_data.appcheck,
        "captchaKey": form_data.captcha_key,
        "appcheckToken": form_data.appcheck_token,
        "chatEngine": form_data.chat_engine,
        "proxies": form_data.proxies,
        "cdp": form_data.cdp,
        "headful": form_data.headful,
        "humanize": form_data.humanize,
        "verifyEmail": form_data.verify_email,
        "warmup": form_data.warmup,
        "export": form_data.export,
        "push": True,
    }
    return await gw("/api/admin/cleverai/register", method="POST", json_body=body, timeout=30)


@router.post("/cleverai/register/stop")
async def cleverai_register_stop(user=Depends(get_admin_user)):
    return await gw("/api/admin/cleverai/register/stop", method="POST", timeout=30)


@router.get("/cleverai/pool")
async def cleverai_pool(limit: int = 200, user=Depends(get_admin_user)):
    return await gw(f"/api/admin/cleverai/pool?limit={limit}", timeout=30)


@router.post("/cleverai/pool/import")
async def cleverai_pool_import(form_data: PoolImportForm, user=Depends(get_admin_user)):
    body = dict(form_data.account)
    body["mintKey"] = form_data.mint_key
    return await gw("/api/admin/cleverai/pool/import", method="POST", json_body=body, timeout=30)


@router.post("/cleverai/pool/export")
async def cleverai_pool_export(
    format: str = "jsonl", download: int = 0, user=Depends(get_admin_user)
):
    query = f"?format={format}" + ("&download=1" if download else "")
    if download:
        body, content_type = await gw_raw(f"/api/admin/cleverai/pool/export{query}")
        filename = f"cleverai-accounts.{'csv' if format == 'csv' else 'jsonl' if format == 'jsonl' else 'json'}"
        return Response(
            content=body,
            media_type=content_type,
            headers={"Content-Disposition": f'attachment; filename="{filename}"'},
        )
    return await gw(f"/api/admin/cleverai/pool/export{query}", method="POST", timeout=30)


@router.delete("/cleverai/pool/{account_id}")
async def cleverai_pool_delete(account_id: str, user=Depends(get_admin_user)):
    return await gw(f"/api/admin/cleverai/pool/{account_id}", method="DELETE", timeout=30)


@router.get("/cleverai/keys")
async def cleverai_keys(user=Depends(get_admin_user)):
    return await gw("/api/admin/cleverai/keys", timeout=30)


@router.post("/cleverai/appcheck")
async def cleverai_appcheck(user=Depends(get_admin_user)):
    """强制续期一枚 App Check 令牌（会开一次浏览器，约 5–15 s）。"""
    return await gw("/api/admin/cleverai/appcheck", method="POST", timeout=180)


@router.get("/cleverai/connect")
async def cleverai_connect_info(user=Depends(get_admin_user)):
    base = await get_gateway_url()
    keys = await gw("/api/admin/keys", timeout=30)
    rows = keys.get("keys") if isinstance(keys, dict) else None
    usable = [k for k in (rows or []) if not k.get("revoked")]
    return {
        "openai_base": f"{base}/cleverai/v1",
        "anthropic_base": f"{base}/cleverai",
        "key_prefix": (usable[0].get("prefix") if usable else "") or "",
        "keys": len(usable),
        "notice_header": "X-Cleverai-Notice",
    }


# ------------------------------------------------------------------- Gab AI


@router.get("/gabai/status")
async def gabai_status(user=Depends(get_admin_user)):
    return await gw("/api/admin/gabai/status", timeout=30)


@router.post("/gabai/register")
async def gabai_register(form_data: GabaiRegisterForm, user=Depends(get_admin_user)):
    body = {
        "count": form_data.count,
        "concurrency": form_data.concurrency,
        "engine": form_data.engine,
        "browserStack": form_data.browser_stack,
        "mailProvider": form_data.mail_provider,
        "mailDomains": form_data.mail_domains,
        "email": form_data.email,
        "captcha": form_data.captcha,
        "captchaKey": form_data.captcha_key,
        "proxies": form_data.proxies,
        "cdp": form_data.cdp,
        "headful": form_data.headful,
        "humanize": form_data.humanize,
        "verifyEmail": form_data.verify_email,
        "createApiKey": form_data.create_api_key,
        "export": form_data.export,
        "push": True,
    }
    return await gw("/api/admin/gabai/register", method="POST", json_body=body, timeout=30)


@router.post("/gabai/register/stop")
async def gabai_register_stop(user=Depends(get_admin_user)):
    return await gw("/api/admin/gabai/register/stop", method="POST", timeout=30)


@router.get("/gabai/pool")
async def gabai_pool(limit: int = 200, user=Depends(get_admin_user)):
    return await gw(f"/api/admin/gabai/pool?limit={limit}", timeout=30)


@router.post("/gabai/pool/import")
async def gabai_pool_import(form_data: PoolImportForm, user=Depends(get_admin_user)):
    body = dict(form_data.account)
    body["mintKey"] = form_data.mint_key
    return await gw("/api/admin/gabai/pool/import", method="POST", json_body=body, timeout=30)


@router.post("/gabai/pool/{account_id}/mint")
async def gabai_pool_mint(account_id: str, user=Depends(get_admin_user)):
    """网页反代出 apikey：用账号登录态在 gab.ai 上补建一把 `gab_…` key。"""
    return await gw(f"/api/admin/gabai/pool/{account_id}/mint", method="POST", timeout=90)


@router.post("/gabai/pool/export")
async def gabai_pool_export(
    format: str = "jsonl", download: int = 0, user=Depends(get_admin_user)
):
    query = f"?format={format}" + ("&download=1" if download else "")
    if download:
        body, content_type = await gw_raw(f"/api/admin/gabai/pool/export{query}")
        filename = f"gabai-accounts.{'csv' if format == 'csv' else 'jsonl' if format == 'jsonl' else 'json'}"
        return Response(
            content=body,
            media_type=content_type,
            headers={"Content-Disposition": f'attachment; filename="{filename}"'},
        )
    return await gw(f"/api/admin/gabai/pool/export{query}", method="POST", timeout=30)


@router.delete("/gabai/pool/{account_id}")
async def gabai_pool_delete(account_id: str, user=Depends(get_admin_user)):
    return await gw(f"/api/admin/gabai/pool/{account_id}", method="DELETE", timeout=30)


@router.get("/gabai/connect")
async def gabai_connect_info(user=Depends(get_admin_user)):
    base = await get_gateway_url()
    keys = await gw("/api/admin/keys", timeout=30)
    rows = keys.get("keys") if isinstance(keys, dict) else None
    usable = [k for k in (rows or []) if not k.get("revoked")]
    return {
        "openai_base": f"{base}/gab/v1",
        "anthropic_base": f"{base}/gab",
        "key_prefix": (usable[0].get("prefix") if usable else "") or "",
        "keys": len(usable),
    }


# --------------------------------------------------------- proxyscrape 代理池
#
# 产物是**出口**而不是模型通道：注册领 premium 试用 → 试用给出代理 → 写进代理池
# → `applyProxies()` 分发成 data/proxies/<line>.txt，别的注册机自动吃。
# 所以这一组没有反代面，只有管理面 + 分发。


class ProxyscrapeRegisterForm(BaseModel):
    count: int = 1
    concurrency: int = 1
    mail_provider: str = "imap"
    mail_domains: list[str] = []
    captcha: str = "browser"  # browser | capsolver | manual | none
    captcha_key: Optional[str] = None
    engine: str = "auto"
    browser_stack: str = "auto"
    proxies: list[str] = []
    headful: bool = False
    verify_email: bool = True
    claim_trial: bool = True
    export: Optional[str] = None


@router.get("/proxyscrape/status")
async def proxyscrape_status(user=Depends(get_admin_user)):
    return await gw("/api/admin/proxyscrape/status", timeout=30)


@router.post("/proxyscrape/register")
async def proxyscrape_register(form_data: ProxyscrapeRegisterForm, user=Depends(get_admin_user)):
    body = {
        "count": form_data.count,
        "concurrency": form_data.concurrency,
        "mailProvider": form_data.mail_provider,
        "mailDomains": form_data.mail_domains,
        "captcha": form_data.captcha,
        "captchaKey": form_data.captcha_key,
        "engine": form_data.engine,
        "browserStack": form_data.browser_stack,
        "proxies": form_data.proxies,
        "headful": form_data.headful,
        "verifyEmail": form_data.verify_email,
        "claimTrial": form_data.claim_trial,
        "export": form_data.export,
    }
    return await gw("/api/admin/proxyscrape/register", method="POST", json_body=body, timeout=30)


@router.post("/proxyscrape/register/stop")
async def proxyscrape_register_stop(user=Depends(get_admin_user)):
    return await gw("/api/admin/proxyscrape/register/stop", method="POST", timeout=30)


@router.get("/proxyscrape/pool")
async def proxyscrape_pool(limit: int = 200, user=Depends(get_admin_user)):
    return await gw(f"/api/admin/proxyscrape/pool?limit={limit}", timeout=30)


@router.get("/proxyscrape/proxies")
async def proxyscrape_proxies(user=Depends(get_admin_user)):
    return await gw("/api/admin/proxyscrape/proxies", timeout=30)


@router.post("/proxyscrape/pool/export")
async def proxyscrape_pool_export(
    format: str = "proxies", download: int = 0, user=Depends(get_admin_user)
):
    query = f"?format={format}" + ("&download=1" if download else "")
    if download:
        body, content_type = await gw_raw(f"/api/admin/proxyscrape/pool/export{query}")
        return Response(
            content=body,
            media_type=content_type,
            headers={"Content-Disposition": f'attachment; filename="proxyscrape.{format}"'},
        )
    return await gw(f"/api/admin/proxyscrape/pool/export{query}", method="POST", timeout=30)


@router.get("/proxies")
async def proxy_share_status(user=Depends(get_admin_user)):
    """出口分发状态：每条线现在分到几条代理。"""
    return await gw("/api/admin/proxies", timeout=30)


@router.post("/proxies/apply")
async def proxy_share_apply(form_data: dict, user=Depends(get_admin_user)):
    """把代理池分发给注册机（可指定 lines / limitPerLine）。"""
    return await gw("/api/admin/proxies/apply", method="POST", json_body=form_data or {}, timeout=30)

# --------------------------------------------------- 代理导出 / 订阅（给别的设备）


class ProxyShareForm(BaseModel):
    name: str = ""
    format: str = "url"
    note: str = ""


@router.get("/proxies/formats")
async def proxy_formats(user=Depends(get_admin_user)):
    """可用的导出格式（面板下拉用）。"""
    return await gw("/api/admin/proxies/formats", timeout=20)


@router.get("/proxies/export")
async def proxy_export(
    format: str = "url",
    alive: int = 0,
    limit: int = 0,
    download: int = 0,
    user=Depends(get_admin_user),
):
    """把代理池渲染成给别的设备用的形状；`download=1` 直接下发文件。"""
    query = f"?format={format}&alive={alive}&limit={limit}" + ("&download=1" if download else "")
    if download:
        body, content_type = await gw_raw(f"/api/admin/proxies/download{query}")
        return Response(
            content=body,
            media_type=content_type,
            headers={"Content-Disposition": f'attachment; filename="proxyscrape-{format}"'},
        )
    return await gw(f"/api/admin/proxies/export{query}", timeout=60)


@router.post("/proxies/check")
async def proxy_check(form_data: dict, user=Depends(get_admin_user)):
    """TCP 探测一遍，死的进冷却。"""
    return await gw("/api/admin/proxies/check", method="POST", json_body=form_data or {}, timeout=120)


@router.get("/proxies/shares")
async def proxy_shares(user=Depends(get_admin_user)):
    return await gw("/api/admin/proxies/shares", timeout=20)


@router.post("/proxies/share")
async def proxy_share_mint(form_data: ProxyShareForm, user=Depends(get_admin_user)):
    """生成/复用一条订阅链接（手机、路由器、别的电脑直接用）。"""
    return await gw(
        "/api/admin/proxies/share",
        method="POST",
        json_body={"name": form_data.name, "format": form_data.format, "note": form_data.note},
        timeout=20,
    )


@router.delete("/proxies/shares/{token}")
async def proxy_share_revoke(token: str, purge: int = 0, user=Depends(get_admin_user)):
    return await gw(
        f"/api/admin/proxies/shares/{token}?purge={purge}", method="DELETE", timeout=20
    )

# ------------------------------------------------------ jev-ai.org Key 池


class JevaiRegisterForm(BaseModel):
    count: int = 1
    concurrency: int = 1
    mail_provider: str = "imap"
    mail_domains: list[str] = []
    key_name: str = ""
    proxies: list[str] = []
    verify_email: bool = True
    checkin: bool = True
    export: Optional[str] = None


@router.get("/jevai/status")
async def jevai_status(user=Depends(get_admin_user)):
    return await gw("/api/admin/jevai/status", timeout=30)


@router.post("/jevai/register")
async def jevai_register(form_data: JevaiRegisterForm, user=Depends(get_admin_user)):
    body = {
        "count": form_data.count,
        "concurrency": form_data.concurrency,
        "mailProvider": form_data.mail_provider,
        "mailDomains": form_data.mail_domains,
        "keyName": form_data.key_name,
        "proxies": form_data.proxies,
        "verifyEmail": form_data.verify_email,
        "checkin": form_data.checkin,
        "export": form_data.export,
    }
    return await gw("/api/admin/jevai/register", method="POST", json_body=body, timeout=30)


@router.post("/jevai/register/stop")
async def jevai_register_stop(user=Depends(get_admin_user)):
    return await gw("/api/admin/jevai/register/stop", method="POST", timeout=30)


@router.get("/jevai/pool")
async def jevai_pool(limit: int = 200, user=Depends(get_admin_user)):
    return await gw(f"/api/admin/jevai/pool?limit={limit}", timeout=30)


@router.get("/jevai/keys")
async def jevai_keys(user=Depends(get_admin_user)):
    return await gw("/api/admin/jevai/keys", timeout=30)


@router.post("/jevai/apply")
async def jevai_apply(user=Depends(get_admin_user)):
    """写 data/keys/jevai.{txt,json}，其它工具/线据此接入。"""
    return await gw("/api/admin/jevai/apply", method="POST", timeout=60)


@router.get("/jevai/connect")
async def jevai_connect(user=Depends(get_admin_user)):
    return await gw("/api/admin/jevai/connect", timeout=30)


@router.post("/jevai/pool/export")
async def jevai_pool_export(format: str = "keys", download: int = 0, user=Depends(get_admin_user)):
    query = f"?format={format}" + ("&download=1" if download else "")
    if download:
        body, content_type = await gw_raw(f"/api/admin/jevai/pool/export{query}")
        return Response(
            content=body,
            media_type=content_type,
            headers={"Content-Disposition": f'attachment; filename="jevai-keys.{format}"'},
        )
    return await gw(f"/api/admin/jevai/pool/export{query}", method="POST", timeout=30)

@router.post("/jevai/checkin")
async def jevai_checkin(form_data: Optional[dict] = None, user=Depends(get_admin_user)):
    """给池里每个账号签到（jev 的免费额度主要靠这个）。"""
    return await gw("/api/admin/jevai/checkin", method="POST", json_body=form_data or {}, timeout=30)

# ---------------------------------------------- 六条 OpenAI 兼容线的通用管理面
#
# 一条路径服务六条线：/lines/{line}/{action}。这样面板侧边栏里每条线都有自己的页面，
# 而后端只有这一份转发代码。


class LineRegisterForm(BaseModel):
    count: int = 1
    concurrency: int = 1
    mail_provider: str = "imap"
    mail_domains: list[str] = []
    key_name: str = ""
    proxies: list[str] = []
    verify_email: bool = True
    export: Optional[str] = None


class LinePoolImportForm(BaseModel):
    email: str
    apiKey: str = ""
    password: str = ""
    name: str = ""
    emailVerified: Optional[bool] = None
    credits: dict = {}
    mintKey: bool = True


@router.get("/lines")
async def lines_overview(user=Depends(get_admin_user)):
    """侧边栏/总览：六条线各自的状态与号池计数。"""
    return await gw("/api/admin/lines", timeout=30)


@router.get("/lines/{line}/status")
async def line_status(line: str, user=Depends(get_admin_user)):
    return await gw(f"/api/admin/{line}/status", timeout=30)


@router.post("/lines/{line}/register")
async def line_register(line: str, form_data: LineRegisterForm, user=Depends(get_admin_user)):
    body = {
        "count": form_data.count,
        "concurrency": form_data.concurrency,
        "mailProvider": form_data.mail_provider,
        "mailDomains": form_data.mail_domains,
        "keyName": form_data.key_name,
        "proxies": form_data.proxies,
        "verifyEmail": form_data.verify_email,
        "export": form_data.export,
    }
    return await gw(f"/api/admin/{line}/register", method="POST", json_body=body, timeout=30)


@router.post("/lines/{line}/register/stop")
async def line_register_stop(line: str, user=Depends(get_admin_user)):
    return await gw(f"/api/admin/{line}/register/stop", method="POST", timeout=30)


@router.get("/lines/{line}/pool")
async def line_pool(line: str, limit: int = 200, user=Depends(get_admin_user)):
    return await gw(f"/api/admin/{line}/pool?limit={limit}", timeout=30)


@router.post("/lines/{line}/pool/import")
async def line_pool_import(line: str, form_data: LinePoolImportForm, user=Depends(get_admin_user)):
    return await gw(
        f"/api/admin/{line}/pool/import",
        method="POST",
        json_body=form_data.model_dump(),
        timeout=30,
    )


@router.post("/lines/{line}/pool/export")
async def line_pool_export(
    line: str, format: str = "keys", download: int = 0, user=Depends(get_admin_user)
):
    query = f"?format={format}" + ("&download=1" if download else "")
    if download:
        body, content_type = await gw_raw(f"/api/admin/{line}/pool/export{query}")
        return Response(
            content=body,
            media_type=content_type,
            headers={"Content-Disposition": f'attachment; filename="{line}-keys.txt"'},
        )
    return await gw(f"/api/admin/{line}/pool/export{query}", method="POST", timeout=30)


@router.get("/lines/{line}/keys")
async def line_keys(line: str, user=Depends(get_admin_user)):
    return await gw(f"/api/admin/{line}/keys", timeout=30)


@router.post("/lines/{line}/apply")
async def line_apply(line: str, user=Depends(get_admin_user)):
    return await gw(f"/api/admin/{line}/apply", method="POST", timeout=60)


@router.get("/lines/{line}/connect")
async def line_connect(line: str, user=Depends(get_admin_user)):
    return await gw(f"/api/admin/{line}/connect", timeout=30)


@router.delete("/lines/{line}/pool/{account_id}")
async def line_account_delete(line: str, account_id: str, user=Depends(get_admin_user)):
    return await gw(f"/api/admin/{line}/pool/{account_id}", method="DELETE", timeout=30)

# ------------------------------------------------------ AMD 开发者云（GPU）

class AmdPoolImportForm(BaseModel):
    email: str
    apiKey: str = ""
    credits: dict = {}


class AmdStudyForm(BaseModel):
    email: Optional[str] = None
    course: Optional[str] = None
    step: int = 30
    target: int = 3600
    pause: float = 1.0
    maxCalls: int = 400
    limit: int = 0
    force: bool = False


@router.get("/amd/status")
async def amd_status(user=Depends(get_admin_user)):
    return await gw("/api/admin/amd/status", timeout=30)


@router.get("/amd/pool")
async def amd_pool(limit: int = 200, user=Depends(get_admin_user)):
    return await gw(f"/api/admin/amd/pool?limit={limit}", timeout=30)


@router.post("/amd/pool/import")
async def amd_pool_import(form_data: AmdPoolImportForm, user=Depends(get_admin_user)):
    """导入浏览器登录后的 cookie（这条线没有自动注册：AMD 账号要手机号 + 人工审批）。"""
    return await gw("/api/admin/amd/pool/import", method="POST", json_body=form_data.model_dump(), timeout=30)


@router.get("/amd/courses")
async def amd_courses(email: str = "", user=Depends(get_admin_user)):
    """学院课程列表 + 我们的学习进度。"""
    query = f"?email={email}" if email else ""
    return await gw(f"/api/admin/amd/courses{query}", timeout=90)


@router.post("/amd/study")
async def amd_study(form_data: AmdStudyForm, user=Depends(get_admin_user)):
    """自动刷课：把课程刷到目标进度，攒 Devpoints 换 GPU 小时。"""
    return await gw("/api/admin/amd/study", method="POST", json_body=form_data.model_dump(), timeout=30)


@router.post("/amd/study/stop")
async def amd_study_stop(user=Depends(get_admin_user)):
    return await gw("/api/admin/amd/register/stop", method="POST", timeout=30)


@router.post("/amd/apply")
async def amd_apply(user=Depends(get_admin_user)):
    return await gw("/api/admin/amd/apply", method="POST", timeout=30)

class AmdExamForm(BaseModel):
    course: str
    email: Optional[str] = None
    child: Optional[str] = None
    auto: bool = True
    submit: bool = False
    strategy: str = "first"
    rounds: int = 3


@router.post("/amd/exam")
async def amd_exam(form_data: AmdExamForm, user=Depends(get_admin_user)):
    """课程考试：--auto 会自动交卷、捡正确答案、回填重交。"""
    return await gw("/api/admin/amd/exam", method="POST", json_body=form_data.model_dump(), timeout=30)

class AmdVouchersForm(BaseModel):
    email: Optional[str] = None


class AmdExchangeForm(BaseModel):
    email: Optional[str] = None
    amount: int = 1
    dryRun: bool = False
    extra: Optional[dict] = None


class AmdConsolidateForm(BaseModel):
    master: Optional[str] = None
    redeem: bool = False
    amount: int = 1


@router.post("/amd/vouchers")
async def amd_vouchers(form_data: AmdVouchersForm, user=Depends(get_admin_user)):
    """算力券汇总：每个号换到多少小时、兑换码、合计。"""
    return await gw("/api/admin/amd/vouchers", method="POST", json_body=form_data.model_dump(), timeout=30)


@router.post("/amd/exchange")
async def amd_exchange(form_data: AmdExchangeForm, user=Depends(get_admin_user)):
    return await gw("/api/admin/amd/exchange", method="POST", json_body=form_data.model_dump(), timeout=30)


@router.post("/amd/models")
async def amd_models(user=Depends(get_admin_user)):
    """免费模型（Token Factory）接入信息：base url + 计费项。"""
    return await gw("/api/admin/amd/models", method="POST", json_body={}, timeout=60)


@router.post("/amd/consolidate")
async def amd_consolidate(form_data: AmdConsolidateForm, user=Depends(get_admin_user)):
    """把多号算力券汇总到主号（可选在主号上集中兑换）。"""
    return await gw("/api/admin/amd/consolidate", method="POST", json_body=form_data.model_dump(), timeout=60)
