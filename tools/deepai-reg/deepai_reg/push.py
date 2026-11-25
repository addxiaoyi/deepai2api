"""Push freshly created accounts into the gateway's pool.

The gateway exposes `POST /api/admin/accounts`; this module is the client used
when the tool runs as a child process of that gateway (WebUI "批量注册" panel).
"""

from __future__ import annotations

from typing import Any

from .errors import SignupError
from .net import build_client, request


def push_account(base_url: str, token: str, account: dict[str, Any], *, timeout: float = 30.0) -> bool:
    if not base_url or not token:
        return False
    payload = {
        "apiKey": account.get("api_key", ""),
        "username": account.get("username") or account.get("email", ""),
        "isPro": bool(account.get("is_pro")),
        "email": account.get("email", ""),
        "label": account.get("email") or account.get("username") or "deepai",
        "password": account.get("password", ""),
    }
    if not payload["apiKey"]:
        return False
    client = build_client(timeout=timeout)
    try:
        response = request(
            client,
            "POST",
            f"{base_url.rstrip('/')}/api/admin/accounts",
            json=payload,
            headers={"Authorization": f"Bearer {token}", "Accept": "application/json"},
        )
        if response.status_code >= 400:
            raise SignupError(
                f"gateway rejected the account push (HTTP {response.status_code})",
                detail=response.text[:200],
            )
        return True
    finally:
        client.close()
