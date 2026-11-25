"""Camoufox session for Poe: earn Cloudflare clearance, then drive DOM or GraphQL.

Poe sits behind a Cloudflare managed challenge *and* a reCAPTCHA-gated signup, so
both engines start the same way — a real Firefox fingerprint through the account's
proxy — and differ only in how they talk to the app afterwards:

  PoeBrowser   fills the real form (repair-typing, because Poe's controlled input
               drops keystrokes while hydrating)
  PoeProtocol  calls /api/gql_POST from inside the page with the session's own
               `poe-formkey` and a fresh reCAPTCHA token
"""

from __future__ import annotations

import json
import random
import re
import time
from pathlib import Path
from typing import Any

from .errors import CloudflareBlocked, PoeError, Transport
from .net import proxy_dict

LOGIN_URL = "https://poe.com/login?redirect_url=%2F"

STATE_JS = """() => {
  const inputs = [...document.querySelectorAll('input')].map(i => [i.type, i.placeholder || '']);
  const buttons = [...document.querySelectorAll('button')].map(b => (b.innerText || '').trim()).filter(Boolean);
  const email = document.querySelector('input[type=email]');
  const code = document.querySelector('input[placeholder=Code]');
  const tel = document.querySelector('input[type=tel]');
  return {
    url: location.href,
    email: email ? email.value : null,
    code: code ? code.value : null,
    tel: tel ? tel.value : null,
    inputs, buttons: buttons.slice(0, 14),
    body: (document.body.innerText || '').slice(-600),
    blocked: /just a moment|attention required|verify you are human/i.test(document.body.innerText || ''),
  };
}"""

SITEKEY_JS = """() => {
  const direct = window.__RECAPTCHA_SITE_KEY__ || window.RECAPTCHA_SITE_KEY ||
                 window.GOOGLE_RECAPTCHA_SITE_KEY || '';
  const dom = document.querySelector('[data-sitekey]');
  const fromDom = dom ? dom.getAttribute('data-sitekey') : '';
  const html = document.documentElement.innerHTML;
  const m = html.match(/6L[a-zA-Z0-9_-]{38}/);
  const next = window.__NEXT_DATA__ ? JSON.stringify(window.__NEXT_DATA__).match(/6L[a-zA-Z0-9_-]{38}/) : null;
  return {direct, fromDom, fromHtml: m ? m[0] : '', fromNext: next ? next[0] : ''};
}"""

RECAPTCHA_JS = """async ({key, action}) => {
  if (!window.grecaptcha) return {error: 'no grecaptcha'};
  await new Promise((resolve) => grecaptcha.ready(resolve));
  try {
    const token = await grecaptcha.execute(key, {action});
    return {token};
  } catch (e) {
    return {error: String(e)};
  }
}"""

GQL_JS = """async ({body, formkey, tag}) => {
  const r = await fetch('/api/gql_POST', {
    method: 'POST',
    credentials: 'include',
    headers: {
      'content-type': 'application/json',
      'poe-formkey': formkey,
      'poe-tag-id': tag,
      'x-poe-web-request': 'true',
    },
    body: JSON.stringify(body),
  });
  let text = '';
  try { text = await r.text(); } catch (e) { text = '<unreadable>'; }
  return {status: r.status, text};
}"""


