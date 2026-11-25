"""Fingerprint browser fallback for gab.ai.

The worker prefers protocol. If the protocol path hits a bot gate / captcha,
this module can drive the real /signup page to completion and then mint the API
key from inside the logged-in session.

Stacks:
  - Camoufox (Firefox fingerprinting)
  - Playwright Chromium fallback
  - External CDP (AdsPower/BitBrowser/Morelogin...)
"""

from __future__ import annotations

import random
import re
import time
from pathlib import Path
from typing import Any, Callable
from urllib.parse import urlparse

from .errors import Blocked, CaptchaRequired, EmailTaken, NoApiKey, SignupError

NAME_SELECTORS = ("input[autocomplete='name']", "input[name='name']")
EMAIL_SELECTORS = ("input[type='email']", "input[autocomplete='email']")
PASSWORD_SELECTORS = ("input[autocomplete='new-password']", "input[type='password']")
SUBMIT_TEXT = re.compile(r"sign\s*up|create\s+account", re.I)
ERROR_SELECTOR = "[role='alert'], .text-destructive, [data-sonner-toast], .auth-form-message.error"
TURNSTILE_IFRAME = "iframe[src*='challenges.cloudflare.com']"
TURNSTILE_INPUT = "input[name='cf-turnstile-response']"

FETCH_JS = """
async ({ path, method, body, headers }) => {
  const init = { method, credentials: 'include', headers: headers || {} };
  if (body !== null && body !== undefined) {
    init.headers = { ...init.headers, 'content-type': 'application/json' };
    init.body = JSON.stringify(body);
  }
  const response = await fetch(path, init);
  const text = await response.text();
  let json = null;
  try { json = text ? JSON.parse(text) : null; } catch (e) {}
  return { status: response.status, json, text, headers: Object.fromEntries(response.headers.entries()) };
}
"""

TURNSTILE_STATE_JS = """
() => {
  const input = document.querySelector("input[name='cf-turnstile-response']");
  const frame = document.querySelector("iframe[src*='challenges.cloudflare.com']");
  const body = (document.body && document.body.innerText) || '';
  return {
    token: input ? input.value : '',
    widget: Boolean(input || frame),
    challenge: /just a moment|checking your browser|verify you are human/i.test(body),
  };
}
"""


def _proxy_dict(proxy: str) -> dict[str, str] | None:
    if not proxy:
        return None
    parsed = urlparse(proxy if "://" in proxy else f"http://{proxy}")
    server = f"{parsed.scheme}://{parsed.hostname}"
    if parsed.port:
        server += f":{parsed.port}"
    out = {"server": server}
    if parsed.username:
        out["username"] = parsed.username
    if parsed.password:
        out["password"] = parsed.password
    return out


class BrowserUnavailable(SignupError):
    kind = "browser_unavailable"


