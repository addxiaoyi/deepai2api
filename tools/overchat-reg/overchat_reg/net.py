"""HTTP plumbing: per-account proxy, browser-shaped headers, device fingerprint.

Overchat's API host (`api.overchat.ai`) sits behind Cloudflare and answers with a
strict `access-control-allow-origin: https://overchat.ai`, so every request has to
carry the same header set the SPA sends:

    x-device-platform   web | ios | android | desktop
    x-device-version    app version string
    x-device-language   en
    x-device-uuid       a per-install id, stable for the life of one persona
    x-scrf-token        echoed from the `x-scrf-token` cookie the edge hands out
    origin / referer    https://overchat.ai

`trust_env` is off on purpose: ambient `NO_PROXY` in the wild contains `[::1]`,
which httpx rejects, and per-account proxies must win over the environment anyway.
"""

from __future__ import annotations

import random
import string
import uuid
from dataclasses import dataclass, field
from typing import Any, Iterable

import httpx

from .errors import Transport

ORIGIN = "https://overchat.ai"
REFERER = "https://overchat.ai/web/"

CHROME_MAJOR = (129, 130, 131, 132, 133, 134)
PLATFORMS = (
    ("Windows NT 10.0; Win64; x64", "Windows"),
    ("Macintosh; Intel Mac OS X 10_15_7", "macOS"),
    ("X11; Linux x86_64", "Linux"),
)

# Header order matters a little for TLS/HTTP2 fingerprints; httpx emits insertion
# order, so build the dict in the order Chrome would send it.
HEADER_ORDER = (
    "user-agent",
    "accept",
    "accept-language",
    "content-type",
    "origin",
    "referer",
    "sec-ch-ua",
    "sec-ch-ua-mobile",
    "sec-ch-ua-platform",
    "x-device-platform",
    "x-device-version",
    "x-device-language",
    "x-device-uuid",
    "x-scrf-token",
)


def chrome_user_agent(rng: random.Random | None = None) -> str:
    rng = rng or random.Random()
    major = rng.choice(CHROME_MAJOR)
    platform = rng.choice(PLATFORMS)[0]
    return (
        f"Mozilla/5.0 ({platform}) AppleWebKit/537.36 (KHTML, like Gecko) "
        f"Chrome/{major}.0.0.0 Safari/537.36"
    )


def sec_ch_ua(rng: random.Random | None = None) -> str:
    rng = rng or random.Random()
    major = rng.choice(CHROME_MAJOR)
    return f'"Chromium";v="{major}", "Google Chrome";v="{major}", "Not?A_Brand";v="24"'


def new_device_uuid() -> str:
    """The SPA stores this per install; a fresh one per account is what we want."""
    return str(uuid.uuid4())


def random_local_part(rng: random.Random | None = None, *, length: int = 10) -> str:
    rng = rng or random.Random()
    head = rng.choice(string.ascii_lowercase)
    tail = "".join(rng.choices(string.ascii_lowercase + string.digits, k=max(1, length - 1)))
    return head + tail


@dataclass(slots=True)
class DeviceProfile:
    """Everything the edge can correlate between two requests from one persona."""

    device_uuid: str = field(default_factory=new_device_uuid)
    platform: str = "web"
    version: str = "1.0.0"
    language: str = "en"
    user_agent: str = field(default_factory=chrome_user_agent)
    sec_ch_ua: str = field(default_factory=sec_ch_ua)
    sec_ch_ua_platform: str = '"Windows"'
    timezone: str = "America/New_York"
    locale: str = "en-US"
    screen: tuple[int, int] = (1920, 1080)
    proxy: str = ""

    def as_dict(self) -> dict[str, Any]:
        return {
            "device_uuid": self.device_uuid,
            "platform": self.platform,
            "version": self.version,
            "language": self.language,
            "user_agent": self.user_agent,
            "timezone": self.timezone,
            "locale": self.locale,
            "screen": list(self.screen),
            "proxy": _mask_proxy(self.proxy),
        }

    def headers(self, *, scrf: str = "", content_type: str = "") -> dict[str, str]:
        out: dict[str, str] = {
            "user-agent": self.user_agent,
            "accept": "application/json, text/plain, */*",
            "accept-language": f"{self.locale},en;q=0.8",
            "origin": ORIGIN,
            "referer": REFERER,
            "sec-ch-ua": self.sec_ch_ua,
            "sec-ch-ua-mobile": "?0",
            "sec-ch-ua-platform": self.sec_ch_ua_platform,
            "x-device-platform": self.platform,
            "x-device-version": self.version,
            "x-device-language": self.language,
            "x-device-uuid": self.device_uuid,
        }
        if content_type:
            out["content-type"] = content_type
        if scrf:
            out["x-scrf-token"] = scrf
        return out


