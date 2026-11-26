"""回推网关 `/api/admin/jevai/pool/import`。"""

from __future__ import annotations

from typing import Any

from .errors import JevError
from .net import build_client, request

POOL_IMPORT = "/api/admin/jevai/pool/import"


def push_account(base_url: str, token: str, record: dict[str, Any], *, timeout: float = 30.0) -> dict[str, Any]:
    if not base_url or not token:
        return {}
    payload = {
        "email": record.get("email", ""),
        "password": record.get("password", ""),
        "name": record.get("name", ""),
        "apiKey": record.get("apiKey") or record.get("api_key", ""),
        "keyPrefix": record.get("keyPrefix") or "sk-glm5-",
        "userId": record.get("userId") or record.get("user_id", ""),
        "emailVerified": record.get("emailVerified"),
        "credits": record.get("credits") or {},
        "engine": record.get("engine", "protocol"),
        "createdAt": record.get("createdAt") or record.get("created_at", ""),
    }
    if not payload["apiKey"]:
        raise JevError("record has no apiKey")
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
            raise JevError(f"gateway rejected the import (HTTP {response.status_code})", detail=response.text[:300])
        body = response.json() if response.content else {}
        return body if isinstance(body, dict) else {}
    finally:
        client.close()
