"""指纹浏览器引擎（可选）。

和 protocol.py 走同一套业务步骤，但每一步都发生在真实浏览器里：

    1. 打开 https://www.zo.computer/signup（Cloudflare 自己过）
    2. 填邮箱 → 点 "Email me a sign-up link"
    3. runner 从 IMAP / mail.tm 取到魔法链，goto 那个 verify 页
    4. 页面里的 Turnstile 由真实浏览器自己解；点 "Continue" 完成 confirm
    5. 在 api.zo.computer 源上用页内 fetch 建工作区 / 建 token / 自检

为什么值得慢这一趟：
  * `POST /api/email-login/confirm` 上挂 Turnstile（sitekey 0x4AAAAAADTRT4Uvmz3G7YH5）。
    纯协议没有 token 时要么 403、要么拿到 `sms_onboarding` 分支；真实浏览器腿
    自己渲染就能过。
  * 指纹（UA / 时区 / 语言 / 视口 / WebGL）由 persona 决定，`geo_align` 时跟出口对齐。

支持的内核（`--browser-stack`）：
    camoufox   Firefox 的 C++ 层指纹改写，托管挑战通过率最高
    patchright Playwright 反检测分支（Chromium 指纹 + 真实 TLS）
    playwright 原版兜底
    cdp        接管已有商业指纹浏览器（AdsPower / BitBrowser / Morelogin）
"""

from __future__ import annotations

import json
import time
from typing import Any

from .config import API_BASE, DEFAULT_MODEL, PANEL_BASE
from .errors import BrowserUnavailable, NoApiKey, NoToken, UpstreamError
from .identity import Persona
from .protocol import Registration

SIGNUP_URL = f"{PANEL_BASE}/signup"
API = API_BASE


def detect_stack(preferred: str = "auto") -> str:
    """按可用性挑内核；`preferred` 指定时只用它（缺依赖就报错而不是悄悄降级）。"""
    order = [preferred] if preferred and preferred != "auto" else ["camoufox", "patchright", "playwright"]
    for name in order:
        if name == "camoufox":
            try:
                import camoufox  # noqa: F401

                return "camoufox"
            except Exception:  # noqa: BLE001
                continue
        if name == "patchright":
            try:
                import patchright  # noqa: F401

                return "patchright"
            except Exception:  # noqa: BLE001
                continue
        if name == "playwright":
            try:
                import playwright  # noqa: F401

                return "playwright"
            except Exception:  # noqa: BLE001
                continue
    raise BrowserUnavailable(
        "没有可用的浏览器内核：pip install camoufox[geoip] / patchright / playwright，并 playwright install chromium。"
    )


def _launch(stack: str, persona: Persona, *, headful: bool, proxy: str = "", cdp: str = ""):
    """返回 (context, closer)。"""
    context_args: dict[str, Any] = {
        "user_agent": persona.user_agent,
        "locale": persona.locale,
        "timezone_id": persona.timezone,
        "viewport": {"width": persona.viewport[0], "height": persona.viewport[1]},
        "device_scale_factor": persona.device_scale,
        "screen": {"width": persona.screen[0], "height": persona.screen[1]},
        "color_scheme": "light",
        "extra_http_headers": {"Accept-Language": persona.accept_language},
    }
    proxy_cfg = {"server": proxy} if proxy else None
    if proxy_cfg:
        context_args["proxy"] = proxy_cfg

    if stack == "camoufox":
        from camoufox.sync_api import Camoufox

        cm = Camoufox(
            headless=not headful,
            humanize=True,
            geoip=persona.timezone is not None,
            locale=[persona.locale],
            proxy=proxy_cfg,
            os=["windows"],
        )
        browser = cm.__enter__()

        def closer():
            try:
                cm.__exit__(None, None, None)
            except Exception:  # noqa: BLE001
                pass

        context = browser.new_context(**{k: v for k, v in context_args.items() if k not in {"proxy"}})
        return context, closer

    if stack == "patchright":
        from patchright.sync_api import sync_playwright
    else:
        from playwright.sync_api import sync_playwright

    pw = sync_playwright().start()
    if cdp:
        browser = pw.chromium.connect_over_cdp(cdp)
        context = browser.contexts[0] if browser.contexts else browser.new_context(**context_args)
        return context, lambda: (browser.close(), pw.stop())

    launch_args: dict[str, Any] = {"headless": not headful}
    if proxy_cfg:
        launch_args["proxy"] = proxy_cfg
    browser = pw.chromium.launch(**launch_args)
    context = browser.new_context(**context_args)

    def closer():
        for fn in (context.close, browser.close, pw.stop):
            try:
                fn()
            except Exception:  # noqa: BLE001
                pass

    return context, closer


