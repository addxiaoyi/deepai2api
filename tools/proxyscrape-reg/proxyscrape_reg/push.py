"""回推网关 `/api/admin/proxyscrape/pool/import`。

一条记录 = 一个账号 + 它带出来的代理串；网关那边会同时写进账号池与共享代理池。
"""

from __future__ import annotations

from typing import Any

from .errors import ProxyScrapeError
from .net import build_client, request

POOL_IMPORT = "/api/admin/proxyscrape/pool/import"


def push_account(base_url: str, token: str, record: dict[str, Any], *, timeout: float = 30.0) -> dict[str, Any]:
    if not base_url or not token:
        return {}
    payload = {
        "email": record.get("email", ""),
        "password": record.get("password", ""),
        "accessToken": record.get("accessToken") or record.get("access_token", ""),
        "refreshToken": record.get("refreshToken") or record.get("refresh_token", ""),
        "trialClaimed": bool(record.get("trialClaimed", record.get("trial_claimed"))),
        "proxies": record.get("proxies") or [],
        "engine": record.get("engine", "protocol"),
        "createdAt": record.get("createdAt") or record.get("created_at", ""),
    }
    if not payload["email"]:
        raise ProxyScrapeError("record has no email")
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
            raise ProxyScrapeError(
                f"gateway rejected the import (HTTP {response.status_code})", detail=response.text[:300]
            )
        body = response.json() if response.content else {}
        return body if isinstance(body, dict) else {}
    finally:
        client.close()
