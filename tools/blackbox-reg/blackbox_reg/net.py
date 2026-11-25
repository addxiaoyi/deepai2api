"""HTTP 会话。

一条会话 = 一个 persona + 一个出口 + 一个 cookie jar。Blackbox 的登录态是
Auth.js 的 `__Secure-authjs.session-token`（HttpOnly/Secure/SameSite=Lax），
跨请求必须保持；Google 那半程的 cookie 也在同一个 jar 里，所以一次 OAuth
走完之前不能换 jar、不能换 IP。

错误映射（决定网关侧退避与是否换号）：
    429                  → rate_limited
    403 / CF 挑战         → blocked
    401                  → session_expired（会话没了，需要重新授权）
    5xx / 连接失败        → upstream / transport
"""

from __future__ import annotations

import random
import time
from typing import Any
from urllib.parse import urlparse

import httpx

from .errors import Blocked, RateLimited, SessionExpired, TransportError, UpstreamError
from .identity import Persona

DEFAULT_TIMEOUT = 45.0
RETRY_STATUS = {500, 502, 503, 504, 520, 521, 522, 524}
CF_MARKERS = ("just a moment", "cf-browser-verification", "challenge-platform", "attention required")


def make_client(*, persona: Persona, proxy: str = "", timeout: float = DEFAULT_TIMEOUT) -> httpx.Client:
    kwargs: dict[str, Any] = {
        "headers": persona.headers(),
        "timeout": timeout,
        "follow_redirects": False,
        "http2": False,
        # 不读系统代理：这台机器上 HTTP_PROXY 可能是坏的，出口只认 --proxy
        "trust_env": False,
    }
    if proxy:
        try:
            return httpx.Client(proxy=proxy, **kwargs)
        except TypeError:  # pragma: no cover - httpx < 0.26
            return httpx.Client(proxies={"all://": proxy}, **kwargs)
    return httpx.Client(**kwargs)


def as_cookie_rows(client: httpx.Client) -> list[dict]:
    rows = []
    for cookie in client.cookies.jar:
        rows.append(
            {
                "name": cookie.name,
                "value": cookie.value,
                "domain": cookie.domain or "",
                "path": cookie.path or "/",
            }
        )
    return rows


def apply_cookie_rows(client: httpx.Client, cookies: list[dict], default_domain: str = "") -> int:
    """把 cookies 灌进 jar。

    浏览器导出的 cookies 常常不带 domain（或带的是 `127.0.0.1` 这种）。http.cookiejar
    对 `domain=""` 的 cookie 不会匹配任何 host，所以这里必须补一个默认域，否则
    "注入了 N 条"但其实一条都不会被发出去。
    """
    applied = 0
    for row in cookies or []:
        name = str(row.get("name") or "")
        if not name:
            continue
        domain = str(row.get("domain") or "").lstrip(".") or default_domain
        path = str(row.get("path") or "/")
        try:
            client.cookies.set(name, str(row.get("value") or ""), domain=domain or None, path=path)
            applied += 1
        except Exception:  # noqa: BLE001 - 单条坏 cookie 不影响其它
            continue
    return applied


def host_of(url: str) -> str:
    try:
        return urlparse(url).hostname or ""
    except Exception:  # noqa: BLE001
        return ""


