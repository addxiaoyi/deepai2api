"""指纹浏览器引擎。

和 protocol.py 走同一套业务步骤，但每一步都发生在真实浏览器里：

    signup 表单 → 验证邮件链接 → OAuth 跳转 → 在 dashboard 源上 fetch 换令牌
    → fetch 建组织 / 建 api_ Key → fetch 打一次 llm-proxy 自检

为什么值得慢这一趟：
  * moonai 的 OAuth 是 authentik + Django session，纯 HTTP 容易在 cookie 属性、
    `Sec-Fetch-*`、同源策略上踩坑；浏览器腿天然全对。
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

from .config import DEFAULT_MODEL
from .errors import MoonAIError, NoApiKey, NoToken, UpstreamError
from .identity import Persona
from .protocol import Registration

SIGNUP_URL = "https://auth.mooonai.com/auth/signup/"
SIGNIN_URL = "https://auth.mooonai.com/auth/signin/"
CORE = "https://core.mooonai.com"
CONSOLE = "https://dashboard.mooonai.com"


class BrowserUnavailable(MoonAIError):
    kind = "browser_unavailable"
    retryable = False


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

        # Camoufox 自己管指纹；传 humanize 让点击/输入带人类节奏
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
    if proxy:
        launch_args["proxy"] = proxy_cfg
    browser = pw.chromium.launch(**launch_args)
    context = browser.new_context(**context_args)

    def closer():
        try:
            context.close()
        except Exception:  # noqa: BLE001
            pass
        try:
            browser.close()
        except Exception:  # noqa: BLE001
            pass
        try:
            pw.stop()
        except Exception:  # noqa: BLE001
            pass

    return context, closer


def run_browser_registration(
    *,
    config,
    persona: Persona,
    email: str,
    password: str,
    username: str,
    verify_link: str = "",
    proxy: str = "",
    log=None,
    keys_per_account: int = 1,
    key_name: str = "openai-sdk",
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
        # ---- 1. 注册表单
        page.goto(SIGNUP_URL, wait_until="domcontentloaded")
        page.click("#show-email-form")
        page.fill('input[name="username"]', username)
        page.fill('input[name="email"]', email)
        page.fill('input[name="password1"]', password)
        page.click('button[type="submit"]')
        page.wait_for_load_state("domcontentloaded")
        log(f"signup → {page.url}")

        # ---- 2. 验证邮件（链接由 runner 的 mailbox 提供）
        if verify_link:
            page.goto(verify_link, wait_until="domcontentloaded")
            log(f"verify → {page.url}")

        # ---- 3. 登录（验证后通常已带 session；没有就补一次）
        if "/auth/signin" in page.url:
            page.fill('input[name="username"]', email)
            page.fill('input[name="password"]', password)
            page.click('button[type="submit"]')
            page.wait_for_load_state("domcontentloaded")

        # ---- 4. OAuth：在 dashboard 源上请求 auth/url，然后整页跳过去
        page.goto(f"{CONSOLE}/", wait_until="domcontentloaded")
        auth_url = page.evaluate(
            """async () => {
                const res = await fetch('https://core.mooonai.com/api/v1/auth/url', { credentials: 'omit' });
                if (!res.ok) return '';
                const data = await res.json();
                return data.auth_url || '';
            }"""
        )
        if not auth_url:
            raise UpstreamError("浏览器里拿不到 auth_url。")
        page.goto(auth_url, wait_until="domcontentloaded")
        deadline = time.time() + 60
        while time.time() < deadline and "code=" not in page.url:
            page.wait_for_timeout(500)
        if "code=" not in page.url:
            raise NoToken(f"OAuth 没回到带 code 的回调：{page.url}")

        # ---- 5. 换令牌 + 建组织 + 建 Key + 自检，全部在 dashboard 源上 fetch
        result = page.evaluate(
            """async ({ callback, core, keyName, keysPerAccount, model }) => {
                const post = async (path, body) => {
                    const res = await fetch(core + path, { method: 'POST', body });
                    const text = await res.text();
                    return { status: res.status, text };
                };
                const tokenRes = await post(
                    '/api/v1/auth/token',
                    new URLSearchParams({ auth_response_url: callback }).toString(),
                );
                let token = {};
                try { token = JSON.parse(tokenRes.text); } catch (e) {}
                if (!token.access_token) return { error: 'no_token', detail: tokenRes.text.slice(0, 200) };
                const hr = { Authorization: 'Bearer ' + token.access_token, 'Content-Type': 'application/json' };
                const jget = async (path) => {
                    const res = await fetch(core + path, { headers: hr });
                    return { status: res.status, text: await res.text() };
                };
                const jpost = async (path, payload) => {
                    const res = await fetch(core + path, { method: 'POST', headers: hr, body: JSON.stringify(payload) });
                    return { status: res.status, text: await res.text() };
                };
                const orgs = JSON.parse((await jget('/api/v1/organizations/')).text || '{}');
                let org = (orgs.results || [])[0];
                if (!org) {
                    const created = JSON.parse((await jpost('/api/v1/organizations', { name: 'Moon Web' })).text || '{}');
                    org = created;
                }
                if (!org || !org.id) return { error: 'no_org' };
                const keys = [];
                for (let i = 0; i < Math.max(1, keysPerAccount); i++) {
                    const res = await jpost('/api/v1/organizations/' + org.id + '/api-keys', { name: i ? keyName + '-' + (i + 1) : keyName });
                    let parsed = {};
                    try { parsed = JSON.parse(res.text); } catch (e) {}
                    if (parsed.key) keys.push(parsed);
                }
                const probe = await jpost('/api/v1/services/llm-proxy/chat/completions', {
                    model, messages: [{ role: 'user', content: 'ping' }], max_tokens: 8,
                });
                return {
                    access_token: token.access_token,
                    refresh_token: token.refresh_token || '',
                    expires_in: token.expires_in || 86400,
                    org: { id: String(org.id), name: org.name || '' },
                    keys,
                    probe: { status: probe.status, body: probe.text.slice(0, 220) },
                };
            }""",
            {
                callback: page.url,
                core: CORE,
                keyName: key_name,
                keysPerAccount: max(1, keys_per_account),
                model: config.model or DEFAULT_MODEL,
            },
        )

        if result.get("error"):
            if result["error"] == "no_token":
                raise NoToken(f"浏览器换令牌失败：{result.get('detail', '')}")
            raise UpstreamError(f"浏览器链路失败：{result['error']}")

        keys = result.get("keys") or []
        if not keys:
            raise NoApiKey("浏览器腿没签出任何 api_ Key。")

        registration = Registration(
            email=email,
            password=password,
            username=username,
            name=f"{persona.first_name} {persona.last_name}",
            apiKey=str(keys[0].get("key") or ""),
            keyId=str(keys[0].get("id") or ""),
            organizationId=str((result.get("org") or {}).get("id") or ""),
            organizationName=str((result.get("org") or {}).get("name") or ""),
            accessToken=str(result.get("access_token") or ""),
            refreshToken=str(result.get("refresh_token") or ""),
            accessTokenExpiresAt=int((time.time() + int(result.get("expires_in") or 86400)) * 1000),
            emailVerified=True,
            engine=f"browser:{stack}",
            domain=email.split("@", 1)[-1] if "@" in email else "",
            proxy=proxy,
            note=persona.note,
        )
        registration.extraKeys = [
            {"key": row.get("key"), "id": row.get("id"), "source": "created"} for row in keys[1:]
        ]
        registration.probe = {
            "ok": result.get("probe", {}).get("status") == 200,
            "status": result.get("probe", {}).get("status", 0),
            "body": result.get("probe", {}).get("body", ""),
        }
        try:
            cookies = context.cookies()
        except Exception:  # noqa: BLE001
            cookies = []
        registration.cookies = [
            {"name": c.get("name", ""), "value": c.get("value", ""), "domain": c.get("domain", "")}
            for c in cookies
        ]
        return registration
    finally:
        try:
            page.close()
        except Exception:  # noqa: BLE001
            pass
        closer()


def _dump(value: Any) -> str:  # pragma: no cover - 调试用
    try:
        return json.dumps(value, ensure_ascii=False)[:400]
    except Exception:  # noqa: BLE001
        return str(value)[:400]