def _mask_proxy(proxy: str) -> str:
    if not proxy:
        return ""
    if "@" in proxy:
        scheme, rest = proxy.split("://", 1) if "://" in proxy else ("http", proxy)
        creds, host = rest.split("@", 1)
        return f"{scheme}://***@{host}"
    return proxy


def profile_for(rng: random.Random, proxy: str = "") -> DeviceProfile:
    """A coherent persona: UA platform, sec-ch-ua platform and timezone agree."""
    platform, os_name = rng.choice(PLATFORMS)
    major = rng.choice(CHROME_MAJOR)
    ua = (
        f"Mozilla/5.0 ({platform}) AppleWebKit/537.36 (KHTML, like Gecko) "
        f"Chrome/{major}.0.0.0 Safari/537.36"
    )
    tz, locale, screen = {
        "Windows": ("America/New_York", "en-US", (1920, 1080)),
        "macOS": ("America/Los_Angeles", "en-US", (2560, 1440)),
        "Linux": ("Europe/Berlin", "en-GB", (1920, 1080)),
    }[os_name]
    return DeviceProfile(
        platform="web",
        user_agent=ua,
        sec_ch_ua=f'"Chromium";v="{major}", "Google Chrome";v="{major}", "Not?A_Brand";v="24"',
        sec_ch_ua_platform=f'"{os_name}"',
        timezone=tz,
        locale=locale,
        screen=screen,
        proxy=proxy,
    )


class ProxyPool:
    """Round-robin proxy list with per-entry cooldown.

    Registration traffic is extremely rate-limit sensitive: the API answers 429
    after a handful of `send-code` calls from one egress. A pool that hands the
    same proxy to two workers at once, or reuses one that just got 429'd, is the
    single most common reason a batch collapses. So: one lease per proxy at a
    time, and a cooldown the caller can set when an entry gets throttled.
    """

    def __init__(self, entries: Iterable[str] = (), *, cooldown: float = 90.0) -> None:
        self.entries = [e.strip() for e in entries if e and e.strip()]
        self.cooldown = cooldown
        self._cool: dict[str, float] = {}
        self._lease: dict[str, str] = {}
        self._cursor = 0

    def __bool__(self) -> bool:
        return bool(self.entries)

    def __len__(self) -> int:
        return len(self.entries)

    def lease(self, owner: str, *, now: float) -> str:
        """Return a free proxy for `owner`, or "" when every entry is busy/cooling."""
        if not self.entries:
            return ""
        for _ in range(len(self.entries)):
            proxy = self.entries[self._cursor % len(self.entries)]
            self._cursor += 1
            if self._cool.get(proxy, 0) > now:
                continue
            holder = self._lease.get(proxy)
            if holder and holder != owner:
                continue
            self._lease[proxy] = owner
            return proxy
        return ""

    def release(self, owner: str, proxy: str, *, now: float, penalize: bool = False) -> None:
        if not proxy:
            return
        if self._lease.get(proxy) == owner:
            self._lease.pop(proxy, None)
        if penalize:
            self._cool[proxy] = now + self.cooldown

    def penalize(self, proxy: str, *, now: float, seconds: float | None = None) -> None:
        if proxy:
            self._cool[proxy] = now + (self.cooldown if seconds is None else seconds)

    def stats(self, *, now: float) -> dict[str, Any]:
        return {
            "size": len(self.entries),
            "leased": len(self._lease),
            "cooling": sum(1 for t in self._cool.values() if t > now),
        }


def build_client(
    *,
    proxy: str = "",
    timeout: float = 45.0,
    headers: dict[str, str] | None = None,
    follow_redirects: bool = True,
    transport: Any = None,
    base_url: str = "",
    http2: bool = True,
) -> httpx.Client:
    kwargs: dict[str, Any] = {
        "timeout": timeout,
        "trust_env": False,
        "follow_redirects": follow_redirects,
        "headers": headers or {},
        "http2": http2,
    }
    if proxy:
        kwargs["proxy"] = proxy
    if transport is not None:
        kwargs["transport"] = transport
    if base_url:
        kwargs["base_url"] = base_url
    try:
        return httpx.Client(**kwargs)
    except ImportError:
        # http2 needs the `h2` package; plain HTTP/1.1 still works.
        kwargs["http2"] = False
        return httpx.Client(**kwargs)


def request(client: httpx.Client, method: str, url: str, **kwargs: Any) -> httpx.Response:
    try:
        return client.request(method, url, **kwargs)
    except httpx.HTTPError as exc:
        raise Transport(f"{method} {url} failed: {exc.__class__.__name__}: {exc}") from exc
