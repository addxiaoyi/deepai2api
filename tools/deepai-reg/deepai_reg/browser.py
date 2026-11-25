"""Camoufox engine: drive the real signup dialog when the bare protocol is refused.

Camoufox is a patched Firefox with a coherent, per-launch fingerprint (canvas,
WebGL, fonts, screen, timezone/locale from the proxy's geo). That is what makes
it worth the extra seconds: Cloudflare's managed challenge and any Turnstile
widget on the dialog see a browser that behaves like a browser.

It returns the same record shape as the protocol engine, so the store, the
exporter and the gateway importer never need to know which path produced it.
"""

from __future__ import annotations

import json
import random
import re
import time
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

from .captcha import INJECT_JS, CaptchaError, solve_turnstile
from .errors import Blocked, EmailRejected, EmailTaken, NoApiKey, SignupError

PROFILE_JS = """
async () => {
  const r = await fetch('https://api.deepai.org/daily-time-sync/user/', {
    credentials: 'include',
    headers: {'Accept': 'application/json, text/plain, */*'},
  });
  const text = await r.text();
  return {status: r.status, body: text};
}
"""

OPEN_DIALOG_JS = """
() => {
  if (typeof openLogin === 'function') { openLogin(); return 'openLogin'; }
  const el = document.getElementById('headerLoginButton');
  if (el) { el.click(); return 'click'; }
  return 'none';
}
"""

SWITCH_TO_SIGNUP_JS = """
() => {
  if (typeof toggleAuthOption === 'function') { toggleAuthOption('signup'); return 'toggle'; }
  const el = document.getElementById('signupToggle');
  if (el) { el.click(); return 'click'; }
  return 'none';
}
"""

SUBMIT_JS = """
() => {
  const dialog = document.querySelector('dialog[open]') || document;
  const candidates = Array.from(dialog.querySelectorAll('button, input[type=submit]'));
  const btn = candidates.find(b => /sign\\s*up|create\\s*account|register/i.test(b.textContent || b.value || ''));
  if (btn) { btn.click(); return 'button'; }
  if (typeof signup === 'function') { signup({preventDefault(){}}); return 'signup()'; }
  return 'none';
}
"""

