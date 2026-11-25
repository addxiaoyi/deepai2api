"""回推网关 `/api/admin/providers/zo/pool/import`。

和 `provider-pool.mjs` 的 `importAccount` 幂等口径一致：按 apiKey 去重，
重复推同一个账号只会刷新凭据，不会重复发网关 Key。
"""

from __future__ import annotations

import json
import urllib.error
import urllib.request


class PushError(Exception):
    pass


def push_account(
    record: dict,
    *,
    base_url: str,
    token: str,
    mint_key: bool = True,
    timeout: float = 30.0,
) -> dict:
    url = f"{base_url.rstrip('/')}/api/admin/providers/zo/pool/import"
    payload = json.dumps({"mintKey": mint_key, "account": record}, ensure_ascii=False).encode("utf-8")
    request = urllib.request.Request(
        url,
        data=payload,
        method="POST",
        headers={
            "Content-Type": "application/json",
            "Authorization": f"Bearer {token}",
        },
    )
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            body = response.read().decode("utf-8", errors="replace")
    except urllib.error.HTTPError as cause:
        detail = cause.read().decode("utf-8", errors="replace")[:200]
        raise PushError(f"HTTP {cause.code}: {detail}") from cause
    except urllib.error.URLError as cause:
        raise PushError(f"连不上网关：{cause.reason}") from cause
    try:
        return json.loads(body)
    except json.JSONDecodeError:
        return {"ok": False, "raw": body[:200]}


def push_many(
    rows: list[dict],
    *,
    base_url: str,
    token: str,
    mint_key: bool = True,
    log=None,
) -> dict:
    log = log or (lambda *a, **k: None)
    pushed = 0
    failed: list[dict] = []
    gateway_keys: list[str] = []
    for row in rows:
        email = row.get("email", "?")
        try:
            result = push_account(row, base_url=base_url, token=token, mint_key=mint_key)
        except PushError as cause:
            failed.append({"email": email, "error": str(cause)})
            log(f"回推失败 {email}：{cause}")
            continue
        if not result.get("ok"):
            failed.append({"email": email, "error": json.dumps(result)[:200]})
            continue
        pushed += 1
        key = (result.get("gatewayKey") or {}).get("plaintext")
        if key:
            gateway_keys.append(f"{email}\t{key}")
    return {"pushed": pushed, "failed": failed, "gateway_keys": gateway_keys}