"""Cloudflare Turnstile tokens — 注册和登录都要，字段名 `cf_turnstile_token`。

三种来源（与其它 worker 同一个取舍）：

``browser``    用 Camoufox 在 `/v2/sign-up` 上把 widget 跑出来，读 `cf-turnstile-response`
               隐藏域的值。最省事，也最像真人。
``capsolver``  `AntiTurnstileTaskProxyLess` / `AntiTurnstileTask`，填 sitekey + url。
``manual``     `--turnstile-token` 直接给一枚（调试/复用）。
"""

from __future__ import annotations

import json
import time
from typing import Any

from .errors import Transport, TurnstileRequired
from .net import build_client, request

SITE_KEY = "0x4AAAAAAAFWUVCKyusT9T8r"
SIGNUP_URL = "https://dashboard.proxyscrape.com/v2/sign-up"

CAPSOLVER_CREATE = "https://api.capsolver.com/createTask"
CAPSOLVER_RESULT = "https://api.capsolver.com/getTaskResult"


def solve_turnstile(
    *,
    provider: str = "browser",
    api_key: str = "",
    site_url: str = SIGNUP_URL,
    site_key: str = SITE_KEY,
    proxy: str = "",
    timeout: float = 150.0,
    poll: float = 3.0,
    headless: bool = True,
) -> str:
    provider = (provider or "browser").strip().lower()
    if provider in ("none", ""):
        return ""
    if provider == "manual":
        return (api_key or "").strip()
    if provider == "capsolver":
        return capsolver(api_key, site_url=site_url, site_key=site_key, proxy=proxy, timeout=timeout, poll=poll)
    return from_browser(site_url=site_url, proxy=proxy, headless=headless, timeout=timeout)


def from_browser(*, site_url: str = SIGNUP_URL, proxy: str = "", headless: bool = True, timeout: float = 150.0) -> str:
    try:
        from camoufox.sync_api import Camoufox  # type: ignore import-not-found
    except ImportError as exc:  # pragma: no cover - depends on env
        raise TurnstileRequired("camoufox 没装；pip install 'camoufox[geoip]' 或改用 --captcha capsolver") from exc

    kwargs: dict[str, Any] = {"headless": headless, "humanize": True, "os": "windows", "geoip": True}
    if proxy:
        kwargs["proxy"] = {"server": proxy}
    deadline = time.time() + timeout
    with Camoufox(**kwargs) as browser:
        page = browser.new_page()
        page.goto(site_url, wait_until="domcontentloaded", timeout=timeout * 1000)
        while time.time() < deadline:
            page.wait_for_timeout(1500)
            token = page.evaluate(
                """() => {
                    const el = document.querySelector('[name=cf-turnstile-response]');
                    if (el && el.value) return el.value;
                    const iframe = document.querySelector('iframe[src*="challenges.cloudflare.com"]');
                    return iframe ? '' : '';
                }"""
            )
            if token:
                return str(token)
        # 最后再摸一次：有些站点在提交时才把 token 写进隐藏域
        token = page.evaluate(
            "() => (document.querySelector('[name=cf-turnstile-response]') || {}).value || ''"
        )
    if not token:
        raise TurnstileRequired("浏览器没有产出 Turnstile token（出口可能被降分）")
    return str(token)


