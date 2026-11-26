"""浏览器引擎：在 `/v2/sign-up` 上把表单填完并提交。

为什么还要浏览器腿：Turnstile 是 managed 模式，很多出口下只有真实浏览器点提交
才会拿到 token。协议腿带着 token 再跑，能省掉每号一次页面加载。

提交成功后：如果页面跳到 `/v2/overview`，就把 cookie/令牌读出来返回；否则至少把
"账号已建"这件事确认下来，交给协议腿去登录 + 领试用。
"""

from __future__ import annotations

import json
import time
from typing import Any

from .errors import Blocked, ProxyScrapeError, TurnstileRequired

SIGNUP_URL = "https://dashboard.proxyscrape.com/v2/sign-up"
OVERVIEW_MARKERS = ("/v2/overview", "/v2/services", "/v2/onboarding")


class BrowserUnavailable(ProxyScrapeError):
    kind = "browser_unavailable"


def available_stacks() -> list[str]:
    out: list[str] = []
    for name, module in (("camoufox", "camoufox"), ("patchright", "patchright"), ("playwright", "playwright")):
        try:
            __import__(module)
            out.append(name)
        except ImportError:
            continue
    return out


def _launch(stack: str, *, headless: bool, proxy: str, cdp: str, humanize: float, geoip: bool) -> Any:
    stacks = available_stacks()
    if not stacks:
        raise BrowserUnavailable("没有浏览器内核：pip install 'camoufox[geoip]' 或 playwright")
    chosen = stacks[0] if stack in ("auto", "") else stack
    if chosen not in stacks:
        raise BrowserUnavailable(f"{stack} 不可用，现有：{', '.join(stacks)}")
    if chosen == "camoufox":
        from camoufox.sync_api import Camoufox  # type: ignore import-not-found

        kwargs: dict[str, Any] = {"headless": headless, "humanize": bool(humanize), "os": "windows", "geoip": geoip}
        if proxy:
            kwargs["proxy"] = {"server": proxy}
        return Camoufox(**kwargs)
    from playwright.sync_api import sync_playwright  # type: ignore import-not-found

    runner = sync_playwright().start()
    launch: dict[str, Any] = {"headless": headless, "args": ["--disable-blink-features=AutomationControlled"]}
    if proxy:
        launch["proxy"] = {"server": proxy}
    browser = runner.chromium.launch(**launch)
    browser._ps_runner = runner  # 让 close 能停掉 playwright
    return browser


def _close(browser: Any) -> None:
    runner = getattr(browser, "_ps_runner", None)
    for method in ("__exit__", "close"):
        fn = getattr(browser, method, None)
        if fn is None:
            continue
        try:
            fn(None, None, None) if method == "__exit__" else fn()
            break
        except Exception:  # noqa: BLE001
            continue
    if runner is not None:
        try:
            runner.stop()
        except Exception:  # noqa: BLE001
            pass


def run_browser_registration(
    *,
    email: str,
    password: str,
    stack: str = "auto",
    headless: bool = True,
    proxy: str = "",
    humanize: float = 1.0,
    geoip: bool = True,
    cdp: str = "",
    timeout: float = 120.0,
    log: Any = None,
) -> dict[str, Any]:
    say = log or (lambda *_: None)
    browser = _launch(stack, headless=headless, proxy=proxy, cdp=cdp, humanize=humanize, geoip=geoip)
    try:
        if hasattr(browser, "new_page"):
            page = browser.new_page()
        else:
            page = browser.contexts[0].new_page()
        page.goto(SIGNUP_URL, wait_until="domcontentloaded", timeout=timeout * 1000)
        page.wait_for_timeout(3000)

        # 邮箱 / 密码 / 确认密码
        page.locator("input[type='email']").first.fill(email, timeout=15000)
        passwords = page.locator("input[type='password']")
        passwords.nth(0).fill(password, timeout=15000)
        if passwords.count() > 1:
            passwords.nth(1).fill(password, timeout=15000)
        # 条款勾选（有的版本是 checkbox 在密码下面）
        try:
            box = page.locator("input[type='checkbox']").first
            if box.count() and not box.is_checked():
                box.check(timeout=5000)
        except Exception:  # noqa: BLE001
            pass

        page.wait_for_timeout(1500)
        say("浏览器提交注册表单")
        submitted = False
        for selector in ("button[type='submit']", "button:has-text('Sign up')", "button:has-text('Create')"):
            try:
                page.locator(selector).first.click(timeout=8000)
                submitted = True
                break
            except Exception:  # noqa: BLE001
                continue
        if not submitted:
            page.keyboard.press("Enter")

        deadline = time.time() + timeout
        redirected = False
        while time.time() < deadline:
            page.wait_for_timeout(2000)
            url = page.url
            if any(marker in url for marker in OVERVIEW_MARKERS):
                redirected = True
                break
            body = page.evaluate("() => document.body.innerText.slice(0, 2000)").lower()
            if "turnstile" in body and ("fail" in body or "verify" in body):
                raise TurnstileRequired("页面提示 Turnstile 校验失败")
            if "already" in body and "account" in body:
                raise ProxyScrapeError("地址已注册", detail=body[:200])
            if "just a moment" in body:
                raise Blocked("Cloudflare 拦截页")

        data = page.evaluate(
            """() => {
                const out = {};
                try {
                    for (let i = 0; i < localStorage.length; i++) {
                        const k = localStorage.key(i);
                        if (/token/i.test(k)) out[k] = localStorage.getItem(k);
                    }
                } catch (e) {}
                return out;
            }"""
        )
        token = ""
        for key, value in (data or {}).items():
            if isinstance(value, str) and value.count(".") >= 1 and len(value) > 40:
                token = value
                break
        say(f"浏览器注册{'成功' if redirected else '已提交'}")
        return {"email": email, "access_token": token, "redirected": redirected, "url": page.url}
    finally:
        _close(browser)
