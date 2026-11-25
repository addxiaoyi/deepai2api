"""Turnstile token minting for gab.ai signup.

gab.ai signup endpoint accepts `turnstileToken` in JSON body.
The public signup page uses Cloudflare Turnstile with sitekey:
  0x4AAAAAAAQeXKSGTzxYAKT8

We support:
  - none   : send empty token
  - manual : provided token string
  - browser: render turnstile on real origin and read token

(Unlike allmodels-reg, we don't wire capsolver/2captcha here by default; if you
want it, we can copy those implementations verbatim.)
"""

from __future__ import annotations

from typing import Any

from .errors import CaptchaRequired


class CaptchaError(CaptchaRequired):
    pass


def solve_turnstile(*, provider: str, api_key: str, site_url: str, site_key: str, action: str = "signup", proxy: str = "", timeout: float = 120.0) -> str:
    name = (provider or "none").strip().lower()
    if name in ("", "none"):
        return ""
    if name == "manual":
        if not api_key:
            raise CaptchaError("manual captcha provider needs a token in `captcha.api_key`")
        return api_key
    if name == "browser":
        return mint_with_browser(site_url=site_url, site_key=site_key, action=action, proxy=proxy, timeout=timeout)
    raise CaptchaError(f"unknown captcha provider: {provider}")


_MINT_JS = """
async (args) => {
  const { siteKey, action } = args;
  if (!window.turnstile) {
    await new Promise((resolve, reject) => {
      const s = document.createElement('script');
      s.src = 'https://challenges.cloudflare.com/turnstile/v0/api.js?render=explicit';
      s.async = true;
      s.onload = resolve;
      s.onerror = () => reject(new Error('turnstile script failed'));
      document.head.appendChild(s);
    });
  }
  const host = document.createElement('div');
  host.id = '__gab_captcha';
  host.style.position = 'fixed';
  host.style.inset = '0';
  host.style.zIndex = '2147483647';
  document.body.appendChild(host);
  return await new Promise((resolve, reject) => {
    const timer = setTimeout(() => reject(new Error('turnstile mint timed out')), 120000);
    try {
      window.turnstile.render(host, {
        sitekey: siteKey,
        action,
        theme: 'light',
        callback: (token) => { clearTimeout(timer); resolve(token); },
        'error-callback': () => { clearTimeout(timer); reject(new Error('turnstile error callback')); },
      });
    } catch (error) {
      clearTimeout(timer);
      reject(error);
    }
  });
}
"""


def mint_with_browser(*, site_url: str, site_key: str, action: str = "signup", proxy: str = "", timeout: float = 180.0, headless: bool = True) -> str:
    try:
        from playwright.sync_api import sync_playwright
    except Exception as exc:  # noqa: BLE001
        raise CaptchaError("browser captcha provider needs playwright") from exc

    launch: dict[str, Any] = {"headless": headless}
    if proxy:
        launch["proxy"] = {"server": proxy}

    with sync_playwright() as pw:
        browser = pw.chromium.launch(**launch)
        try:
            context = browser.new_context(locale="en-US")
            page = context.new_page()
            page.goto(site_url, wait_until="domcontentloaded", timeout=int(timeout * 1000))
            token = page.evaluate(_MINT_JS, {"siteKey": site_key, "action": action})
            if not token:
                raise CaptchaError("browser produced an empty turnstile token")
            return str(token)
        finally:
            browser.close()
