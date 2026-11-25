"""HTTP 客户端与重试。

一条会话 = 一个 persona + 一个出口 + 一个 cookie jar。Zo 的登录态是
`Domain=.zo.computer` 上的 HttpOnly cookie（`access_token` / `refresh_token`），
面板与 API 两个 host 共用，所以 jar 必须跨 host 保持。

失败映射（决定网关侧的退避与是否换号）：
    403 / Cloudflare 挑战            → blocked
    429                              → rate_limited
    5xx / 超时                       → upstream / transport
"""

from __future__ import annotations

import random
import time
from typing import Any

import httpx

from .errors import Blocked, RateLimited, TransportError, UpstreamError
from .identity import Persona

DEFAULT_TIMEOUT = 45.0
RETRY_STATUS = {500, 502, 503, 504, 520, 521, 522, 524}


def make_client(*, persona: Persona, proxy: str = "", timeout: float = DEFAULT_TIMEOUT) -> httpx.Client:
    headers = persona.headers()
    kwargs: dict[str, Any] = {
        "headers": headers,
        "timeout": timeout,
        "follow_redirects": False,
        "http2": False,
        # 不读系统代理环境：出口只认 --proxy 显式传入的那个
        "trust_env": False,
    }
    if proxy:
        try:
            return httpx.Client(proxy=proxy, **kwargs)
        except TypeError:  # pragma: no cover - 老版本 httpx
            return httpx.Client(proxies={"all://": proxy}, **kwargs)
    return httpx.Client(**kwargs)


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
        data: Any = None,
        json: Any = None,
        params: dict[str, str] | None = None,
        allow_redirects: bool = False,
        expected: tuple[int, ...] = (200, 201, 202, 204, 301, 302, 303, 307, 308),
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
            if res.status_code == 429:
                raise RateLimited(f"{method} {url} 被限流（429）", status=429, detail=_snippet(res))
            if res.status_code in RETRY_STATUS:
                last_error = UpstreamError(
                    f"{method} {url} 上游 {res.status_code}", status=res.status_code, detail=_snippet(res)
                )
                self._sleep(attempt)
                continue
            # 4xx 一律交给调用方判断语义（409 会话占用、403 拦截、401 令牌过期…）
            return res
        raise last_error or TransportError(f"{method} {url} 重试耗尽")

    def get(self, url: str, **kwargs) -> httpx.Response:
        return self.request("GET", url, **kwargs)

    def post(self, url: str, **kwargs) -> httpx.Response:
        return self.request("POST", url, **kwargs)

    # ------------------------------------------------------------ 便捷封装
    def json_call(
        self,
        method: str,
        url: str,
        *,
        payload: Any = None,
        token: str = "",
        origin: str = "",
        referer: str = "",
        headers: dict[str, str] | None = None,
        expected: tuple[int, ...] = (200, 201, 202, 204, 301, 302, 303, 307, 308),
    ) -> httpx.Response:
        merged = {
            "User-Agent": self.persona.user_agent,
            "Accept": "application/json, text/event-stream",
            "Accept-Language": self.persona.accept_language,
        }
        if payload is not None:
            merged["Content-Type"] = "application/json"
        if token:
            merged["Authorization"] = f"Bearer {token}"
        if origin:
            merged["Origin"] = origin
        if referer:
            merged["Referer"] = referer
        merged.update(headers or {})
        return self.request(method, url, headers=merged, json=payload, expected=expected)

    # ------------------------------------------------------------------ 工具
    def cookies_dict(self) -> dict[str, str]:
        return {cookie.name: cookie.value for cookie in self.client.cookies.jar}

    def cookie_records(self) -> list[dict]:
        return [
            {
                "name": cookie.name,
                "value": cookie.value,
                "domain": cookie.domain or "",
                "path": cookie.path or "/",
            }
            for cookie in self.client.cookies.jar
        ]

    def cookie_header(self) -> str:
        return "; ".join(f"{c.name}={c.value}" for c in self.client.cookies.jar)

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


def raise_for_status(res: httpx.Response, *, url: str) -> None:
    """统一的 4xx/5xx 归类；调用方只需要关心"成功/失败"。"""
    if res.status_code < 400:
        return
    if res.status_code in (401, 403):
        raise Blocked(f"{url} 被拒（{res.status_code}）", status=res.status_code, detail=_snippet(res))
    if res.status_code == 429:
        raise RateLimited(f"{url} 被限流（429）", status=429, detail=_snippet(res))
    raise UpstreamError(f"{url} 返回 {res.status_code}", status=res.status_code, detail=_snippet(res))


def _snippet(res: httpx.Response, limit: int = 220) -> str:
    try:
        return res.text[:limit].replace("\n", " ")
    except Exception:  # noqa: BLE001
        return ""