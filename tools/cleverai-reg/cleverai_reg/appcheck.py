"""Firebase App Check tokens — the one gate the protocol engine cannot skip.

`api.cleverai.app` refuses every write with

    401 {"code":"APP_CHECK_REQUIRED","message":"App Check token required"}

until the request carries `X-Firebase-AppCheck`. The token is minted by
exchanging a **reCAPTCHA Enterprise** token for this project:

    POST https://firebaseappcheck.googleapis.com/v1/projects/<project>
         /apps/<app-id>:exchangeRecaptchaEnterpriseToken?key=<web api key>
         {"recaptcha_enterprise_token": "..."}

Two ways to get the reCAPTCHA half:

``browser``   open the real site in Camoufox; the SPA mints an App Check token
              on boot, so the exchange response can be read straight off the
              wire. Costs one page load, yields a 3600 s token.
``capsolver`` solve `ReCaptchaV3EnterpriseTask` for the same site key with
              `pageAction=firebase_app_check`. No browser, paid per solve.

Both produce the same JWT, and one token serves every account in the batch —
it is bound to the *app install*, not the user — so `AppCheckVault` caches it
and refreshes only when the TTL runs out. That is also the anti-ban lever: a
single clean mint instead of one challenge per signup.
"""

from __future__ import annotations

import json
import threading
import time
from typing import Any

import httpx

from .errors import AppCheckRequired, CaptchaRequired, Transport
from .net import build_client, request

PROJECT_ID = "genius-ai-app-test-1"
APP_ID = "1:387451343950:web:7098a29d2a2aa91c751f1f"
WEB_API_KEY = "AIzaSyAdbIs_1isauoMViqf4rmpGUH5UFb3z_JQ"
RECAPTCHA_SITE_KEY = "6LcitAstAAAAAOYKi-GIZSRHifuDvgNfcp6fFTeN"

EXCHANGE = (
    "https://firebaseappcheck.googleapis.com/v1/projects/{project}"
    "/apps/{app}:exchangeRecaptchaEnterpriseToken?key={key}"
).format(project=PROJECT_ID, app=APP_ID.replace(":", "%3A"), key=WEB_API_KEY)

# The exchange endpoint lives on Google's own origin, which a captive proxy
# handles fine; this is the same client the rest of the engine uses.
CAPSOLVER_CREATE = "https://api.capsolver.com/createTask"
CAPSOLVER_RESULT = "https://api.capsolver.com/getTaskResult"


def exchange(recaptcha_token: str, *, proxy: str = "", timeout: float = 30.0) -> dict[str, Any]:
    """Swap a reCAPTCHA Enterprise token for an App Check JWT."""
    client = build_client(proxy=proxy, timeout=timeout)
    try:
        response = request(
            client,
            "POST",
            EXCHANGE,
            json={"recaptcha_enterprise_token": recaptcha_token},
        )
        if response.status_code >= 400:
            raise AppCheckRequired(
                f"app check exchange refused (HTTP {response.status_code})",
                detail=response.text[:300],
            )
        payload = response.json() or {}
        token = str(payload.get("token") or "")
        if not token:
            raise AppCheckRequired("app check exchange returned no token", detail=str(payload)[:200])
        return {"token": token, "ttl": _ttl_seconds(payload.get("ttl"))}
    finally:
        client.close()


def _ttl_seconds(raw: Any) -> int:
    """`"3600s"` -> 3600; anything unparsable falls back to 55 minutes."""
    try:
        return int(str(raw).rstrip("s")) or 3300
    except (TypeError, ValueError):
        return 3300


def mint_from_browser(*, proxy: str = "", timeout: float = 120.0, headless: bool = True) -> dict[str, Any]:
    """Load the SPA once and read the token it exchanges for itself.

    The page calls `appCheck().getToken()` during boot; the response body of
    `content-firebaseappcheck…/exchangeRecaptchaEnterpriseToken` is the JWT. No
    UI interaction is needed, which keeps this usable from a server.
    """
    try:
        from camoufox.sync_api import Camoufox  # type: ignore import-not-found
    except ImportError as exc:  # pragma: no cover - depends on env
        raise CaptchaRequired(
            "camoufox is not installed; `pip install camoufox` or use --captcha capsolver"
        ) from exc

    captured: dict[str, Any] = {}
    deadline = time.time() + timeout
    kwargs: dict[str, Any] = {"headless": headless, "humanize": True, "os": "windows", "geoip": True}
    if proxy:
        kwargs["proxy"] = {"server": proxy}

    with Camoufox(**kwargs) as browser:
        page = browser.new_page()

        def on_response(response: Any) -> None:
            if "firebaseappcheck" in response.url and "token" not in captured:
                try:
                    payload = json.loads(response.text())
                except Exception:  # noqa: BLE001 - a body may already be consumed
                    return
                token = str(payload.get("token") or "")
                if token:
                    captured["token"] = token
                    captured["ttl"] = _ttl_seconds(payload.get("ttl"))

        page.on("response", on_response)
        page.goto("https://cleverai.app/app/chat", wait_until="domcontentloaded", timeout=timeout * 1000)
        while time.time() < deadline and "token" not in captured:
            page.wait_for_timeout(1000)

    if "token" not in captured:
        raise CaptchaRequired("the SPA never minted an App Check token in this session")
    return {"token": captured["token"], "ttl": captured.get("ttl", 3300)}


