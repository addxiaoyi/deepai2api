"""HTTP plumbing: proxy-aware clients, UA generation, small helpers."""

from __future__ import annotations

import random
from typing import Any

import httpx

from .errors import Transport

CHROME_MAJOR = (129, 130, 131, 132, 133, 134)
PLATFORMS = (
    ("Windows NT 10.0; Win64; x64", "Windows"),
    ("Macintosh; Intel Mac OS X 10_15_7", "macOS"),
    ("X11; Linux x86_64", "Linux"),
)


def chrome_user_agent(rng: random.Random | None = None) -> str:
    """The dashboard is a Vite/React SPA: presenting as Chrome keeps it happy."""
    rng = rng or random.Random()
    major = rng.choice(CHROME_MAJOR)
    platform = rng.choice(PLATFORMS)[0]
    return (
        f"Mozilla/5.0 ({platform}) AppleWebKit/537.36 (KHTML, like Gecko) "
        f"Chrome/{major}.0.0.0 Safari/537.36"
    )


def build_client(
    *,
    proxy: str = "",
    timeout: float = 45.0,
    user_agent: str = "",
    language: str = "en-US",
    follow_redirects: bool = True,
    transport: Any = None,
    base_url: str = "",
) -> httpx.Client:
    """A client that never inherits ambient proxy settings.

    `NO_PROXY` in the wild frequently contains entries httpx rejects (`[::1]`),
    and per-account proxies must win over the environment anyway. `transport`
    exists so the offline tests can drive the protocol without a network.
    """
    ua = user_agent or chrome_user_agent()
    headers = {
        "User-Agent": ua,
        "Accept": "application/json, text/plain, */*",
        "Accept-Language": f"{language},en;q=0.7",
        "Cache-Control": "no-cache",
        "Pragma": "no-cache",
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
    if base_url:
        kwargs["base_url"] = base_url
    return httpx.Client(**kwargs)


def request(client: httpx.Client, method: str, url: str, **kwargs: Any) -> httpx.Response:
    try:
        return client.request(method, url, **kwargs)
    except httpx.HTTPError as exc:  # network, proxy, DNS, TLS
        raise Transport(f"{method} {url} failed: {exc.__class__.__name__}: {exc}") from exc
