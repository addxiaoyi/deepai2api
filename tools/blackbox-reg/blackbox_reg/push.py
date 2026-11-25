"""回推网关：把账号写进 `data/blackbox-pool.json` 并领一把 `sk-dcg-…`。

走网关的通用 provider 管理端点（与 worker 的 NDJSON 事件流并行，两条路都幂等）：

    POST {gateway}/api/admin/providers/blackbox/pool/import
    Authorization: Bearer <管理口令>
    {"account": {...}, "mintKey": true}

`importAccount()` 在网关侧按 `apiKey` → `email` 去重，已发过的网关 Key 不会重发，
所以"worker 推送 + 事件流"同时到达也不会出现两把 Key。
"""

from __future__ import annotations

from typing import Any

import httpx

PROVIDER_ID = "blackbox"


class PushError(Exception):
    pass


def push_account(
    account: dict,
    *,
    base_url: str,
    token: str,
    provider_id: str = PROVIDER_ID,
    mint_key: bool = True,
    timeout: float = 30.0,
) -> dict:
    if not base_url:
        raise PushError("没有 push-url。")
    if not token:
        raise PushError("没有 push-token（网关管理口令）。")
    url = f"{base_url.rstrip('/')}/api/admin/providers/{provider_id}/pool/import"
    payload: dict[str, Any] = {"account": account, "mintKey": bool(mint_key)}
    with httpx.Client(timeout=timeout, trust_env=False) as client:
        res = client.post(url, json=payload, headers={"Authorization": f"Bearer {token}"})
    if res.status_code >= 300:
        raise PushError(f"push {res.status_code}: {res.text[:300]}")
    try:
        return res.json()
    except ValueError:
        return {"ok": True, "raw": res.text[:300]}


def push_many(
    accounts: list[dict],
    *,
    base_url: str,
    token: str,
    provider_id: str = PROVIDER_ID,
    mint_key: bool = True,
    log=None,
) -> dict:
    log = log or (lambda *a, **k: None)
    ok = 0
    failed = 0
    keys: list[str] = []
    for account in accounts:
        try:
            result = push_account(
                account, base_url=base_url, token=token, provider_id=provider_id, mint_key=mint_key
            )
            ok += 1
            plaintext = (result.get("gatewayKey") or {}).get("plaintext")
            if plaintext:
                keys.append(plaintext)
            log(f"pushed {account.get('email', '?')}")
        except Exception as cause:  # noqa: BLE001 - 单条失败不打断整批
            failed += 1
            log(f"push failed {account.get('email', '?')}: {cause}")
    return {"pushed": ok, "failed": failed, "gateway_keys": keys}