def _click_first(page, selectors: tuple[str, ...], *, timeout: int = 8_000) -> bool:
    """按顺序试选择器，点中第一个存在的（Zo 的按钮文案会变，id 更稳）。"""
    for selector in selectors:
        try:
            page.locator(selector).first.click(timeout=timeout)
            return True
        except Exception:  # noqa: BLE001 - 换下一个选择器
            continue
    return False


def run_browser_registration(
    *,
    config,
    persona: Persona,
    email: str,
    verify_link: str = "",
    proxy: str = "",
    log=None,
    keys_per_account: int = 1,
    token_name: str = "gateway",
) -> Registration:
    log = log or (lambda *a, **k: None)
    stack = detect_stack(config.browser_stack)
    log(f"browser stack = {stack}")

    context, closer = _launch(
        stack,
        persona,
        headful=config.headful,
        proxy=proxy,
        cdp=config.cdp,
    )
    page = context.new_page()
    page.set_default_timeout(45_000)
    try:
        # ---- 1. 打开注册页 ----
        page.goto(SIGNUP_URL, wait_until="domcontentloaded")
        log(f"signup page → {page.url}")

        # ---- 2. 填邮箱（Google 按钮与邮箱按钮同屏；先点 "Email me a sign-up link"）----
        _click_first(
            page,
            (
                'button:has-text("Email me a sign-up link")',
                'button:has-text("Email me a login link")',
                'button:has-text("Email")',
            ),
        )
        filled = False
        for selector in ('input[type="email"]', 'input[name="email"]', 'input[placeholder*="mail" i]'):
            try:
                page.fill(selector, email, timeout=5_000)
                filled = True
                break
            except Exception:  # noqa: BLE001
                continue
        if not filled:
            raise UpstreamError("注册页上找不到邮箱输入框（页面结构变了？）", status=0)

        _click_first(page, ('button[type="submit"]', 'button:has-text("Continue")', 'button:has-text("Send")'))
        page.wait_for_timeout(1_500)
        log(f"magic link requested → {page.url}")

        # ---- 3. 魔法链：整页跳到 verify，页内自己过 Turnstile ----
        if not verify_link:
            raise NoToken("浏览器腿没有拿到魔法链（runner 的邮箱还没收到信）。")
        page.goto(verify_link, wait_until="domcontentloaded")
        log(f"verify page → {page.url}")

        # Turnstile 是 iframe；等它自己完成（最多 30s），然后点继续
        for _ in range(30):
            page.wait_for_timeout(1_000)
            done = page.evaluate(
                """() => {
                    const el = document.querySelector('input[name="cf-turnstile-response"]');
                    return !!(el && el.value && el.value.length > 10);
                }"""
            )
            if done:
                break
        _click_first(
            page,
            (
                'button:has-text("Continue")',
                'button:has-text("Sign in")',
                'button:has-text("Verify")',
                'button[type="submit"]',
            ),
            timeout=10_000,
        )
        page.wait_for_timeout(2_000)
        log(f"confirm → {page.url}")

        # ---- 4. 通过页内 fetch 走 API（cookie 由浏览器自动带）----
        result = page.evaluate(
            """async ({ api, handle, keyName, keysPerAccount, model }) => {
                const j = async (path, init) => {
                    const res = await fetch(api + path, { credentials: 'include', ...(init || {}) });
                    const text = await res.text();
                    let body = null;
                    try { body = JSON.parse(text); } catch (e) {}
                    return { status: res.status, body, text };
                };
                const state = await j('/../api/placeholder');
                return { ok: false, reason: 'unused' };
            }""",
            {"api": API, "handle": persona.handle, "keyName": token_name, "keysPerAccount": keys_per_account, "model": config.model},
        )
        log(f"browser evaluate → {json.dumps(result)[:120]}")

        # 真正的 API 步骤在下面用 page.request 做（带 context 的 cookie jar）
        req = context.request
        state_res = req.get(f"{PANEL_BASE}/api/login-state")
        if state_res.status >= 400:
            raise NoToken(f"login-state 失败（HTTP {state_res.status}）")
        state = state_res.json() or {}
        workspaces = state.get("workspaces") or []
        claims = state.get("claims") or {}
        registration = Registration(
            email=email,
            handle=persona.handle,
            name=persona.username,
            domain=email.split("@", 1)[-1] if "@" in email else "",
            proxy=proxy,
            note=persona.note,
            engine=f"browser:{stack}",
        )
        registration.claims = {k: claims.get(k) for k in ("email", "sub", "name") if claims.get(k)}
        registration.emailVerified = bool(claims.get("email_verified", True))
        if workspaces:
            first = workspaces[0] if isinstance(workspaces[0], dict) else {}
            registration.workspaceId = str(first.get("id") or first.get("slug") or "")
            registration.workspaceUrl = str(first.get("url") or "")
            handle_from_state = str(first.get("handle") or first.get("slug") or "")
            if handle_from_state:
                registration.handle = handle_from_state

        if not workspaces:
            # 探占用 + 建工作区
            chosen = registration.handle
            for index in range(6):
                probe = req.get(f"{API}/signup/{chosen}/available")
                body = probe.json() if probe.status < 400 else {}
                if body.get("available"):
                    break
                chosen = f"{registration.handle[:22]}{index + 2}"
            created = req.post(
                f"{API}/signup/",
                headers={"Content-Type": "application/json", "Origin": PANEL_BASE},
                data=json.dumps({"handle": chosen}),
            )
            if created.status >= 400:
                raise UpstreamError(f"建工作区失败（HTTP {created.status}）：{created.text()[:200]}", status=created.status)
            registration.handle = chosen
            created_body = created.json() if created.status < 400 else {}
            registration.workspaceId = str(created_body.get("id") or "")
            registration.workspaceUrl = str(created_body.get("url") or chosen)

        # 建 token
        keys: list[dict] = []
        for index in range(max(1, keys_per_account)):
            name = token_name if index == 0 else f"{token_name}-{index + 1}"
            res = req.post(
                f"{API}/api-keys/",
                headers={"Content-Type": "application/json", "Origin": PANEL_BASE},
                data=json.dumps({"name": name}),
            )
            if res.status >= 400:
                raise NoApiKey(f"建 token 失败（HTTP {res.status}）：{res.text()[:200]}")
            body = res.json() or {}
            token = body.get("key") or body.get("token") or body.get("api_key") or ""
            if not token:
                raise NoApiKey(f"api-keys 没返回 token：{json.dumps(body)[:200]}")
            keys.append({"key": token, "id": str(body.get("id") or ""), "name": name})

        registration.apiKey = keys[0]["key"]
        registration.keyId = keys[0]["id"]
        registration.keyName = keys[0]["name"]
        registration.extraKeys = [{"key": row["key"], "id": row["id"], "source": "created"} for row in keys[1:]]

        # 会话 cookie（context 的 cookie jar 里有 access_token / refresh_token）
        cookies = context.cookies()
        registration.cookies = [
            {"name": c.get("name", ""), "value": c.get("value", ""), "domain": c.get("domain", ""), "path": c.get("path", "/")}
            for c in cookies
        ]
        jar = {c["name"]: c["value"] for c in registration.cookies}
        registration.accessToken = jar.get("access_token") or registration.apiKey
        registration.refreshToken = jar.get("refresh_token") or ""
        registration.accessTokenExpiresAt = int((time.time() + 86400) * 1000)
        registration.status = "active"

        if config.probe:
            try:
                models = req.get(
                    f"{API}/models/available",
                    headers={"Authorization": f"Bearer {registration.accessToken}"},
                )
                if models.status < 400:
                    registration.models = [
                        str(row.get("model_name"))
                        for row in ((models.json() or {}).get("models") or [])
                        if row.get("model_name")
                    ][:40]
                ask = req.post(
                    f"{API}/zo/ask",
                    headers={
                        "Authorization": f"Bearer {registration.accessToken}",
                        "Content-Type": "application/json",
                    },
                    data=json.dumps({"input": "ping", "model_name": config.model or DEFAULT_MODEL, "stream": False}),
                )
                registration.probe = {"ok": ask.status == 200, "status": ask.status, "body": ask.text()[:200]}
            except Exception as cause:  # noqa: BLE001
                registration.probe = {"ok": False, "reason": str(cause)[:200]}

        return registration
    finally:
        try:
            closer()
        except Exception:  # noqa: BLE001
            pass