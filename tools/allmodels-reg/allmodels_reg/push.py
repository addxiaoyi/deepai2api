"""Push freshly created accounts into the gateway's Allmodels pool.

The gateway exposes `POST /api/admin/allmodels/pool/import`; this is the client
used when the worker runs as a child of that gateway ("批量注册" panel) or from
the CLI with `--push-url/--push-token`. The gateway mints its own `sk-dcg-…`
key for the account and writes the merged pool file, so one call is enough.
"""

from __future__ import annotations

from typing import Any

from .errors import SignupError
from .net import build_client, request

POOL_IMPORT = "/api/admin/allmodels/pool/import"


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
        "projectId": account.get("projectId") or account.get("project_id", ""),
        "guardrailId": account.get("guardrailId") or account.get("guardrail_id", ""),
        "organizationId": account.get("organizationId") or account.get("organization_id", ""),
        "emailVerified": account.get("emailVerified", account.get("email_verified")),
        "cookies": account.get("cookies") or [],
        "engine": account.get("engine", "protocol"),
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
