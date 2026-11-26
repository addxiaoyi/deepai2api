"""验证码取值：腾讯云验证码（ticket + randstr）/ Turnstile。

两种来源：

``manual``   调用方直接给 `--ticket` / `--randstr`（或 `--turnstile`），最可控。
``browser``  Camoufox 打开注册页、填邮箱、点"发送验证码"，**拦截发码请求**把
             `ticket` / `randstr` 从 query 里抓出来 —— 相当于让真人浏览器替我们把
             验证码做完，然后把票交回协议链路。
"""

from __future__ import annotations

import time
from typing import Any
from urllib.parse import parse_qs, urlparse

from .errors import CaptchaRequired, LineError
from .site import Site


def manual(values: dict[str, str] | None = None) -> dict[str, str]:
    return {k: str(v).strip() for k, v in (values or {}).items() if str(v or "").strip()}


def from_browser(
    site: Site,
    *,
    email: str,
    proxy: str = "",
    headless: bool = True,
    timeout: float = 120.0,
) -> dict[str, str]:
    """打开登录页，点发码，从网络请求里取 ticket/randstr。"""
    try:
        from camoufox.sync_api import Camoufox  # type: ignore import-not-found
    except ImportError as exc:  # pragma: no cover - 取决于环境
        raise CaptchaRequired(
            "camoufox 没装（pip install 'camoufox[geoip]'），或改用 --captcha manual --ticket/--randstr"
        ) from exc

    login_url = site.login_url or site.base_url
    marker = (site.verify_code_path or "/api/verification").split("?")[0]
    found: dict[str, str] = {}
    kwargs: dict[str, Any] = {"headless": headless, "humanize": True, "os": "windows"}
    if proxy:
        kwargs["proxy"] = {"server": proxy}

    with Camoufox(**kwargs) as browser:
        page = browser.new_page()

        def on_request(request: Any) -> None:
            url = request.url
            if marker not in url:
                return
            query = parse_qs(urlparse(url).query)
            for name in site.captcha_params or ("ticket", "randstr"):
                value = (query.get(name) or [""])[0]
                if value:
                    found[name] = value

        page.on("request", on_request)
        page.goto(login_url, wait_until="domcontentloaded", timeout=timeout * 1000)
        page.wait_for_timeout(2500)

        # 邮箱输入框：不同站 placeholder 不同，挨个试
        for selector in (
            "input[type='email']",
            "input[name='email']",
            "input[placeholder*='mail' i]",
            "input[placeholder*='邮箱']",
        ):
            try:
                box = page.locator(selector).first
                if box.count():
                    box.fill(email, timeout=5000)
                    break
            except Exception:  # noqa: BLE001
                continue

        deadline = time.time() + timeout
        clicked = False
        while time.time() < deadline:
            if not clicked:
                for text in ("发送验证码", "Send code", "获取验证码", "Send verification", "Continue", "Next"):
                    try:
                        page.get_by_text(text, exact=False).first.click(timeout=2500)
                        clicked = True
                        break
                    except Exception:  # noqa: BLE001
                        continue
            if found:
                break
            page.wait_for_timeout(1000)

    if not found:
        raise LineError("浏览器没抓到验证码票据（页面结构可能变了）")
    return found


def resolve(
    site: Site,
    *,
    provider: str = "auto",
    values: dict[str, str] | None = None,
    email: str = "",
    proxy: str = "",
    headless: bool = True,
) -> dict[str, str]:
    """按 provider 取验证码参数；`auto` = 有手填用手填，否则开浏览器。"""
    # 这条线没有验证码门槛：直接返回空，别去开浏览器
    if not site.captcha_params:
        return {}
    mode = (provider or "auto").strip().lower()
    given = manual(values)
    if mode in ("none", ""):
        return {}
    if mode == "manual":
        missing = [name for name in site.captcha_params if not given.get(name)]
        if missing:
            raise CaptchaRequired(f"缺少验证码参数：{', '.join(missing)}（--ticket/--randstr）")
        return given
    if given:
        return given
    if mode in ("auto", "browser"):
        return from_browser(site, email=email, proxy=proxy, headless=headless)
    raise CaptchaRequired(f"未知的 captcha provider：{provider}")
