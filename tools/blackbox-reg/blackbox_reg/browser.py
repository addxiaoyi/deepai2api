"""指纹浏览器腿。

为什么 Blackbox 必须要这一条腿：Google 登录要过的是 Google 自己的风控，
纯 HTTP 依赖"已经登录的 Google 会话"；真浏览器则是正常人类路径
（输入邮箱 → 密码 → 可选 TOTP → 同意授权），成功率最高。

支持三种内核（装哪个用哪个）：

    camoufox   Firefox + C++ 层指纹改写（**首选**：Google / Cloudflare 都认它）
    patchright Playwright 反检测分支（Chromium）
    playwright 官方 Chromium 兜底
    外部 CDP    AdsPower / BitBrowser / Morelogin —— `--cdp http://127.0.0.1:50325`

## Camoufox 的用法要点（https://camoufox.com/python/usage/）

    from camoufox.sync_api import Camoufox
    with Camoufox(**kwargs) as browser:      # 默认返回 Browser
        page = browser.new_page()

    persistent_context=True + user_data_dir → 返回 BrowserContext（身份档复用）

几个**不能搞错**的点：

1. Camoufox 是 **Firefox**。绝不能把我们的 Chrome UA 注进去（一秒就被认出来），
   所以 camoufox 分支不传 `user_agent` / `sec-ch-ua`，让它自己按 fingerprint 生成。
2. 有代理时应传 `geoip=True`（或直接给 IP）：时区/语言/经纬度按出口 IP 对齐，
   这比我们自己拍一个 `Asia/Tokyo` 要可靠得多。
3. `disable_coop=True` 才能点击 cross-origin iframe 里的 Turnstile 复选框
   （console 那条 OTP 腿用得上），默认开着。
4. 不要固定 `window=(w,h)`：文档明确说固定窗口尺寸本身就是指纹特征。
5. 首次使用要先 `camoufox fetch` 把浏览器本体拉下来（pip 包不含内核）。
"""

from __future__ import annotations

import json
import os
import random
import re
import time
from typing import Any, Callable

from .errors import BlackboxError, NoApiKey, OAuthDenied
from .identity import GoogleIdentity, Persona, totp_now
from .protocol import KEY_RE, Registration

BROWSER_UNAVAILABLE_HINT = "pip install 'camoufox[geoip]' && camoufox fetch（或 playwright install chromium）"

# camoufox 的 `os` 参数只接受这三个值
CAMOUFOX_OS = {"Win32": "windows", "MacIntel": "macos", "Linux x86_64": "linux"}


class BrowserUnavailable(BlackboxError):
    kind = "browser_unavailable"


def _try_import(name: str):
    try:
        return __import__(name, fromlist=["*"])
    except Exception:  # noqa: BLE001
        return None


def available_stacks() -> list[str]:
    stacks = []
    if _try_import("camoufox"):
        stacks.append("camoufox")
    if _try_import("patchright"):
        stacks.append("patchright")
    if _try_import("playwright"):
        stacks.append("playwright")
    return stacks


def detect_stack(preferred: str = "auto") -> str:
    stacks = available_stacks()
    if preferred and preferred != "auto":
        if preferred not in stacks:
            raise BrowserUnavailable(f"没有安装 {preferred}（{BROWSER_UNAVAILABLE_HINT}）")
        return preferred
    if not stacks:
        raise BrowserUnavailable(f"没有任何浏览器内核（{BROWSER_UNAVAILABLE_HINT}）")
    return stacks[0]


def camoufox_installed_version() -> str:
    """camoufox 内核（浏览器本体）的版本；没 fetch 过就是空串。"""
    pkgman = _try_import("camoufox.pkgman")
    if not pkgman:
        return ""
    try:
        return str(pkgman.installed_verstr() or "")
    except Exception:  # noqa: BLE001 - 老版本没有这个函数
        return ""