class Session:
    """带 cookie 的会话，自动重试与错误分类。"""

    def __init__(
        self,
        *,
        persona: Persona,
        proxy: str = "",
        timeout: float = DEFAULT_TIMEOUT,
        retries: int = 3,
        log=None,
    ):
        self.persona = persona
        self.proxy = proxy
        self.timeout = timeout
        self.retries = max(1, retries)
        self.client = make_client(persona=persona, proxy=proxy, timeout=timeout)
        self.log = log or (lambda *a, **k: None)

    # ------------------------------------------------------------------ 基础
    def request(
        self,
        method: str,
        url: str,
        *,
        headers: dict[str, str] | None = None,
        data: dict[str, str] | str | None = None,
        json: Any = None,
        params: dict[str, str] | None = None,
        allow_redirects: bool = False,
        expected: tuple[int, ...] = (200, 201, 202, 204, 301, 302, 303, 307, 308),
        allow_auth_failure: bool = False,
    ) -> httpx.Response:
        last_error: Exception | None = None
        for attempt in range(self.retries):
            try:
                res = self.client.request(
                    method,
                    url,
                    headers=headers,
                    data=data,
                    json=json,
                    params=params,
                    follow_redirects=allow_redirects,
                )
            except (httpx.TimeoutException, httpx.TransportError) as cause:
                last_error = TransportError(f"{method} {url} 传输失败：{cause}")
                self._sleep(attempt)
                continue

            if res.status_code in expected:
                return res

            body = _snippet(res).lower()
            if any(marker in body for marker in CF_MARKERS):
                raise Blocked(f"{method} {url} 命中人机挑战", status=res.status_code, detail=_snippet(res))
            if res.status_code == 429:
                raise RateLimited(f"{method} {url} 被限流（429）", status=429, detail=_snippet(res))
            if res.status_code == 401 and not allow_auth_failure:
                raise SessionExpired(f"{method} {url} 会话失效（401）", status=401, detail=_snippet(res))
            if res.status_code == 403 and not allow_auth_failure:
                raise Blocked(f"{method} {url} 被拒（403）", status=403, detail=_snippet(res))
            if res.status_code in RETRY_STATUS:
                last_error = UpstreamError(
                    f"{method} {url} 上游 {res.status_code}", status=res.status_code, detail=_snippet(res)
                )
                self._sleep(attempt)
                continue
            raise UpstreamError(
                f"{method} {url} 返回 {res.status_code}",
                status=res.status_code,
                detail=_snippet(res),
            )
        raise last_error or TransportError(f"{method} {url} 重试耗尽")

    def get(self, url: str, **kwargs) -> httpx.Response:
        return self.request("GET", url, **kwargs)

    def post(self, url: str, **kwargs) -> httpx.Response:
        return self.request("POST", url, **kwargs)

    # ------------------------------------------------------------ 便捷封装
    def form_post(self, url: str, fields: dict[str, str], *, origin: str, referer: str, **kwargs) -> httpx.Response:
        return self.request(
            "POST",
            url,
            headers=self.persona.xhr_headers(origin=origin, referer=referer),
            data=fields,
            **kwargs,
        )

    def json_call(
        self,
        method: str,
        url: str,
        *,
        payload: Any = None,
        token: str = "",
        origin: str = "",
        referer: str = "",
        allow_auth_failure: bool = False,
    ) -> httpx.Response:
        headers = {
            "User-Agent": self.persona.user_agent,
            "Accept": "application/json",
            "Accept-Language": self.persona.accept_language,
        }
        if payload is not None:
            headers["Content-Type"] = "application/json"
        if token:
            headers["Authorization"] = f"Bearer {token}"
        if origin:
            headers["Origin"] = origin
        if referer:
            headers["Referer"] = referer
        return self.request(method, url, headers=headers, json=payload, allow_auth_failure=allow_auth_failure)

    # ------------------------------------------------------------------ 工具
    def cookies_dict(self) -> dict[str, str]:
        return {cookie.name: cookie.value for cookie in self.client.cookies.jar}

    def cookie_rows(self) -> list[dict]:
        return as_cookie_rows(self.client)

    def has_session_cookie(self, name: str = "__Secure-authjs.session-token") -> bool:
        return any(cookie.name.endswith("session-token") or cookie.name == name for cookie in self.client.cookies.jar)

    def close(self) -> None:
        try:
            self.client.close()
        except Exception:  # noqa: BLE001
            pass

    def __enter__(self) -> "Session":
        return self

    def __exit__(self, *exc) -> None:
        self.close()

    def _sleep(self, attempt: int) -> None:
        delay = min(20.0, 1.5 * (2**attempt)) + random.uniform(0, 0.8)
        time.sleep(delay)


def _snippet(res: httpx.Response, limit: int = 220) -> str:
    try:
        return res.text[:limit].replace("\n", " ")
    except Exception:  # noqa: BLE001
        return ""
