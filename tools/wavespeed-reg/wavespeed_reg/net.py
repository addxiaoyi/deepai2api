"""HTTP plumbing: proxy-aware clients, UA generation, small helpers.

和 `allmodels_reg.net` 同构，但多了两件 WaveSpeed 特有的事：

  1. **POST 不自动重试**。生成接口 `POST /api/v3/{model_id}` 是"提交一个计费任务"，
     重试会产生重复任务与重复扣费；只有 GET 允许重试。
  2. **提交响应的 `code` 可能与 HTTP 200 不一致**。WaveSpeed 在 body 里也带一个
     `code`，HTTP 200 + body.code != 200 依然是失败，必须解析 body。
"""

from __future__ import annotations

import random
from typing import Any

import httpx

from .errors import Transport

CHROME_MAJOR = (131, 132, 133, 134, 135, 136)
PLATFORMS = (
    ("Windows NT 10.0; Win64; x64", "Windows"),
    ("Macintosh; Intel Mac OS X 10_15_7", "macOS"),
    ("X11; Linux x86_64", "Linux"),
)

# 生成任务的状态机（官方枚举）
TASK_STATES = ("created", "processing", "completed", "failed", "cancelled", "timeout", "deleted")
TASK_OK = ("completed",)
TASK_FAILED = ("failed", "cancelled", "timeout", "deleted")


def chrome_user_agent(rng: random.Random | None = None) -> str:
    """面板是 Next.js SPA：装成 Chrome 最省事。"""
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
    """一个绝不继承环境代理的客户端。

    `NO_PROXY` 在真实环境里经常包含 httpx 解析不了的条目（`[::1]`），而且每账号
    的代理必须优先于环境变量。`transport` 让离线测试可以完全不走网络。
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
    except httpx.HTTPError as exc:  # 网络、代理、DNS、TLS
        raise Transport(f"{method} {url} failed: {exc.__class__.__name__}: {exc}") from exc


def safe_request(
    client: httpx.Client,
    method: str,
    url: str,
    *,
    retries: int = 0,
    retry_status: tuple[int, ...] = (429, 502, 503, 504),
    **kwargs: Any,
) -> httpx.Response:
    """带可选重试的请求。

    `retries` 默认 0：**POST 提交任务必须显式传 0**，避免重复计费。
    只有幂等 GET 才传 retries>0。
    """
    import time

    last: httpx.Response | None = None
    for attempt in range(retries + 1):
        last = request(client, method, url, **kwargs)
        if last.status_code not in retry_status or attempt >= retries:
            return last
        time.sleep(min(8.0, 1.2 * (attempt + 1)))
    assert last is not None
    return last


def body_code(payload: Any) -> int:
    """WaveSpeed 在 body 里也带 `code`；缺失时按 200 处理。"""
    if isinstance(payload, dict):
        for key in ("code", "statusCode", "status_code"):
            value = payload.get(key)
            if isinstance(value, int):
                return value
            if isinstance(value, str) and value.isdigit():
                return int(value)
    return 200


def looks_like_cloudflare(text: str) -> bool:
    lowered = (text or "")[:4000].lower()
    return any(
        marker in lowered
        for marker in ("just a moment", "cf-browser-verification", "challenge-platform", "attention required")
    )