def camoufox_launch_kwargs(
    *,
    headless: bool = True,
    humanize: float = 1.0,
    proxy: str = "",
    geoip: bool = True,
    locale: str = "",
    os_name: str = "windows",
    disable_coop: bool = True,
    profile_dir: str = "",
    screen: tuple[int, int] | None = None,
) -> dict[str, Any]:
    """把本项目的配置翻成 Camoufox 的启动参数（纯函数，便于离线测试）。

    只放"不会互相打架"的项：
      * `geoip` 开时不再传 `locale`（地理信息会覆盖它，两个都给反而可能不一致）；
      * 不传 `user_agent`（Camoufox 是 Firefox，见模块 docstring 第 1 条）；
      * 不传 `window`（固定窗口尺寸本身是指纹特征）。
    """
    kwargs: dict[str, Any] = {
        "headless": bool(headless),
        # humanize=True 用默认游标轨迹；>1 时当作"最长移动秒数"传进去
        "humanize": (min(3.0, float(humanize)) if humanize and humanize > 1.0 else True),
        "disable_coop": bool(disable_coop),
        "os": os_name or "windows",
    }
    if proxy:
        kwargs["proxy"] = {"server": proxy}
    if geoip:
        kwargs["geoip"] = True
    elif locale:
        kwargs["locale"] = locale
    if profile_dir:
        kwargs["persistent_context"] = True
        kwargs["user_data_dir"] = profile_dir
    if screen:
        # 仅作为约束（Camoufox 会在范围内挑），不是固定值
        try:
            from browserforge.fingerprints import Screen  # type: ignore

            kwargs["screen"] = Screen(max_width=int(screen[0]), max_height=int(screen[1]))
        except Exception:  # noqa: BLE001 - 没装 browserforge 就不加约束
            pass
    return kwargs


def chromium_context_kwargs(persona: Persona, *, proxy: str = "") -> dict[str, Any]:
    """Playwright Chromium 的新上下文参数（UA / 时区 / 视口一套自洽）。"""
    fp = persona.fingerprint()
    kwargs: dict[str, Any] = {
        "user_agent": persona.user_agent,
        "locale": persona.locale,
        "timezone_id": persona.timezone,
        "viewport": fp["viewport"],
        "screen": fp["screen"],
        "device_scale_factor": fp["deviceScaleFactor"],
        "extra_http_headers": {
            "Accept-Language": persona.accept_language,
            "Sec-CH-UA": persona.sec_ch_ua,
            "Sec-CH-UA-Mobile": "?0",
            "Sec-CH-UA-Platform": f'"{persona.platform}"',
        },
    }
    if proxy:
        kwargs["proxy"] = {"server": proxy}
    return kwargs


def _launcher(stack: str):
    """返回 camoufox 的 Camoufox 类，或 playwright 的 sync_playwright。"""
    if stack == "camoufox":
        camoufox = _try_import("camoufox.sync_api")
        if not camoufox or not hasattr(camoufox, "Camoufox"):
            raise BrowserUnavailable("camoufox 已安装但 camoufox.sync_api 导入失败")
        return camoufox.Camoufox
    if stack == "patchright":
        mod = _try_import("patchright.sync_api")
    else:
        mod = _try_import("playwright.sync_api")
    if not mod or not hasattr(mod, "sync_playwright"):
        raise BrowserUnavailable(f"{stack} 导入失败（{BROWSER_UNAVAILABLE_HINT}）")
    return mod.sync_playwright


GOOGLE_ID_SELECTORS = [
    'input[type="email"]',
    "#identifierId",
    'input[name="identifier"]',
]
GOOGLE_PW_SELECTORS = [
    'input[type="password"]',
    'input[name="Passwd"]',
]
GOOGLE_NEXT_SELECTORS = [
    "#identifierNext",
    "#passwordNext",
    'button:has-text("Next")',
    'button:has-text("下一步")',
]
GOOGLE_TOTP_SELECTORS = [
    'input[name="totpPin"]',
    'input[type="tel"]',
    'input[aria-label*="code"]',
]
GOOGLE_CONSENT_SELECTORS = [
    'button:has-text("Continue")',
    'button:has-text("Allow")',
    'button:has-text("继续")',
    "#submit_approve_access",
]
APP_GOOGLE_BUTTON = [
    'button:has-text("Google")',
    'a:has-text("Google")',
    'button:has-text("Continue with Google")',
    'a[href*="signin/google"]',
    'div[role="button"]:has-text("Google")',
]
KEY_PAGE_PATHS = ["/dashboard", "/keys", "/agent-api"]
CREATE_KEY_SELECTORS = [
    'button:has-text("Get an API Key")',
    'button:has-text("Create API key")',
    'button:has-text("Create key")',
    'button:has-text("New key")',
    'button:has-text("Generate")',
    'button:has-text("Get API Key")',
]


def _first_visible(page, selectors: list[str], timeout_ms: int = 8000):
    for selector in selectors:
        try:
            locator = page.locator(selector).first
            locator.wait_for(state="visible", timeout=timeout_ms)
            return locator
        except Exception:  # noqa: BLE001 - 选择器不匹配就试下一个
            continue
    return None