class PoeSession:
    """Owns one browser, one page, one proxy, one cookie jar."""

    def __init__(
        self,
        *,
        proxy: str = "",
        headless: bool = True,
        geoip: bool = True,
        locale: str = "en-US",
        os_list: list[str] | None = None,
        screenshots: Path | None = None,
        log=None,
        rng: random.Random | None = None,
    ) -> None:
        self.proxy = proxy
        self.headless = headless
        self.geoip = geoip
        self.locale = locale
        self.os_list = os_list or ["windows"]
        self.screenshots = Path(screenshots) if screenshots else None
        self.log = log or (lambda *_a, **_k: None)
        self.rng = rng or random.Random()

        self._browser = None
        self._context = None
        self.page = None
        self.formkey = ""
        self._cookies: dict[str, str] = {}
        self._launch_error: str = ""

    # ------------------------------------------------------------- lifecycle
    def open(self, *, clear_wait: float = 75.0) -> "PoeSession":
        from camoufox.sync_api import Camoufox  # optional dependency
        from camoufox.fingerprints import Screen

        launch: dict[str, Any] = {
            "headless": self.headless,
            "humanize": 2.2,
            "os": self.os_list,
            "locale": [self.locale],
            "geoip": bool(self.geoip and self.proxy),
            "block_webrtc": True,
            "fingerprint_preset": True,
            "screen": Screen(min_width=1280, min_height=720, max_width=1920, max_height=1080),
            "window": (1366, 768),
            "pin_cpu_cores": True,
            "enable_cache": True,
        }
        proxy = proxy_dict(self.proxy)
        if proxy:
            launch["proxy"] = proxy

        self.log("session: launching Camoufox")
        self._cm = Camoufox(**launch)
        self._browser = self._cm.__enter__()
        contexts = list(getattr(self._browser, "contexts", []) or [])
        self._context = contexts[0] if contexts else self._browser.new_context()
        self.page = self._context.new_page()
        self.page.set_default_timeout(45_000)
        self.page.on("request", self._sniff_formkey)

        self.page.goto(LOGIN_URL, wait_until="domcontentloaded", timeout=90_000)
        self._wait_clear(clear_wait)
        self._await_formkey(15.0)
        self._cookies = self._cookie_map()
        if not self.formkey:
            self._formkey_from_page()
        self.log(
            f"session: ready (clearance={'yes' if self._cookies.get('cf_clearance') else 'no'}, "
            f"formkey={'yes' if self.formkey else 'no'})"
        )
        return self

    def close(self) -> None:
        try:
            if self._context is not None:
                self._cookies = self._cookies or self._cookie_map()
        except Exception:  # noqa: BLE001 - closing a dead context raises
            pass
        manager = getattr(self, "_cm", None)
        if manager is not None:
            try:
                manager.__exit__(None, None, None)
            except Exception:  # noqa: BLE001
                pass
            self._cm = None
        self._browser = None
        self._context = None
        self.page = None

    def __enter__(self) -> "PoeSession":
        return self.open()

    def __exit__(self, *exc: object) -> None:
        self.close()

    # --------------------------------------------------------------- helpers
    def _sniff_formkey(self, request) -> None:
        try:
            if "/api/gql_POST" in request.url:
                key = request.headers.get("poe-formkey")
                if key and not self.formkey:
                    self.formkey = key
                    self.log(f"session: captured formkey {key[:8]}…")
        except Exception:  # noqa: BLE001 - header access on dead requests raises
            pass

    def _await_formkey(self, budget: float) -> None:
        """The app signs its own bootstrap queries; that request carries the formkey."""
        deadline = time.time() + budget
        while time.time() < deadline and not self.formkey:
            self.page.wait_for_timeout(700)
            self._formkey_from_page()

    def _formkey_from_page(self) -> None:
        try:
            value = self.page.evaluate(
                """() => document.querySelector('meta[name="poe-formkey"]')?.content
                     || window.__poe_formkey || window.formkey || ''"""
            )
            if value:
                self.formkey = str(value)
        except Exception:  # noqa: BLE001
            pass

    def _wait_clear(self, budget: float) -> None:
        started = time.time()
        deadline = started + budget
        reloads = 0
        while time.time() < deadline:
            try:
                state = self.page.evaluate(STATE_JS)
            except Exception:  # noqa: BLE001 - navigation in flight
                self.page.wait_for_timeout(1500)
                continue
            if not state.get("blocked") and state.get("email") is not None:
                return
            # A stuck interstitial often clears on a second document load.
            if reloads < 2 and time.time() - started > 30 * (reloads + 1):
                reloads += 1
                self.log(f"session: challenge still up, reloading ({reloads})")
                try:
                    self.page.reload(wait_until="domcontentloaded", timeout=60_000)
                except Exception:  # noqa: BLE001
                    pass
            self.page.wait_for_timeout(1500)
        self.screenshot("cloudflare")
        raise CloudflareBlocked("Cloudflare challenge did not clear", detail=self._launch_error)

    def _cookie_map(self) -> dict[str, str]:
        try:
            return {c["name"]: c["value"] for c in self._context.cookies()}
        except Exception:  # noqa: BLE001
            return {}

    def cookies(self) -> dict[str, str]:
        current = self._cookie_map()
        if current:
            self._cookies = current
        return self._cookies

    def state(self) -> dict[str, Any]:
        try:
            return self.page.evaluate(STATE_JS)
        except Exception as exc:  # noqa: BLE001
            return {"error": str(exc)[:200]}

    def screenshot(self, tag: str) -> str:
        if not self.screenshots:
            return ""
        self.screenshots.mkdir(parents=True, exist_ok=True)
        path = self.screenshots / f"poe-{tag}-{time.strftime('%Y%m%d-%H%M%S')}.png"
        try:
            self.page.screenshot(path=str(path), full_page=True)
            self.log(f"session: screenshot {path}")
            return str(path)
        except Exception:  # noqa: BLE001
            return ""

    # ------------------------------------------------------------- protocol
    def recaptcha_token(self, action: str = "submit") -> str:
        info = self.page.evaluate(SITEKEY_JS)
        key = info.get("direct") or info.get("fromDom") or info.get("fromHtml") or info.get("fromNext") or ""
        if not key:
            raise PoeError("reCAPTCHA site key not found on the page", detail=info)
        result = self.page.evaluate(RECAPTCHA_JS, {"key": key, "action": action})
        if not result.get("token"):
            raise PoeError(f"reCAPTCHA token failed: {result.get('error')}", detail=result)
        return str(result["token"])

    def gql(self, body: dict[str, Any]) -> dict[str, Any]:
        """Signed in-page POST. Uses the session's formkey; tag id is per request."""
        if not self.formkey:
            self._formkey_from_page()
        if not self.formkey:
            raise PoeError("no poe-formkey captured; the page never issued a GraphQL call")
        tag = f"{self.rng.getrandbits(128):032x}"[:32]
        result = self.page.evaluate(GQL_JS, {"body": body, "formkey": self.formkey, "tag": tag})
        text = result.get("text") or ""
        if result.get("status") != 200:
            raise PoeError(f"gql_POST HTTP {result.get('status')}", detail=text[:300])
        try:
            payload = json.loads(text)
        except json.JSONDecodeError as exc:
            raise PoeError("gql_POST returned non-JSON", detail=text[:300]) from exc
        if payload.get("errors"):
            raise PoeError(f"gql_POST error: {payload['errors'][:1]}", detail=payload)
        return payload

    # ---------------------------------------------------------------- typing
    def set_field(self, selector: str, value: str, *, tries: int = 5, delay: int = 110) -> bool:
        """Poe's controlled inputs drop keystrokes while the page hydrates.

        Type, read back, and if the value is a prefix of the target append the
        remainder; retry from scratch otherwise. A plain `fill()` leaves React's
        state empty (the submit button stays disabled), so keystrokes are required.
        """
        field = self.page.locator(selector).first
        for attempt in range(tries):
            field.click()
            self.page.keyboard.press("Control+A")
            self.page.keyboard.press("Delete")
            self.page.wait_for_timeout(self.rng.randint(120, 260))
            for char in value:
                self.page.keyboard.type(char)
                self.page.wait_for_timeout(self.rng.randint(delay // 2, delay))
            self.page.wait_for_timeout(self.rng.randint(250, 500))
            got = field.input_value()
            if got == value:
                return True
            if value.startswith(got):
                self.page.keyboard.press("End")
                for char in value[len(got):]:
                    self.page.keyboard.type(char)
                    self.page.wait_for_timeout(self.rng.randint(delay // 2, delay + 40))
                self.page.wait_for_timeout(300)
                if field.input_value() == value:
                    return True
            self.log(f"session: field {selector} drifted ({len(got)}/{len(value)}), retry {attempt + 1}")
        return False

    def click_text(self, text: str, *, exact: bool = True, timeout: int = 15_000) -> bool:
        try:
            self.page.get_by_role("button", name=text, exact=exact).first.click(timeout=timeout)
            return True
        except Exception:  # noqa: BLE001
            return False

    def submit_email_and_capture(self, email: str, *, timeout: int = 45_000) -> dict[str, Any]:
        """Let the app submit the email and hand back the mutation's JSON.

        Poe only loads reCAPTCHA at submit time, so this is the one place the
        protocol engine borrows the DOM: one click buys a genuine token plus the
        `sendVerificationCode` response, everything afterwards is pure GraphQL.
        """
        if not self.set_field("input[type=email]", email):
            raise PoeError("could not type the email into Poe's field")
        self.page.wait_for_timeout(self.rng.randint(300, 700))
        predicate = lambda r: (
            "/api/gql_POST" in r.url and "sendVerificationCode" in (r.request.post_data or "")
        )
        with self.page.expect_response(predicate, timeout=timeout) as info:
            if not self.click_text("Go"):
                self.page.keyboard.press("Enter")
        response = info.value
        try:
            payload = json.loads(response.text())
        except json.JSONDecodeError as exc:
            raise PoeError("sendVerificationCode returned non-JSON", detail=response.text()[:200]) from exc
        node = (payload.get("data") or {}).get("sendVerificationCode") or {}
        return node


class PoeBrowser:
    """DOM engine: same steps a person takes."""

    def __init__(self, session: PoeSession, log=None) -> None:
        self.session = session
        self.log = log or (lambda *_a, **_k: None)

    def submit_email_and_capture(self, email: str, **kwargs) -> dict[str, Any]:
        return self.session.submit_email_and_capture(email, **kwargs)

    def submit_email(self, email: str) -> dict[str, Any]:
        if not self.session.set_field("input[type=email]", email):
            raise PoeError("could not type the email into Poe's field")
        self.session.page.wait_for_timeout(500)
        if not self.session.click_text("Go"):
            self.session.page.keyboard.press("Enter")
        self.session.page.wait_for_timeout(6000)
        return self.session.state()

    def enter_code(self, code: str) -> dict[str, Any]:
        if not self.session.set_field("input[placeholder=Code]", code, delay=90):
            raise PoeError("could not type the verification code")
        self.session.page.wait_for_timeout(400)
        if not self.session.click_text("Verify"):
            self.session.page.keyboard.press("Enter")
        self.session.page.wait_for_timeout(6000)
        return self.session.state()

    def add_phone(self, phone: str) -> bool:
        digits = re.sub(r"[^\d]", "", phone)
        if not self.session.set_field("input[type=tel]", digits, delay=90):
            return False
        self.session.page.wait_for_timeout(300)
        return self.session.click_text("Add")