TURNSTILE_STATE_JS = """
() => {
  const input = document.querySelector('input[name="cf-turnstile-response"]');
  const iframe = document.querySelector('iframe[src*="challenges.cloudflare.com"]');
  return {
    token: input ? input.value : '',
    widget: Boolean(input || iframe),
    challenge: /just a moment|checking your browser|verify you are human/i.test(document.body.innerText || ''),
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


def _read_error(page: Any) -> str:
    try:
        text = page.inner_text("#user-email-error", timeout=1500)
        return re.sub(r"\s+", " ", text).strip()
    except Exception:  # noqa: BLE001 - selector is optional
        return ""


def register_with_browser(
    email: str,
    password: str,
    *,
    proxy: str = "",
    headless: bool = True,
    geoip: bool = True,
    locale: str = "en-US",
    timeout: float = 180.0,
    captcha_key: str = "",
    screenshots: Path | None = None,
    rng: random.Random | None = None,
    log: Any = None,
) -> dict[str, Any]:
    from camoufox.sync_api import Camoufox  # imported late: optional dependency

    rng = rng or random.Random()
    say = log or (lambda *_a, **_k: None)
    shot_dir = Path(screenshots) if screenshots else None
    if shot_dir:
        shot_dir.mkdir(parents=True, exist_ok=True)
    stamp = time.strftime("%Y%m%d-%H%M%S")

    launch: dict[str, Any] = {
        "headless": headless,
        "humanize": True,
        "os": ["windows"],
        "locale": [locale],
        "geoip": bool(geoip),
        "block_webrtc": True,
        "main_world_eval": True,
    }
    proxy_cfg = _proxy_dict(proxy)
    if proxy_cfg:
        launch["proxy"] = proxy_cfg

    with Camoufox(**launch) as browser:
        # Camoufox configures the browser itself; reuse its default context so the
        # generated fingerprint (UA/locale/timezone/screen) is the one in play.
        contexts = list(getattr(browser, "contexts", []) or [])
        context = contexts[0] if contexts else browser.new_context()
        page = context.new_page()
        page.set_default_timeout(45_000)

        try:
            page.goto("https://deepai.org/", wait_until="domcontentloaded", timeout=90_000)
            say("browser: homepage loaded")

            opened = page.evaluate(OPEN_DIALOG_JS)
            page.wait_for_timeout(rng.randint(600, 1400))
            switched = page.evaluate(SWITCH_TO_SIGNUP_JS)
            say(f"browser: dialog {opened}, signup tab {switched}")
            page.wait_for_selector("#user-email", state="visible", timeout=30_000)

            for selector, value in (
                ("#user-email", email),
                ("#user-password", password),
                ("#confirm-user-password", password),
            ):
                locator = page.locator(selector).first
                locator.click()
                locator.fill("")
                page.keyboard.type(value, delay=rng.randint(35, 90))
                page.wait_for_timeout(rng.randint(150, 500))

            state = page.evaluate(TURNSTILE_STATE_JS)
            if state.get("widget") and not state.get("token"):
                say("browser: captcha widget present, waiting for it to clear")
                deadline = time.time() + 45
                while time.time() < deadline:
                    page.wait_for_timeout(1500)
                    state = page.evaluate(TURNSTILE_STATE_JS)
                    if state.get("token"):
                        break
                if not state.get("token") and captcha_key:
                    site_key = page.evaluate(
                        "() => document.querySelector('[data-sitekey]')?.getAttribute('data-sitekey') || ''"
                    )
                    if site_key:
                        token = solve_turnstile(
                            api_key=captcha_key,
                            site_url="https://deepai.org/",
                            site_key=site_key,
                            proxy=proxy,
                        )
                        page.evaluate(INJECT_JS, {"token": token, "field": "cf-turnstile-response"})
                        say("browser: injected solved captcha token")

            submitted = page.evaluate(SUBMIT_JS)
            say(f"browser: submit via {submitted}")

            # Success condition is the session, not the dialog: a post-signup promo
            # dialog may stay open, so poll the profile endpoint instead.
            deadline = time.time() + min(timeout, 150)
            profile: dict[str, Any] | None = None
            last_status = 0
            while time.time() < deadline:
                page.wait_for_timeout(1200)
                error = _read_error(page)
                if error:
                    lowered = error.lower()
                    if "exist" in lowered:
                        raise EmailTaken(error)
                    if "email" in lowered:
                        raise EmailRejected(error)
                    raise SignupError(f"signup dialog refused: {error}")
                raw_probe = page.evaluate(PROFILE_JS)
                last_status = int(raw_probe.get("status") or 0)
                if last_status == 200:
                    try:
                        candidate = json.loads(raw_probe.get("body") or "{}")
                    except json.JSONDecodeError:
                        candidate = {}
                    if isinstance(candidate, dict) and candidate.get("pk"):
                        profile = candidate
                        break
            if profile is None:
                raise SignupError(
                    f"signup did not produce a session (last profile HTTP {last_status})"
                )

            page.wait_for_timeout(rng.randint(600, 1200))
            raw = page.evaluate(PROFILE_JS)
            status = int(raw.get("status") or 0)
            body = raw.get("body") or ""
            if status >= 400:
                raise SignupError(f"profile call failed in browser (HTTP {status})", detail=body[:200])
            try:
                profile = json.loads(body)
            except json.JSONDecodeError as exc:
                raise SignupError("profile call returned non-JSON") from exc

            api_key = ""
            for path in (
                ("userprofile", "api_key"),
                ("api_key",),
                ("userprofile2", "api_key"),
            ):
                node: Any = profile
                for part in path:
                    node = node.get(part) if isinstance(node, dict) else None
                if isinstance(node, str) and node.strip():
                    api_key = node.strip()
                    break
            if not api_key:
                raise NoApiKey("browser signup produced no api_key")

            cookies = {c.get("name"): c.get("value") for c in context.cookies()}
            return {
                "email": email,
                "password": password,
                "username": profile.get("username") or "",
                "api_key": api_key,
                "api_key_source": "browser-profile",
                "user_pk": profile.get("pk"),
                "plan": (profile.get("userprofile2") or {}).get("plan") or "",
                "is_pro": bool((profile.get("userprofile2") or {}).get("user_has_deepai_pro")),
                "sessionid": cookies.get("sessionid", ""),
                "csrftoken": cookies.get("csrftoken", ""),
                "registration_key": "",
                "profile": {
                    "pk": profile.get("pk"),
                    "username": profile.get("username"),
                    "email": profile.get("email"),
                    "userprofile": profile.get("userprofile"),
                    "userprofile2": profile.get("userprofile2"),
                },
            }
        except Exception as exc:
            if shot_dir:
                target = shot_dir / f"fail-{stamp}-{email.split('@')[0]}.png"
                try:
                    page.screenshot(path=str(target), full_page=True)
                    say(f"browser: screenshot {target}")
                except Exception:  # noqa: BLE001
                    pass
            if isinstance(exc, (EmailRejected, EmailTaken, NoApiKey, SignupError)):
                raise
            message = str(exc)
            if "cloudflare" in message.lower() or "challenge" in message.lower():
                raise Blocked(f"browser blocked: {message}") from exc
            raise SignupError(f"browser engine failed: {type(exc).__name__}: {message}") from exc
        finally:
            try:
                context.close()
            except Exception:  # noqa: BLE001
                pass
