"""回推网关：`/api/admin/<line>/pool/import`。"""

from __future__ import annotations

from typing import Any

from .errors import LineError
from .net import build_client, request

POOL_IMPORT_TEMPLATE = "/api/admin/{line}/pool/import"


def push_account(
    base_url: str,
    token: str,
    record: dict[str, Any],
    *,
    line: str = "",
    timeout: float = 30.0,
) -> dict[str, Any]:
    if not base_url or not token:
        return {}
    payload = {
        "email": record.get("email", ""),
        "password": record.get("password", ""),
        "name": record.get("name", ""),
        "apiKey": record.get("apiKey") or record.get("api_key", ""),
        "keyPrefix": record.get("keyPrefix") or "sk-",
        "userId": record.get("userId") or record.get("user_id", ""),
        "emailVerified": record.get("emailVerified"),
        "credits": record.get("credits") or {},
        "engine": record.get("engine", "protocol"),
        "createdAt": record.get("createdAt") or record.get("created_at", ""),
    }
    if not payload["apiKey"] and not record.get("anonymous"):
        raise LineError("record has no apiKey")
    client = build_client(timeout=timeout)
    try:
        path = POOL_IMPORT_TEMPLATE.format(line=line) if line else "/api/admin/pool/import"
        response = request(
            client,
            "POST",
            f"{base_url.rstrip('/')}{path}",
            json=payload,
            headers={"Authorization": f"Bearer {token}", "Accept": "application/json"},
        )
        if response.status_code >= 400:
            raise LineError(f"gateway rejected the import (HTTP {response.status_code})", detail=response.text[:300])
        body = response.json() if response.content else {}
        return body if isinstance(body, dict) else {}
    finally:
        client.close()