def capsolver(
    api_key: str,
    *,
    site_url: str = SIGNUP_URL,
    site_key: str = SITE_KEY,
    proxy: str = "",
    timeout: float = 150.0,
    poll: float = 3.0,
) -> str:
    if not api_key:
        raise TurnstileRequired("capsolver 需要 clientKey")
    client = build_client(timeout=40.0)
    try:
        task: dict[str, Any] = {
            "type": "AntiTurnstileTaskProxyLess" if not proxy else "AntiTurnstileTask",
            "websiteURL": site_url,
            "websiteKey": site_key,
        }
        if proxy:
            task["proxy"] = proxy
            task["type"] = "AntiTurnstileTask"
        created = request(client, "POST", CAPSOLVER_CREATE, json={"clientKey": api_key, "task": task})
        body = created.json() if created.content else {}
        if created.status_code >= 400 or body.get("errorId"):
            raise TurnstileRequired(
                f"capsolver refused the task: {body.get('errorCode') or created.status_code}",
                detail=str(body)[:300],
            )
        task_id = str(body.get("taskId") or "")
        if not task_id:
            raise TurnstileRequired("capsolver returned no taskId", detail=str(body)[:200])

        deadline = time.time() + timeout
        while time.time() < deadline:
            time.sleep(poll)
            polled = request(client, "POST", CAPSOLVER_RESULT, json={"clientKey": api_key, "taskId": task_id})
            payload = polled.json() if polled.content else {}
            if payload.get("errorId"):
                raise TurnstileRequired(f"capsolver poll failed: {payload.get('errorCode')}", detail=str(payload)[:200])
            if payload.get("status") == "ready":
                token = (payload.get("solution") or {}).get("token") or (
                    payload.get("solution") or {}
                ).get("gRecaptchaResponse")
                if not token:
                    raise TurnstileRequired("capsolver produced an empty token", detail=json.dumps(payload)[:200])
                return str(token)
        raise TurnstileRequired("capsolver timed out")
    finally:
        client.close()


class TurnstileVault:
    """短时缓存：同一出口连续注册时，Turnstile token 一般能用几分钟。

    Turnstile token 通常**一次性**（服务端校验后作废），所以这里不跨账号复用，
    只在重试同一个账号时避免重复解题。
    """

    def __init__(self, *, provider: str = "browser", api_key: str = "", proxy: str = "", headless: bool = True, log: Any = None) -> None:
        self.provider = provider
        self.api_key = api_key
        self.proxy = proxy
        self.headless = headless
        self.log = log or (lambda *_: None)
        self.solves = 0

    @property
    def stats(self) -> dict[str, Any]:
        return {"provider": self.provider, "solves": self.solves}

    def get(self, *, proxy: str = "") -> str:
        token = solve_turnstile(
            provider=self.provider,
            api_key=self.api_key,
            proxy=proxy or self.proxy,
            headless=self.headless,
        )
        if token:
            self.solves += 1
            self.log("info", "Turnstile 已解出")
        return token


def from_solver(
    solver_url: str,
    *,
    site_url: str = SIGNUP_URL,
    site_key: str = SITE_KEY,
    timeout: float = 180.0,
    poll: float = 3.0,
) -> str:
    """走本地 Turnstile-Solver（tools/captcha-solver）。

    `--captcha solver --captcha-key http://127.0.0.1:5000`，
    空则读 `PROXYSCRAPE_CAPTCHA_SOLVER_URL`，再退到 `http://127.0.0.1:5000`。
    """
    import os

    base = (solver_url or os.environ.get("PROXYSCRAPE_CAPTCHA_SOLVER_URL") or "http://127.0.0.1:5000").rstrip("/")
    client = build_client(timeout=30.0)
    try:
        created = request(
            client, "GET", f"{base}/turnstile", params={"url": site_url, "sitekey": site_key}
        )
        if created.status_code >= 400:
            raise TurnstileRequired(f"solver 拒绝了任务（HTTP {created.status_code}）")
        task_id = str((created.json() or {}).get("task_id") or "")
        if not task_id:
            raise TurnstileRequired("solver 没返回 task_id")
        deadline = time.time() + timeout
        while time.time() < deadline:
            result = request(client, "GET", f"{base}/result", params={"id": task_id})
            payload = result.json() if result.content else {}
            value = str((payload or {}).get("value") or "")
            if value == "CAPTCHA_NOT_READY":
                time.sleep(poll)
                continue
            if value and value != "CAPTCHA_FAIL":
                return value
            raise TurnstileRequired(f"solver 解不出来：{value or result.status_code}")
    finally:
        client.close()
    raise TurnstileRequired("solver 超时")
