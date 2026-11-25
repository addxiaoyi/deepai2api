"""把新注册到的账号推进网关的 WaveSpeed 号池。

网关暴露 `POST /api/admin/providers/wavespeed/pool/import`；这个客户端在两种
场合用：worker 作为网关子进程跑（"批量注册"面板），或 CLI 里带
`--push-url/--push-token`。网关会给账号发一把自己的 `sk-dcg-…` Key，
所以一次调用就够。
"""

from __future__ import annotations

from typing import Any

from .errors import SignupError
from .net import build_client, request

POOL_IMPORT = "/api/admin/providers/wavespeed/pool/import"


def push_account(
    base_url: str,
    token: str,
    account: dict[str, Any],
    *,
    timeout: float = 30.0,
    mint_key: bool = True,
) -> dict[str, Any]:
    if not base_url or not token:
        return {}
    payload = {
        "email": account.get("email", ""),
        "password": account.get("password", ""),
        "name": account.get("name", ""),
        "apiKey": account.get("apiKey") or account.get("api_key", ""),
        "keyId": account.get("keyId") or account.get("key_id", ""),
        "keyPrefix": account.get("keyPrefix") or account.get("key_prefix", ""),
        "keyLast4": account.get("keyLast4") or account.get("key_last4", ""),
        "keySource": account.get("keySource") or account.get("api_key_source", ""),
        "extraKeys": account.get("extraKeys") or account.get("extra_keys") or [],
        "expireTime": account.get("expireTime") or account.get("expire_time", ""),
        "organizationId": account.get("organizationId") or account.get("organization_id", ""),
        "emailVerified": account.get("emailVerified", account.get("email_verified")),
        "oauthProvider": account.get("oauthProvider") or account.get("oauth_provider", ""),
        "domain": account.get("domain", ""),
        "cookies": account.get("cookies") or [],
        "engine": account.get("engine", "browser"),
        "createdAt": account.get("createdAt") or account.get("created_at", ""),
        "mintKey": bool(mint_key),
    }
    if not payload["apiKey"]:
        raise SignupError("account carries no apiKey; nothing to push")
    client = build_client(timeout=timeout)
    try:
        response = request(
            client,
            "POST",
            f"{base_url.rstrip('/')}{POOL_IMPORT}",
            json=payload,
            headers={"Authorization": f"Bearer {token}", "Accept": "application/json"},
        )
        if response.status_code >= 400:
            raise SignupError(
                f"gateway rejected the pool import (HTTP {response.status_code})",
                detail=response.text[:300],
            )
        body = response.json() if response.content else {}
        return body if isinstance(body, dict) else {}
    finally:
        client.close()