def mint_from_capsolver(
    api_key: str,
    *,
    proxy: str = "",
    timeout: float = 180.0,
    poll: float = 3.0,
    site_url: str = "https://cleverai.app/app/chat",
    action: str = "firebase_app_check",
) -> dict[str, Any]:
    """Ask capsolver for a reCAPTCHA Enterprise token, then exchange it ourselves."""
    if not api_key:
        raise CaptchaRequired("capsolver needs an api key")
    client = build_client(proxy=proxy, timeout=40.0)
    try:
        task: dict[str, Any] = {
            "type": "ReCaptchaV3EnterpriseTask",
            "websiteURL": site_url,
            "websiteKey": RECAPTCHA_SITE_KEY,
            "pageAction": action,
            "minScore": 0.7,
        }
        if proxy:
            # A proxyless solve is scored against capsolver's own IP; binding the
            # solve to the same exit the signup uses keeps the score consistent.
            task["type"] = "ReCaptchaV3EnterpriseTaskProxyLess"
        created = request(client, "POST", CAPSOLVER_CREATE, json={"clientKey": api_key, "task": task})
        body = created.json() if created.content else {}
        if created.status_code >= 400 or body.get("errorId"):
            raise CaptchaRequired(
                f"capsolver refused the task: {body.get('errorCode') or created.status_code}",
                detail=str(body)[:300],
            )
        task_id = str(body.get("taskId") or "")
        if not task_id:
            raise CaptchaRequired("capsolver returned no taskId", detail=str(body)[:200])

        deadline = time.time() + timeout
        while time.time() < deadline:
            time.sleep(poll)
            polled = request(
                client, "POST", CAPSOLVER_RESULT, json={"clientKey": api_key, "taskId": task_id}
            )
            payload = polled.json() if polled.content else {}
            if payload.get("errorId"):
                raise CaptchaRequired(f"capsolver poll failed: {payload.get('errorCode')}")
            if payload.get("status") == "ready":
                solution = (payload.get("solution") or {}).get("gRecaptchaResponse") or ""
                if not solution:
                    raise CaptchaRequired("capsolver produced an empty token")
                return exchange(solution, proxy=proxy)
        raise CaptchaRequired("capsolver timed out")
    finally:
        client.close()


class AppCheckVault:
    """Thread-safe cache around one App Check token.

    One token per batch is enough: the backend ties it to the app install, not
    the account. Refreshing 60 s before the TTL keeps a long batch from dying
    mid-run.
    """

    def __init__(
        self,
        *,
        provider: str = "browser",
        api_key: str = "",
        proxy: str = "",
        headless: bool = True,
        log: Any = None,
    ) -> None:
        self.provider = (provider or "browser").strip().lower()
        self.api_key = api_key
        self.proxy = proxy
        self.headless = headless
        self.log = log or (lambda *_: None)
        self._token = ""
        self._expires_at = 0.0
        self._lock = threading.Lock()
        self._mints = 0

    # ------------------------------------------------------------------ state
    @property
    def stats(self) -> dict[str, Any]:
        return {
            "mints": self._mints,
            "has_token": bool(self._token),
            "expires_in": max(0, int(self._expires_at - time.time())),
        }

    def seed(self, token: str, ttl: int = 3300) -> None:
        """Test hook / manual seed: reuse a token minted elsewhere."""
        with self._lock:
            self._token = token
            self._expires_at = time.time() + ttl

    # ------------------------------------------------------------------- main
    def get(self, *, force: bool = False, proxy: str = "") -> str:
        with self._lock:
            if not force and self._token and time.time() < self._expires_at - 60:
                return self._token
            self.log("info", f"App Check 令牌续期（provider={self.provider}）")
            minted = self._mint(proxy=proxy or self.proxy)
            self._token = minted["token"]
            self._expires_at = time.time() + int(minted.get("ttl", 3300))
            self._mints += 1
            self.log("info", f"App Check 就绪，{self._expires_at - time.time():.0f}s 后过期")
            return self._token

    def _mint(self, *, proxy: str) -> dict[str, Any]:
        if self.provider == "capsolver":
            return mint_from_capsolver(self.api_key, proxy=proxy)
        if self.provider == "manual":
            raise CaptchaRequired("provider=manual needs `seed()` or a literal token in config")
        try:
            return mint_from_browser(proxy=proxy, headless=self.headless)
        except CaptchaRequired as exc:
            if not self.api_key:
                raise
            self.log("warn", f"浏览器取令牌失败（{exc}），改用 capsolver")
            return mint_from_capsolver(self.api_key, proxy=proxy)


def probe(*, proxy: str = "", token: str = "") -> dict[str, Any]:
    """`doctor`: is the project's App Check gate reachable and mappable?"""
    out: dict[str, Any] = {
        "project": PROJECT_ID,
        "app_id": APP_ID,
        "recaptcha_site_key": RECAPTCHA_SITE_KEY,
        "exchange": EXCHANGE.split("?")[0],
    }
    client = build_client(proxy=proxy, timeout=20.0)
    try:
        health = request(client, "GET", "https://api.cleverai.app/")
        out["backend"] = f"HTTP {health.status_code} {len(health.text or '')} bytes"
        if token:
            try:
                minted = exchange(token, proxy=proxy)
                out["exchange_ok"] = True
                out["ttl"] = minted["ttl"]
            except AppCheckRequired as exc:
                out["exchange_ok"] = False
                out["exchange_error"] = str(exc)[:200]
    except Transport as exc:
        out["backend"] = f"unreachable: {exc}"
    finally:
        client.close()
    return out
