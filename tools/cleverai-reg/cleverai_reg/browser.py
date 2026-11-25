"""指纹浏览器引擎 — 第二条注册腿，也是 App Check 令牌的唯一干净来源。

Why it exists at all: the appserver refuses writes without `X-Firebase-AppCheck`,
and that token is only issued after a real reCAPTCHA Enterprise challenge on
cleverai.app's own origin. A plain HTTP client cannot mint one. So this engine
opens the real site with a coherent fingerprint (Camoufox rewrites canvas, WebGL,
fonts, timezone and audio in C++), mints the token the SPA mints anyway, and then
either

  * 注册：in the page context, so cookies / Referer / Client-Hints all look native, or
  * 交出令牌：给协议引擎复用，剩下几十个账号走纯 HTTP。

Stack choice mirrors `allmodels-reg`: camoufox > patchright > playwright, picked
by `available_stacks()` and overridable with `--browser-stack`.
"""

from __future__ import annotations

import json
import os
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from .errors import AppCheckRequired, Blocked, CaptchaRequired, SignupError

APP_ID = "1:387451343950:web:7098a29d2a2aa91c751f1f"
WEB_API_KEY = "AIzaSyAdbIs_1isauoMViqf4rmpGUH5UFb3z_JQ"
CHAT_URL = "https://cleverai.app/app/chat"


class BrowserUnavailable(SignupError):
    kind = "browser_unavailable"


def available_stacks() -> list[str]:
    """Which browser stacks this interpreter can actually start."""
    out: list[str] = []
    try:
        import camoufox  # noqa: F401

        out.append("camoufox")
    except ImportError:
        pass
    try:
        import patchright  # noqa: F401

        out.append("patchright")
    except ImportError:
        pass
    try:
        import playwright  # noqa: F401

        out.append("playwright")
    except ImportError:
        pass
    return out


@dataclass(slots=True)
class BrowserSignup:
    email: str
    password: str
    uid: str = ""
    id_token: str = ""
    refresh_token: str = ""
    app_check: str = ""
    app_check_ttl: int = 3300
    profile: dict[str, Any] = field(default_factory=dict)
    device: dict[str, Any] = field(default_factory=dict)
    screenshots: list[str] = field(default_factory=list)


