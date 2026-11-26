"""HTTP plumbing for jev-ai.org."""

from __future__ import annotations

import random
from typing import Any

import httpx

from .errors import Transport

CHROME_MAJOR = (131, 132, 133, 134, 135)
PLATFORMS = (
    "Windows NT 10.0; Win64; x64",
    "Macintosh; Intel Mac OS X 10_15_7",
    "X11; Linux x86_64",
)


def chrome_user_agent(rng: random.Random | None = None) -> str:
    rng = rng or random.Random()
    return (
        f"Mozilla/5.0 ({rng.choice(PLATFORMS)}) AppleWebKit/537.36 (KHTML, like Gecko) "
        f"Chrome/{rng.choice(CHROME_MAJOR)}.0.0.0 Safari/537.36"
    )


def build_client(
    *,
    proxy: str = "",
    timeout: float = 45.0,
    user_agent: str = "",
    language: str = "en-US",
    follow_redirects: bool = True,
    transport: Any = None,
) -> httpx.Client:
    headers = {
        "User-Agent": user_agent or chrome_user_agent(),
        "Accept": "application/json, text/plain, */*",
        "Accept-Language": f"{language},en;q=0.7",
        "Cache-Control": "no-cache",
    }
    kwargs: dict[str, Any] = {
        "timeout": timeout,
        "trust_env": False,
        "follow_redirects": follow_redirects,
        "headers": headers,
    }
    if proxy:
        kwargs["proxy"] = proxy
    if transport is not None:
        kwargs["transport"] = transport
    return httpx.Client(**kwargs)


def request(client: httpx.Client, method: str, url: str, **kwargs: Any) -> httpx.Response:
    try:
        return client.request(method, url, **kwargs)
    except httpx.HTTPError as exc:
        raise Transport(f"{method} {url} failed: {exc.__class__.__name__}: {exc}") from exc
