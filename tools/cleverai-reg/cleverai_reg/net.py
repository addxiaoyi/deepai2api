"""HTTP plumbing: proxy-aware clients, UA generation, device descriptors.

The dashboard is a Next.js SPA that talks to two different backends
(Google Identity Toolkit for auth, `api.cleverai.app` for everything else), so
the same client has to present as one coherent Chrome install: UA, device blob
and language all agree with the exit IP's timezone.
"""

from __future__ import annotations

import json
import random
from typing import Any

import httpx

from .errors import Transport

CHROME_MAJOR = (129, 130, 131, 132, 133, 134)
PLATFORMS = (
    ("Windows NT 10.0; Win64; x64", "Windows", "10", "Asia/Shanghai"),
    ("Macintosh; Intel Mac OS X 10_15_7", "macOS", "14.6", "America/New_York"),
    ("X11; Linux x86_64", "Linux", "6.8", "Europe/Berlin"),
)

# Residential exits are what the dashboard actually likes; a datacenter IP gets
# reCAPTCHA-scored into oblivion before the first signup lands.
LOCALES = (
    ("en-US", "en", "US", "USD", "$"),
    ("en-GB", "en", "GB", "GBP", "£"),
    ("de-DE", "de", "DE", "EUR", "€"),
)


def chrome_user_agent(rng: random.Random | None = None) -> str:
    rng = rng or random.Random()
    major = rng.choice(CHROME_MAJOR)
    platform = rng.choice(PLATFORMS)[0]
    return (
        f"Mozilla/5.0 ({platform}) AppleWebKit/537.36 (KHTML, like Gecko) "
        f"Chrome/{major}.0.0.0 Safari/537.36"
    )


def device_blob(user_agent: str = "", rng: random.Random | None = None) -> dict[str, Any]:
    """The `X-DEVICE` header the SPA sends — shape copied from the shipped bundle."""
    rng = rng or random.Random()
    platform = next((p for p in PLATFORMS if p[0] in (user_agent or "")), None)
    if platform is None:
        platform = PLATFORMS[rng.randrange(len(PLATFORMS))]
    tag, language, region, currency, symbol = LOCALES[rng.randrange(len(LOCALES))]
    return {
        "deviceType": "DESKTOP",
        "deviceOs": platform[1],
        "deviceOsVersion": platform[2],
        "deviceModel": "Chrome",
        "deviceLanguageCode": language,
        "deviceLanguageTag": tag,
        "deviceRegionCode": region,
        "deviceTimeZone": platform[3],
        "deviceTextDirection": "ltr",
        "deviceCurrencyCode": currency,
        "deviceCurrencySymbol": symbol,
        "platform": "web",
    }


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

    `NO_PROXY` in the wild frequently carries entries httpx rejects (`[::1]`),
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


def multipart_json(payload: dict[str, Any]) -> dict[str, tuple[None, str]]:
    """The dashboard posts profile writes as `multipart/form-data` with a `json` part."""
    return {"json": (None, json.dumps(payload, separators=(",", ":")))}