class FingerprintBrowser:
    """One browser context per account; never reused, never persisted by default."""

    def __init__(
        self,
        *,
        stack: str = "auto",
        headless: bool = True,
        proxy: str = "",
        humanize: float = 1.0,
        geoip: bool = True,
        cdp: str = "",
        profile_dir: str = "",
        output_dir: Path | str = "out",
        timeout: float = 120.0,
        log: Any = None,
    ) -> None:
        self.stack = (stack or "auto").strip().lower()
        self.headless = headless
        self.proxy = proxy
        self.humanize = humanize
        self.geoip = geoip
        self.cdp = cdp
        self.profile_dir = profile_dir
        self.output_dir = Path(output_dir)
        self.timeout = timeout
        self.log = log or (lambda *_: None)
        self._ctx: Any = None
        self._browser: Any = None
        self._playwright: Any = None

    # ------------------------------------------------------------------ setup
    def _pick(self) -> str:
        stacks = available_stacks()
        if not stacks:
            raise BrowserUnavailable(
                "没有可用的浏览器内核：pip install 'camoufox[geoip]' 或 playwright + playwright install chromium"
            )
        if self.stack in ("auto", ""):
            return stacks[0]
        if self.stack not in stacks:
            raise BrowserUnavailable(f"要求的内核 {self.stack} 不可用，现有：{', '.join(stacks)}")
        return self.stack

    def __enter__(self) -> "FingerprintBrowser":
        self.open()
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()

    def open(self) -> None:
        if self._browser is not None:
            return
        stack = self._pick()
        self.log("info", f"启动指纹浏览器：{stack}（headless={self.headless}）")
        if self.cdp:
            self._open_cdp()
            return
        if stack == "camoufox":
            self._open_camoufox()
        else:
            self._open_chromium(stack)

    def _proxy_arg(self) -> dict[str, Any] | None:
        return {"server": self.proxy} if self.proxy else None

    def _open_camoufox(self) -> None:
        from camoufox.sync_api import Camoufox  # type: ignore import-not-found

        kwargs: dict[str, Any] = {
            "headless": self.headless,
            "humanize": bool(self.humanize),
            "os": ("windows", "macos", "linux")[abs(hash(self.proxy)) % 3] if self.proxy else "windows",
            "geoip": self.geoip,
        }
        proxy = self._proxy_arg()
        if proxy:
            kwargs["proxy"] = proxy
        if self.profile_dir:
            kwargs["persistent_context"] = True
            kwargs["user_data_dir"] = str(Path(self.profile_dir).expanduser())
        self._browser = Camoufox(**kwargs)
        self._browser.__enter__()

    def _open_chromium(self, stack: str) -> None:
        if stack == "patchright":
            from patchright.sync_api import sync_playwright  # type: ignore import-not-found
        else:
            from playwright.sync_api import sync_playwright  # type: ignore import-not-found

        self._playwright = sync_playwright().start()
        launch: dict[str, Any] = {
            "headless": self.headless,
            "args": [
                "--disable-blink-features=AutomationControlled",
                "--no-first-run",
                "--no-default-browser-check",
            ],
        }
        proxy = self._proxy_arg()
        if proxy:
            launch["proxy"] = proxy
        if self.profile_dir:
            self._browser = self._playwright.chromium.launch_persistent_context(
                str(Path(self.profile_dir).expanduser()), **launch
            )
        else:
            self._browser = self._playwright.chromium.launch(**launch)

    def _open_cdp(self) -> None:
        from playwright.sync_api import sync_playwright  # type: ignore import-not-found

        self._playwright = sync_playwright().start()
        self._browser = self._playwright.chromium.connect_over_cdp(self.cdp)
        self.log("info", f"已接管外部指纹浏览器 CDP：{self.cdp}")

    def new_page(self) -> Any:
        if self._browser is None:
            self.open()
        # persistent_context 和 connect_over_cdp 返回的就是 context
        if hasattr(self._browser, "new_page"):
            try:
                return self._browser.new_page()
            except Exception:  # noqa: BLE001 - context without new_page support
                pass
        contexts = getattr(self._browser, "contexts", None)
        if contexts:
            return contexts[0].new_page()
        return self._browser.new_context().new_page()

    # ------------------------------------------------------------------- work
    def appcheck_token(self, *, timeout: float | None = None) -> tuple[str, int]:
        """Open the app once and read the App Check JWT the SPA exchanges for."""
        page = self.new_page()
        captured: dict[str, Any] = {}
        deadline = time.time() + (timeout or self.timeout)

        def on_response(response: Any) -> None:
            if "firebaseappcheck" in response.url and "token" not in captured:
                try:
                    payload = json.loads(response.text())
                except Exception:  # noqa: BLE001 - body already consumed
                    return
                token = str(payload.get("token") or "")
                if token:
                    captured["token"] = token
                    captured["ttl"] = int(str(payload.get("ttl", "3300s")).rstrip("s") or 3300)

        page.on("response", on_response)
        try:
            page.goto(CHAT_URL, wait_until="domcontentloaded", timeout=self.timeout * 1000)
            while time.time() < deadline and "token" not in captured:
                page.wait_for_timeout(1000)
        finally:
            self._close_page(page)
        if "token" not in captured:
            raise CaptchaRequired("页面没有产出 App Check 令牌（可能被 reCAPTCHA 降分）")
        return captured["token"], captured.get("ttl", 3300)

    def signup(self, email: str, password: str, *, verify: bool = True) -> BrowserSignup:
        """Firebase signup executed inside the page context.

        Running it from the origin means the request carries the site's own
        Origin/Referer/Client-Hints, which is what keeps a batch from looking
        like a script farm. Tokens are read straight out of the response.
        """
        page = self.new_page()
        result = BrowserSignup(email=email, password=password)
        captured: dict[str, Any] = {}

        def on_response(response: Any) -> None:
            if "firebaseappcheck" in response.url and "token" not in captured:
                try:
                    payload = json.loads(response.text())
                    token = str(payload.get("token") or "")
                    if token:
                        captured["app_check"] = token
                        captured["ttl"] = int(str(payload.get("ttl", "3300s")).rstrip("s") or 3300)
                except Exception:  # noqa: BLE001
                    pass

        page.on("response", on_response)
        try:
            page.goto(CHAT_URL, wait_until="domcontentloaded", timeout=self.timeout * 1000)
            page.wait_for_timeout(4000)
            payload = page.evaluate(
                """async ([key, email, password]) => {
                    const r = await fetch('https://identitytoolkit.googleapis.com/v1/accounts:signUp?key=' + key,
                        {method:'POST', headers:{'Content-Type':'application/json'},
                         body: JSON.stringify({email, password, returnSecureToken:true})});
                    return {status: r.status, body: await r.text()};
                }""",
                [WEB_API_KEY, email, password],
            )
            status = int(payload.get("status") or 0)
            body = payload.get("body") or ""
            if status >= 400:
                lowered = body.lower()
                if "email_exists" in lowered or "already" in lowered:
                    raise SignupError(f"地址已注册：{email}")
                if "too_many_attempts" in lowered or status == 429:
                    raise Blocked("identity toolkit 限流（该出口已脏）")
                raise SignupError(f"页面内注册失败 HTTP {status}: {body[:200]}")
            data = json.loads(body)
            result.uid = str(data.get("localId") or "")
            result.id_token = str(data.get("idToken") or "")
            result.refresh_token = str(data.get("refreshToken") or "")
            page.wait_for_timeout(1500)
            result.app_check = str(captured.get("app_check") or "")
            result.app_check_ttl = int(captured.get("ttl", 3300))

            # 建号：profile + settings，全部走页面内 fetch，鉴权头照抄 SPA
            prof = page.evaluate(
                """async ([appId, uid, idToken, appCheck]) => {
                    const h = {'Authorization': 'Bearer ' + idToken,
                               'x-functions-key': (os.environ.get('CLEVERAI_FUNCTIONS_KEY') or '').strip(),
                               'Version': '12.2.0', 'X-Version': '12.2.0',
                               'X-App-ID': appId, 'X-UID': uid, 'X-Platform': 'web',
                               'X-Firebase-AppCheck': appCheck,
                               'Accept-Language': 'en'};
                    const fd = new FormData();
                    fd.append('json', JSON.stringify({uid, appId, platform: 'web', isAnonymousUser: false}));
                    const r = await fetch('https://api.cleverai.app/appserver/api/v1/users',
                        {method: 'POST', headers: h, body: fd});
                    return {status: r.status, body: await r.text()};
                }""",
                [APP_ID, result.uid, result.id_token, result.app_check],
            )
            if int(prof.get("status") or 0) != 201:
                raise SignupError(f"建号失败 HTTP {prof.get('status')}: {str(prof.get('body'))[:200]}")
            result.profile = json.loads(prof.get("body") or "{}")
            if self.log:
                self.log("info", f"浏览器建号成功 {email} uid={result.uid}")
        finally:
            self._close_page(page)
        return result

    def warmup(self, email: str, password: str, *, engine: str = "openai-gpt-6-luna") -> bool:
        """注册后先跑一次真实读请求：新号立刻发长文比静置更像活人。"""
        page = self.new_page()
        try:
            page.goto(CHAT_URL, wait_until="domcontentloaded", timeout=self.timeout * 1000)
            page.wait_for_timeout(3000)
            ok = page.evaluate(
                """async ([key, email, password]) => {
                    const r = await fetch('https://identitytoolkit.googleapis.com/v1/accounts:signInWithPassword?key=' + key,
                        {method:'POST', headers:{'Content-Type':'application/json'},
                         body: JSON.stringify({email, password, returnSecureToken:true})});
                    return r.status;
                }""",
                [WEB_API_KEY, email, password],
            )
            page.wait_for_timeout(2500)
            return int(ok or 0) == 200
        except Exception as exc:  # noqa: BLE001 - warmup is best effort
            self.log("warn", f"预热失败（忽略）：{exc}")
            return False
        finally:
            self._close_page(page)

    def screenshot(self, label: str) -> str:
        page = self.new_page()
        try:
            page.goto(CHAT_URL, wait_until="domcontentloaded", timeout=self.timeout * 1000)
            page.wait_for_timeout(2500)
            target = self.output_dir / "screenshots"
            target.mkdir(parents=True, exist_ok=True)
            path = target / f"{label}-{int(time.time())}.png"
            page.screenshot(path=str(path))
            return str(path)
        finally:
            self._close_page(page)

    def _close_page(self, page: Any) -> None:
        try:
            page.close()
        except Exception:  # noqa: BLE001
            pass

    def close(self) -> None:
        browser, self._browser = self._browser, None
        if browser is not None:
            for method in ("__exit__", "close"):
                fn = getattr(browser, method, None)
                if fn is None:
                    continue
                try:
                    if method == "__exit__":
                        fn(None, None, None)
                    else:
                        fn()
                    break
                except Exception:  # noqa: BLE001
                    continue
        if self._playwright is not None:
            try:
                self._playwright.stop()
            except Exception:  # noqa: BLE001
                pass
            self._playwright = None


def probe(*, proxy: str = "", headless: bool = True, timeout: float = 90.0) -> dict[str, Any]:
    """`doctor`: which stacks exist and can one of them reach the site?"""
    out: dict[str, Any] = {"stacks": available_stacks()}
    if not out["stacks"]:
        out["ok"] = False
        out["hint"] = "pip install 'camoufox[geoip]'"
        return out
    try:
        with FingerprintBrowser(stack="auto", headless=headless, proxy=proxy, timeout=timeout) as browser:
            token, ttl = browser.appcheck_token()
            out["ok"] = True
            out["app_check_len"] = len(token)
            out["app_check_ttl"] = ttl
    except (BrowserUnavailable, AppCheckRequired) as exc:
        out["ok"] = False
        out["error"] = str(exc)[:200]
    except Exception as exc:  # noqa: BLE001 - doctor reports, never raises
        out["ok"] = False
        out["error"] = f"{type(exc).__name__}: {str(exc)[:160]}"
    return out