class FingerprintBrowser:
    def __init__(
        self,
        *,
        base_url: str = "https://gab.ai",
        headless: bool = True,
        proxy: str = "",
        cdp: str = "",
        stack: str = "auto",
        locale: str = "en-US",
        geoip: bool = True,
        timeout: float = 60.0,
        humanize: float = 1.0,
        turnstile_timeout: float = 50.0,
        screenshots: Path | str | None = None,
        log: Callable[[str], None] | None = None,
    ) -> None:
        self.base = base_url.rstrip("/")
        self.headless = headless
        self.proxy = proxy
        self.cdp = cdp
        self.stack_pref = (stack or "auto").strip().lower()
        self.locale = locale
        self.geoip = geoip
        self.timeout = timeout
        self.humanize = max(0.0, float(humanize))
        self.turnstile_timeout = turnstile_timeout
        self.screenshots = Path(screenshots) if screenshots else None
        self.log = log or (lambda _m: None)
        self.rng = random.Random()
        self._pw = None
        self._browser = None
        self._context = None
        self.page = None
        self.stack = ""

    def __enter__(self) -> "FingerprintBrowser":
        self.start()
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()

    def start(self) -> None:
        if self.cdp:
            self._start_cdp()
            return
        if self.stack_pref in ("auto", "camoufox") and self._try_camoufox():
            return
        if self._try_playwright():
            return
        raise BrowserUnavailable("no usable browser stack")

    def _start_cdp(self) -> None:
        try:
            from playwright.sync_api import sync_playwright
        except Exception as exc:
            raise BrowserUnavailable(f"CDP requires playwright: {exc}") from exc
        self._pw = sync_playwright().start()
        self._browser = self._pw.chromium.connect_over_cdp(self.cdp)
        self._context = (self._browser.contexts or [self._browser.new_context()])[0]
        self.page = self._context.pages[0] if self._context.pages else self._context.new_page()
        self.stack = "cdp"
        self.log(f"CDP attached: {self.cdp}")

    def _try_camoufox(self) -> bool:
        try:
            from camoufox.sync_api import Camoufox
            from camoufox.fingerprints import Screen
        except Exception as exc:
            self.log(f"camoufox unavailable ({exc.__class__.__name__})")
            return False
        launch: dict[str, Any] = {
            "headless": self.headless,
            "humanize": 2.0 * max(0.2, self.humanize),
            "os": ["windows", "macos", "linux"],
            "locale": [self.locale],
            "block_webrtc": True,
            "fingerprint_preset": True,
            "screen": Screen(min_width=1280, min_height=720, max_width=1920, max_height=1080),
            "window": (1366, 768),
            "pin_cpu_cores": True,
            "enable_cache": True,
            "geoip": bool(self.geoip and self.proxy),
            "i_know_what_im_doing": True,
        }
        if self.proxy:
            launch["proxy"] = _proxy_dict(self.proxy)
        try:
            self._camoufox = Camoufox(**launch)
            self._browser = self._camoufox.__enter__()
        except Exception as exc:
            self.log(f"camoufox launch failed ({exc})")
            return False
        contexts = list(getattr(self._browser, "contexts", []) or [])
        self._context = contexts[0] if contexts else self._browser.new_context()
        self.page = self._context.new_page()
        self.stack = "camoufox"
        self.log("browser stack: camoufox")
        return True

    def _try_playwright(self) -> bool:
        try:
            from playwright.sync_api import sync_playwright
        except Exception as exc:
            self.log(f"playwright unavailable ({exc.__class__.__name__})")
            return False
        self._pw = sync_playwright().start()
        launch: dict[str, Any] = {
            "headless": self.headless,
            "args": [
                "--disable-blink-features=AutomationControlled",
                "--no-first-run",
                "--no-default-browser-check",
            ],
            "ignore_default_args": ["--enable-automation"],
        }
        if self.proxy:
            launch["proxy"] = _proxy_dict(self.proxy)
        try:
            self._browser = self._pw.chromium.launch(**launch)
        except Exception as exc:
            self.log(f"chromium launch failed ({exc})")
            try:
                self._pw.stop()
            except Exception:
                pass
            self._pw = None
            return False
        viewport = self.rng.choice([{ "width": 1366, "height": 768 }, {"width": 1440, "height": 900}])
        self._context = self._browser.new_context(
            locale=self.locale,
            user_agent=(
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                f"(KHTML, like Gecko) Chrome/{self.rng.choice([133,134,135])}.0.0.0 Safari/537.36"
            ),
            viewport=viewport,
            device_scale_factor=self.rng.choice([1, 1.25]),
        )
        self._context.add_init_script(
            """(() => {
              Object.defineProperty(navigator, 'webdriver', { get: () => undefined });
              Object.defineProperty(navigator, 'languages', { get: () => ['en-US', 'en'] });
              Object.defineProperty(navigator, 'plugins', { get: () => [1,2,3,4,5] });
              const orig = window.chrome ?? {};
              window.chrome = { ...orig, runtime: orig.runtime ?? {} };
            })();"""
        )
        self.page = self._context.new_page()
        self.stack = "playwright"
        self.log("browser stack: playwright")
        return True

    def close(self) -> None:
        try:
            if self._context is not None and self.stack != "cdp":
                self._context.close()
        except Exception:
            pass
        try:
            if getattr(self, "_camoufox", None) is not None:
                self._camoufox.__exit__(None, None, None)
            elif self._browser is not None and self.stack != "cdp":
                self._browser.close()
        except Exception:
            pass
        try:
            if self._pw is not None:
                self._pw.stop()
        except Exception:
            pass
        self._pw = self._browser = self._context = self.page = None

    def _shot(self, tag: str) -> None:
        if not self.screenshots or self.page is None:
            return
        try:
            self.screenshots.mkdir(parents=True, exist_ok=True)
            self.page.screenshot(path=str(self.screenshots / f"{tag}-{int(time.time())}.png"), full_page=False)
        except Exception:
            pass

    def _pause(self, low: int = 90, high: int = 320) -> None:
        if self.humanize <= 0:
            return
        time.sleep(self.rng.randint(low, high) / 1000.0 * self.humanize)

    def _human_type(self, locator: Any, text: str) -> None:
        locator.click()
        self._pause(60, 160)
        locator.fill("")
        for char in text:
            locator.type(char, delay=self.rng.randint(38, 110))
        self._pause()

    def _fill_first(self, selectors: tuple[str, ...], value: str, label: str) -> None:
        for selector in selectors:
            locator = self.page.locator(selector).first
            try:
                if locator.count() and locator.is_visible():
                    self._human_type(locator, value)
                    return
            except Exception:
                continue
        raise SignupError(f"signup form missing {label}")

    def _page_error(self) -> str:
        try:
            node = self.page.locator(ERROR_SELECTOR).first
            if node.count():
                return re.sub(r"\s+", " ", node.inner_text(timeout=800) or "").strip()
        except Exception:
            pass
        return ""

    def _classify(self, message: str) -> None:
        lowered = (message or "").lower()
        if not lowered:
            return
        if "already" in lowered or "exists" in lowered:
            raise EmailTaken(message)
        if "captcha" in lowered or "verification" in lowered:
            raise CaptchaRequired(message)

    def open_signup(self) -> None:
        self.page.goto(f"{self.base}/signup", wait_until="domcontentloaded", timeout=int(self.timeout * 1000))
        if re.search(r"just a moment|checking your browser", (self.page.content() or "")[:3000], re.I):
            raise Blocked("signup page blocked by challenge")
        self._shot(f"signup-{self.stack}")

    def _wait_turnstile(self) -> None:
        deadline = time.time() + self.turnstile_timeout
        clicked = False
        while time.time() < deadline:
            state = self.page.evaluate(TURNSTILE_STATE_JS)
            if state.get("token"):
                self.log("turnstile: token ready")
                return
            if state.get("widget") and not clicked:
                try:
                    frame = self.page.frame_locator(TURNSTILE_IFRAME).first
                    box = frame.locator("input[type='checkbox'], label").first
                    if box.count():
                        box.click(timeout=1500)
                        clicked = True
                        self.log("turnstile: clicked")
                except Exception:
                    clicked = True
            self.page.wait_for_timeout(900)
        self.log("turnstile wait timed out")

    def fill_and_submit(self, *, email: str, password: str) -> None:
        # gab.ai public signup page only asks email/password.
        self._fill_first(EMAIL_SELECTORS, email, "Email")
        pwd = self.page.locator(", ".join(PASSWORD_SELECTORS)).first
        if not pwd.count():
            raise SignupError("password input not found")
        self._human_type(pwd, password)
        self._wait_turnstile()
        button = self.page.get_by_role("button", name=SUBMIT_TEXT).first
        if not button.count():
            raise SignupError("submit button not found")
        button.click()
        self._pause(500, 1200)

    def wait_result(self, *, timeout: float = 60.0) -> None:
        deadline = time.time() + timeout
        while time.time() < deadline:
            url = self.page.url or ""
            if "/app" in url:
                return
            msg = self._page_error()
            if msg:
                self._shot("signup-error")
                self._classify(msg)
                raise SignupError(msg)
            self.page.wait_for_timeout(700)
        raise SignupError("signup did not finish (timeout)")

    def _fetch(self, path: str, *, method: str = "GET", body: Any = None, headers: dict[str, str] | None = None) -> dict[str, Any]:
        result = self.page.evaluate(FETCH_JS, {"path": path, "method": method, "body": body, "headers": headers or {}})
        status = int(result.get("status") or 0)
        if status >= 400:
            payload = result.get("json") or {}
            message = str(payload.get("message") or payload.get("error") or result.get("text") or "")[:300]
            if "captcha" in message.lower():
                raise CaptchaRequired(message)
            raise SignupError(f"{path}: HTTP {status} {message}")
        payload = result.get("json")
        return payload if isinstance(payload, dict) else {}

    def mint_api_key(self, name: str = "probe") -> str:
        keys = self._fetch("/json/api-keys")
        api_keys = keys.get("apiKeys") if isinstance(keys, dict) else None
        if isinstance(api_keys, list) and api_keys:
            # list endpoint returns masked `key`? In practice it returns full for new? keep safe.
            for item in api_keys:
                key = (item or {}).get("key")
                if isinstance(key, str) and key.startswith("gab_"):
                    return key
        created = self._fetch("/json/api-keys", method="POST", body={"name": name})
        key = ((created.get("apiKey") or {}).get("key")) if isinstance(created, dict) else ""
        if not isinstance(key, str) or not key.startswith("gab_"):
            raise NoApiKey("no gab_ key from browser session")
        return key

    def signup_flow(self, *, email: str, password: str, key_name: str = "probe") -> dict[str, Any]:
        self.open_signup()
        self.fill_and_submit(email=email, password=password)
        self.wait_result()
        api_key = self.mint_api_key(name=key_name)
        self._shot(f"signed-{email.split('@')[0]}")
        return {"email": email, "password": password, "api_key": api_key, "api_key_source": "created", "cookies": self.cookies(), "browser": self.stack}

    def cookies(self) -> list[dict[str, str]]:
        try:
            jar = self._context.cookies()
        except Exception:
            return []
        return [
            {"name": str(c.get("name", "")), "value": str(c.get("value", "")), "domain": str(c.get("domain", "")), "path": str(c.get("path", "/"))}
            for c in jar
        ]
