"""Proxy-aware HTTP helpers (mirrors deepai-reg so behaviour is identical)."""

from __future__ import annotations

import random
from typing import Any

import httpx

from .errors import Transport

FIREFOX = (134, 135, 136, 137, 138)


def firefox_ua(rng: random.Random | None = None) -> str:
    rng = rng or random.Random()
    v = rng.choice(FIREFOX)
    return f"Mozilla/5.0 (Windows NT 10.0; Win64; x64; rv:{v}.0) Gecko/20100101 Firefox/{v}.0"


def build_client(*, proxy: str = "", timeout: float = 45.0, language: str = "en-US") -> httpx.Client:
    headers = {
        "User-Agent": firefox_ua(),
        "Accept": "application/json, text/plain, */*",
        "Accept-Language": f"{language},en;q=0.7",
        "Cache-Control": "no-cache",
    }
    kwargs: dict[str, Any] = {
        "timeout": timeout,
        "trust_env": False,
        "follow_redirects": True,
        "headers": headers,
    }
    if proxy:
        kwargs["proxy"] = proxy
    return httpx.Client(**kwargs)


def request(client: httpx.Client, method: str, url: str, **kwargs: Any) -> httpx.Response:
    try:
        return client.request(method, url, **kwargs)
    except httpx.HTTPError as exc:
        raise Transport(f"{method} {url} failed: {exc.__class__.__name__}: {exc}") from exc


def proxy_dict(proxy: str) -> dict[str, str] | None:
    from urllib.parse import urlparse

    if not proxy:
        return None
    parsed = urlparse(proxy if "://" in proxy else f"http://{proxy}")
    server = f"{parsed.scheme}://{parsed.hostname}"
    if parsed.port:
        server += f":{parsed.port}"
    out = {"server": server}
    if parsed.username:
        out["username"] = parsed.username
    if parsed.password:
        out["password"] = parsed.password
    return out