def _dump(page, out_dir: str | None, tag: str) -> str:
    if not out_dir:
        return ""
    try:
        os.makedirs(out_dir, exist_ok=True)
        path = os.path.join(out_dir, f"{tag}-{int(time.time())}.html")
        with open(path, "w", encoding="utf-8") as handle:
            handle.write(page.content())
        return path
    except Exception:  # noqa: BLE001
        return ""


class FingerprintBrowser:
    """一个上下文 = 一个账号；退出时整块销毁（或落回 profile_dir）。"""

    def __init__(
        self,
        *,
        config: Any,
        persona: Persona,
        proxy: str = "",
        stack: str = "auto",
        headless: bool = True,
        cdp: str = "",
        humanize: float = 1.0,
        screenshots: str | None = None,
        log: Callable[[str], None] | None = None,
    ) -> None:
        self.config = config
        self.persona = persona
        self.proxy = proxy
        self.stack = stack
        self.headless = headless
        self.cdp = cdp
        self.humanize = max(0.2, float(humanize or 1.0))
        self.screenshots = screenshots
        self.log = log or (lambda _m: None)
        self._stack_used = "playwright"
        self._cm = None
        self.playwright = None
        self.browser = None
        self.context = None
        self.page = None

    # ------------------------------------------------------------------ 启动
    def __enter__(self) -> "FingerprintBrowser":
        self.start()
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()

    def start(self) -> None:
        self._stack_used = detect_stack(self.stack)
        launcher = _launcher(self._stack_used)

        if self._stack_used == "camoufox":
            self._start_camoufox(launcher)
            return
        self._start_chromium(launcher)

    def _start_camoufox(self, launcher) -> None:
        geoip = bool(getattr(self.config, "geo_align", True))
        kwargs = camoufox_launch_kwargs(
            headless=self.headless,
            humanize=self.humanize,
            proxy=self.proxy,
            geoip=geoip,
            locale=self.persona.locale,
            os_name=CAMOUFOX_OS.get(self.persona.platform, "windows"),
            disable_coop=bool(getattr(self.config, "disable_coop", True)),
            profile_dir=str(getattr(self.config, "profile_dir", "") or ""),
            screen=(self.persona.screen[0], self.persona.screen[1]),
        )
        try:
            self._cm = launcher(**kwargs)
            opened = self._cm.__enter__()
        except Exception as cause:  # noqa: BLE001
            raise BrowserUnavailable(f"camoufox 启动失败：{cause}（是否忘了 `camoufox fetch`？）") from cause

        if hasattr(opened, "new_context") and not hasattr(opened, "contexts"):
            # 返回的是 Browser（非 persistent）：再开一个上下文，方便统一 add_cookies
            self.browser = opened
            self.context = self.browser.new_context()
            self.page = self.context.new_page()
        else:
            # persistent_context=True → 返回 BrowserContext
            self.context = opened
            self.browser = opened
            pages = getattr(opened, "pages", [])
            self.page = pages[0] if pages else self.context.new_page()
        self.log(
            f"浏览器内核：camoufox（headless={self.headless}, geoip={geoip}, "
            f"ver={camoufox_installed_version() or '-'}）"
        )

    def _start_chromium(self, launcher) -> None:
        self.playwright = launcher().start()
        chromium = self.playwright.chromium
        launch_args: dict[str, Any] = {
            "headless": self.headless,
            "args": [
                "--disable-blink-features=AutomationControlled",
                "--no-first-run",
                "--no-default-browser-check",
            ],
        }
        proxy_settings = {"server": self.proxy} if self.proxy else None
        if self.cdp:
            try:
                self.browser = chromium.connect_over_cdp(self.cdp)
                context = self.browser.contexts[0] if self.browser.contexts else self.browser.new_context()
                self.context = context
                self.page = context.pages[0] if context.pages else context.new_page()
                self.log(f"接管外部指纹浏览器 CDP：{self.cdp}")
                return
            except Exception as cause:  # noqa: BLE001
                raise BrowserUnavailable(f"CDP 接管失败（{self.cdp}）：{cause}") from cause

        try:
            self.browser = chromium.launch(**launch_args, proxy=proxy_settings)
        except TypeError:
            self.browser = chromium.launch(**launch_args)
        self.context = self.browser.new_context(**chromium_context_kwargs(self.persona))
        try:
            self.context.add_init_script(
                "Object.defineProperty(navigator, 'webdriver', {get: () => undefined});"
            )
        except Exception:  # noqa: BLE001
            pass
        self.page = self.context.new_page()
        self.log(f"浏览器内核：{self._stack_used}（headless={self.headless}, tz={self.persona.timezone}）")

    def close(self) -> None:
        # storage_state 落盘：下次同身份直接复用登录态（antiban.reuse_session）
        state_path = str(getattr(self.config, "storage_state", "") or "")
        if state_path and self.context is not None:
            try:
                self.context.storage_state(path=state_path)
            except Exception:  # noqa: BLE001
                pass
        for closer in (
            lambda: self.context and self.context.close(),
            lambda: self.browser and self.browser is not self.context and self.browser.close(),
            lambda: self.playwright and self.playwright.stop(),
            lambda: self._cm and self._cm.__exit__(None, None, None),
        ):
            try:
                closer()
            except Exception:  # noqa: BLE001
                continue

    # ------------------------------------------------------------------ 工具
    def _pause(self, low_ms: int = 300, high_ms: int = 1200) -> None:
        time.sleep(random.uniform(low_ms, high_ms) / 1000.0 * self.humanize)

    def _shot(self, tag: str) -> None:
        if not self.screenshots or not self.page:
            return
        try:
            os.makedirs(self.screenshots, exist_ok=True)
            self.page.screenshot(path=os.path.join(self.screenshots, f"{tag}-{int(time.time())}.png"), full_page=False)
        except Exception:  # noqa: BLE001
            pass

    def _type_like_human(self, locator, text: str) -> None:
        try:
            locator.click()
        except Exception:  # noqa: BLE001
            pass
        for ch in text:
            try:
                locator.type(ch, delay=int(random.uniform(40, 130) * self.humanize))
            except Exception:  # noqa: BLE001
                try:
                    locator.fill(text)
                except Exception:  # noqa: BLE001
                    pass
                return

    def _wait_url(self, predicate: Callable[[str], bool], timeout_s: float = 60.0) -> bool:
        deadline = time.time() + timeout_s
        while time.time() < deadline:
            try:
                url = self.page.url
            except Exception:  # noqa: BLE001
                return False
            if predicate(url):
                return True
            try:
                self.page.wait_for_timeout(500)
            except Exception:  # noqa: BLE001
                time.sleep(0.5)
        return False

    # ------------------------------------------------------------- 登录流程
    def _click_google(self) -> None:
        button = _first_visible(self.page, APP_GOOGLE_BUTTON, timeout_ms=15000)
        if not button:
            self._dump_page("app-login-no-google")
            raise OAuthDenied("登录页上没找到 Google 按钮（选择器要更新）")
        button.click()
        self._pause(600, 1600)

    def _google_login(self, identity: GoogleIdentity) -> None:
        self.log("Google 登录页…")
        email_box = _first_visible(self.page, GOOGLE_ID_SELECTORS, timeout_ms=20000)
        if email_box is None:
            if "accounts.google.com" not in self.page.url:
                return
            self._dump_page("google-no-email-box")
            raise OAuthDenied("Google 登录页没出现邮箱输入框")

        self._type_like_human(email_box, identity.email)
        nxt = _first_visible(self.page, GOOGLE_NEXT_SELECTORS, timeout_ms=6000)
        if nxt:
            nxt.click()
        else:
            try:
                email_box.press("Enter")
            except Exception:  # noqa: BLE001
                pass
        self._pause(1200, 2600)

        pw_box = _first_visible(self.page, GOOGLE_PW_SELECTORS, timeout_ms=15000)
        if pw_box is None:
            challenge = _first_visible(self.page, ['div[role="link"]', 'text=Use another account'], timeout_ms=4000)
            if challenge:
                challenge.click()
                self._pause(800, 1800)
                pw_box = _first_visible(self.page, GOOGLE_PW_SELECTORS, timeout_ms=12000)
        if pw_box is None:
            self._shot("google-no-password")
            self._dump_page("google-no-password")
            raise OAuthDenied("Google 没给密码框：这个身份需要人工过一次（设备验证/短信）")

        if not identity.password:
            raise OAuthDenied("身份没有密码，且 Google 要求输入密码")
        self._type_like_human(pw_box, identity.password)
        nxt = _first_visible(self.page, GOOGLE_NEXT_SELECTORS, timeout_ms=6000)
        if nxt:
            nxt.click()
        else:
            try:
                pw_box.press("Enter")
            except Exception:  # noqa: BLE001
                pass
        self._pause(1500, 3000)

        totp_box = _first_visible(self.page, GOOGLE_TOTP_SELECTORS, timeout_ms=6000)
        if totp_box is not None:
            code = totp_now(identity.totp_secret) if identity.totp_secret else ""
            if not code:
                raise OAuthDenied("Google 要求二次验证码，但身份没配 totp_secret")
            self._type_like_human(totp_box, code)
            nxt = _first_visible(self.page, GOOGLE_NEXT_SELECTORS, timeout_ms=5000)
            if nxt:
                nxt.click()
            self._pause(1200, 2400)

        if "accounts.google.com" in self.page.url:
            consent = _first_visible(self.page, GOOGLE_CONSENT_SELECTORS, timeout_ms=8000)
            if consent:
                consent.click()
                self._pause(800, 1800)

    def _read_key_from_dom(self) -> str:
        for path in KEY_PAGE_PATHS:
            try:
                self.page.goto(f"{self.config.base_url}{path}", wait_until="domcontentloaded", timeout=45000)
            except Exception:  # noqa: BLE001
                continue
            self._pause(900, 2000)
            try:
                text = self.page.inner_text("body")
            except Exception:  # noqa: BLE001
                text = ""
            match = KEY_RE.search(text or "")
            if match:
                return match.group(0)
            button = _first_visible(self.page, CREATE_KEY_SELECTORS, timeout_ms=4000)
            if button:
                try:
                    button.click()
                    self._pause(1200, 2600)
                    text = self.page.inner_text("body")
                    match = KEY_RE.search(text or "")
                    if match:
                        return match.group(0)
                except Exception:  # noqa: BLE001
                    pass
            self._shot(f"key-page{path.replace('/', '-')}")
        return ""

    def _dump_page(self, tag: str) -> None:
        path = _dump(self.page, self.screenshots, tag)
        if path:
            self.log(f"DOM 快照：{path}")

    # ------------------------------------------------------------------ 主流程
    def signup_flow(
        self,
        identity: GoogleIdentity,
        *,
        keys: int = 1,
        key_name: str = "",
        session_cookies: list[dict] | None = None,
    ) -> Registration:
        if self.page is None:
            raise BrowserUnavailable("浏览器没启动")

        if session_cookies:
            try:
                self.context.add_cookies(session_cookies)
                self.log(f"注入已有 app 会话 cookies {len(session_cookies)} 条")
            except Exception as cause:  # noqa: BLE001
                self.log(f"注入 cookies 失败：{cause}")

        self.page.goto(f"{self.config.base_url}/login", wait_until="domcontentloaded", timeout=60000)
        self._pause(700, 1600)
        self._shot("login")

        if "/login" in self.page.url:
            self._click_google()
            if "accounts.google.com" in self.page.url:
                self._google_login(identity)
            elif identity.cookies:
                try:
                    self.context.add_cookies(identity.cookies)
                except Exception:  # noqa: BLE001
                    pass

            ok = self._wait_url(lambda u: "app.blackbox.ai" in u and "/login" not in u, timeout_s=90)
            if not ok:
                self._shot("after-google")
                self._dump_page("after-google")
                raise OAuthDenied(f"授权没回到 app（当前 {self.page.url[:120]}）")

        self._pause(1200, 2400)
        key = self._read_key_from_dom()
        if not key:
            self._shot("no-key")
            raise NoApiKey("面板里读不到 sk- Key（可能需要 Pro 档，或页面选择器要更新）")

        cookies: list[dict] = []
        try:
            cookies = [
                {"name": c.get("name"), "value": c.get("value"), "domain": c.get("domain", ""), "path": c.get("path", "/")}
                for c in self.context.cookies()
                if c.get("name")
            ]
        except Exception:  # noqa: BLE001
            cookies = []

        return Registration(
            email=identity.email,
            api_key=key,
            key_prefix=key[:8],
            key_last4=key[-4:],
            key_source="dom",
            engine="browser",
            cookies=cookies,
            model=self.config.model,
            note=identity.note,
        )


def run_browser_registration(
    *,
    config: Any,
    persona: Persona,
    identity: GoogleIdentity,
    proxy: str = "",
    log: Callable[[str], None] | None = None,
    keys: int = 1,
    key_name: str = "",
    session_cookies: list[dict] | None = None,
) -> Registration:
    screenshots = os.path.join(str(config.out_dir), "screenshots") if getattr(config, "out_dir", "") else None
    with FingerprintBrowser(
        config=config,
        persona=persona,
        proxy=proxy,
        stack=config.browser_stack,
        headless=not config.headful,
        cdp=config.cdp,
        humanize=config.humanize or 1.0,
        screenshots=screenshots,
        log=log,
    ) as browser:
        return browser.signup_flow(
            identity,
            keys=keys,
            key_name=key_name,
            session_cookies=session_cookies,
        )
