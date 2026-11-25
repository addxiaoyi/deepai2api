"""Push freshly created accounts into the gateway's Cleverai pool.

The gateway exposes `POST /api/admin/cleverai/pool/import`; this is the client
used when the worker runs as a child of that gateway ("批量注册" panel) or from
the CLI with `--push-url/--push-token`. The gateway mints its own `sk-dcg-…`
key for the account and writes the merged pool file, so one call is enough.
"""

from __future__ import annotations

from typing import Any

from .errors import SignupError
from .net import build_client, request

POOL_IMPORT = "/api/admin/cleverai/pool/import"


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
        "uid": account.get("uid", ""),
        "idToken": account.get("idToken") or account.get("id_token", ""),
        "refreshToken": account.get("refreshToken") or account.get("refresh_token", ""),
        "appCheck": account.get("appCheck") or account.get("app_check", ""),
        "appCheckExp": account.get("appCheckExp") or account.get("app_check_exp", 0),
        "wallet": account.get("wallet") or {},
        "device": account.get("device") or {},
        "emailVerified": account.get("emailVerified", account.get("email_verified")),
        "engine": account.get("engine", "protocol"),
        "createdAt": account.get("createdAt") or account.get("created_at", ""),
        "mintKey": bool(mint_key),
    }
    if not payload["refreshToken"]:
        raise SignupError("account carries no refreshToken; nothing to push")
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
