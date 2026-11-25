"""指纹浏览器引擎：OAuth 登录 + /accesskey 领 key。

WaveSpeed 没有邮箱注册，所以"注册"实际是这条链路：

    /sign-in  →  点 Google / GitHub  →  第三方授权  →  回调落 /dashboard
              →  导航 /accesskey  →  填名字点 Generate  →  读一次性明文

四套内核，按指纹质量从高到低自动选：

  1. Camoufox   —— Firefox 内核 + C++ 层指纹改写，canvas/WebGL/fonts/screen/
                   时区/语言每次启动一套自洽组合。
  2. Patchright  —— Playwright 的源码级反检测分支（去 Runtime 域、隔离执行上下文）。
  3. Playwright Chromium —— 兜底，最小 patch（webdriver / languages / plugins）。
  4. 外部 CDP    —— AdsPower / BitBrowser / Morelogin 这类工具开的本地端口，
                   指纹、代理、登录态全用那边的，我们只负责点按钮读文本。

OAuth 的第三方登录页（Google / GitHub）需要**真人凭据或已登录的 profile**，
所以本模块支持两种接法：
  * `cdp` 接管一个已登录的指纹浏览器 profile（推荐，最省事）；
  * `oauth_credentials` 里给邮箱密码，脚本替人填（Google 通常会拦自动化登录，
    GitHub 相对宽松）。
两条都走不通时抛 `OAuthUnavailable`，让上层换账号而不是原地重试。
"""

from __future__ import annotations

import random
import re
import time
from pathlib import Path
from typing import Any, Callable
from urllib.parse import urlparse

from .camoufox_options import camoufox_launch_options, describe_launch
from .errors import (
    Blocked,
    CaptchaRequired,
    KeyNotObtainable,
    NoApiKey,
    OAuthUnavailable,
    SignupError,
)

# ---- 选择器 ---------------------------------------------------------------
#
# 面板是 Next.js + shadcn 风格；优先用"文本 + role"，其次是稳定的属性。
SIGN_IN_PATH = "/sign-in"
DASHBOARD_PATH = "/dashboard"
ACCESS_KEY_PATH = "/accesskey"

OAUTH_BUTTON_TEXTS = {
    "google": re.compile(r"google", re.I),
    "github": re.compile(r"github", re.I),
    "aws": re.compile(r"aws|amazon", re.I),
}
GENERATE_TEXTS = re.compile(r"generate|create\s*(new\s*)?key|new\s*key|add\s*key", re.I)
KEY_NAME_INPUTS = (
    "input[placeholder*='name' i]",
    "input[name*='name' i]",
    "input[autocomplete='off']",
)
REVEAL_TEXTS = re.compile(r"copy|reveal|show", re.I)
ERROR_SELECTOR = "[role='alert'], .text-destructive, [data-sonner-toast], .error"

# Google / GitHub 登录页
GOOGLE_EMAIL = "input[type='email']"
GOOGLE_PASSWORD = "input[type='password']"
GITHUB_LOGIN = "#login_field"
GITHUB_PASSWORD = "#password"

# 一个 WaveSpeed key 的字符集与长度：面板没有固定前缀，只能按"够长的
# 高熵串"识别。这个正则同时用于 DOM 文本和剪贴板。
KEY_RE = re.compile(r"\b[A-Za-z0-9_-]{28,80}\b")


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
    """没有任何可用的浏览器内核（缺依赖 / 缺二进制）。"""

    kind = "browser_unavailable"


class FingerprintBrowser:
    """一次注册 = 一个浏览上下文 = 一套指纹 + 一份 cookie。"""

    def __init__(
        self,
        *,
        base_url: str = "https://wavespeed.ai",
        headless: bool = True,
        proxy: str = "",
        cdp: str = "",
        stack: str = "auto",
        locale: str = "en-US",
        timezone_id: str = "",
        geoip: bool = True,
        timeout: float = 60.0,
        humanize: float = 1.0,
        turnstile_timeout: float = 50.0,
        screenshots: Path | str | None = None,
        fingerprint: dict[str, Any] | None = None,
        profile_dir: str = "",
        oauth_wait: float = 120.0,
        accesskey_wait: float = 60.0,
        oauth_credentials: dict[str, str] | None = None,
        pin_cpu_cores: bool = True,
        use_preset: bool = False,
        canvas_noise: bool | None = None,
        log: Callable[[str], None] | None = None,
    ) -> None:
        self.base = base_url.rstrip("/")
        self.headless = headless
        self.proxy = proxy
        self.cdp = cdp
        self.stack_pref = (stack or "auto").strip().lower()
        self.locale = locale
        self.timezone_id = timezone_id
        self.geoip = geoip
        self.timeout = timeout
        self.humanize = max(0.0, float(humanize))
        self.turnstile_timeout = turnstile_timeout
        self.screenshots = Path(screenshots) if screenshots else None
        self.fingerprint = fingerprint or {}
        self.profile_dir = profile_dir
        self.oauth_wait = oauth_wait
        self.accesskey_wait = accesskey_wait
        self.oauth_credentials = oauth_credentials or {}
        self.pin_cpu_cores = pin_cpu_cores
        self.use_preset = use_preset
        self.canvas_noise = canvas_noise
        self.log = log or (lambda _m: None)
        self.rng = random.Random()
        self._pw = None
        self._browser = None
        self._context = None
        self._camoufox = None
        self.page = None
        self.stack = ""

    # ------------------------------------------------------------- lifecycle
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
        if self.stack_pref in ("auto", "patchright") and self._try_playwright("patchright"):
            return
        if self._try_playwright("playwright"):
            return
        raise BrowserUnavailable(
            "没有可用的浏览器内核：pip install camoufox[geoip] 或 pip install playwright && playwright install chromium"
        )

    def _start_cdp(self) -> None:
        """接管外部指纹浏览器的 profile（AdsPower / BitBrowser / Morelogin…）。"""
        try:
            from playwright.sync_api import sync_playwright
        except Exception as exc:  # noqa: BLE001
            raise BrowserUnavailable(f"CDP 模式需要 playwright：{exc}") from exc
        self._pw = sync_playwright().start()
        self._browser = self._pw.chromium.connect_over_cdp(self.cdp)
        self._context = (self._browser.contexts or [self._browser.new_context()])[0]
        self.page = self._context.pages[0] if self._context.pages else self._context.new_page()
        self.stack = "cdp"
        self.log(f"已接管外部浏览器 CDP：{self.cdp}")

    def _try_camoufox(self) -> bool:
        try:
            from camoufox.sync_api import Camoufox
        except Exception as exc:  # noqa: BLE001 - 可选依赖
            self.log(f"camoufox 不可用（{exc.__class__.__name__}），换下一个内核")
            return False

        # 参数构造与校验集中在 camoufox_options.py —— 那里记录了三条实测硬约束：
        # timezone 非法、os 不能传列表、screen 必须容纳 window。
        options = camoufox_launch_options(
            fingerprint=self.fingerprint,
            proxy=self.proxy,
            headless=self.headless,
            humanize=self.humanize,
            locale=self.locale,
            geo_align=self.geoip,
            pin_cpu_cores=self.pin_cpu_cores,
            use_preset=self.use_preset,
            canvas_noise=self.canvas_noise,
        )
        if self.fingerprint.get("timezone") and "geoip" not in options:
            # 有期望时区但没法让 camoufox 跟随（无代理 / 域名解析失败）：说清楚
            self.log(
                f"时区无法跟随出口（期望 {self.fingerprint['timezone']}）："
                "camoufox 只接受 geoip=<IP>，没有可用出口 IP 时只能用宿主时区"
            )
        try:
            self._camoufox = Camoufox(**options)
            self._browser = self._camoufox.__enter__()
        except Exception as exc:  # noqa: BLE001 - 二进制没下 / 启动失败
            self.log(f"camoufox 启动失败（{exc}），换下一个内核")
            return False
        contexts = list(getattr(self._browser, "contexts", []) or [])
        self._context = contexts[0] if contexts else self._browser.new_context()
        self.page = self._context.new_page()
        self.stack = "camoufox"
        self.log(f"指纹内核：{describe_launch(options)}")
        return True

    def _try_playwright(self, flavour: str) -> bool:
        try:
            if flavour == "patchright":
                from patchright.sync_api import sync_playwright
            else:
                from playwright.sync_api import sync_playwright
        except Exception as exc:  # noqa: BLE001
            self.log(f"{flavour} 不可用（{exc.__class__.__name__}）")
            return False
        self._pw = sync_playwright().start()
        launch: dict[str, Any] = {
            "headless": self.headless,
            "args": [
                "--disable-blink-features=AutomationControlled",
                "--no-first-run",
                "--no-default-browser-check",
                "--disable-features=Translate,BackForwardCache",
            ],
            "ignore_default_args": ["--enable-automation"],
        }
        if self.proxy:
            launch["proxy"] = _proxy_dict(self.proxy)
        try:
            if self.profile_dir:
                self._context = self._pw.chromium.launch_persistent_context(self.profile_dir, **launch)
                self._browser = None
            else:
                self._browser = self._pw.chromium.launch(**launch)
        except Exception as exc:  # noqa: BLE001 - 二进制缺失
            self.log(f"{flavour} 启动失败（{exc}）")
            try:
                self._pw.stop()
            except Exception:  # noqa: BLE001
                pass
            self._pw = None
            return False

        if self._context is None:
            fp = self.fingerprint
            viewport = fp.get("viewport") or {"width": 1440, "height": 900}
            self._context = self._browser.new_context(
                locale=fp.get("locale") or self.locale,
                timezone_id=fp.get("timezone") or self.timezone_id or None,
                user_agent=fp.get("user_agent")
                or (
                    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                    f"(KHTML, like Gecko) Chrome/{self.rng.choice([131, 132, 133, 134])}.0.0.0 Safari/537.36"
                ),
                viewport=viewport,
                screen=fp.get("screen") or {"width": 1920, "height": 1080},
                device_scale_factor=fp.get("device_scale_factor") or 1,
                extra_http_headers={"Accept-Language": fp.get("accept_language") or "en-US,en;q=0.9"},
            )
            self._context.add_init_script(_patch_js(fp))
        self.page = self._context.pages[0] if self._context.pages else self._context.new_page()
        self.stack = flavour
        self.log(f"指纹内核：{flavour}（Chromium + 最小 patch）")
        return True

    def close(self) -> None:
        try:
            if self._context is not None and self.stack != "cdp":
                self._context.close()
        except Exception:  # noqa: BLE001
            pass
        try:
            if self._camoufox is not None:
                self._camoufox.__exit__(None, None, None)
            elif self._browser is not None and self.stack != "cdp":
                self._browser.close()
        except Exception:  # noqa: BLE001
            pass
        try:
            if self._pw is not None:
                self._pw.stop()
        except Exception:  # noqa: BLE001
            pass
        self._pw = self._browser = self._context = self.page = None

    # --------------------------------------------------------------- helpers
    def _shot(self, tag: str) -> None:
        if not self.screenshots or self.page is None:
            return
        try:
            self.screenshots.mkdir(parents=True, exist_ok=True)
            self.page.screenshot(path=str(self.screenshots / f"{tag}-{int(time.time())}.png"), full_page=False)
        except Exception:  # noqa: BLE001
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

    def _body_text(self) -> str:
        try:
            return self.page.locator("body").inner_text(timeout=1500) or ""
        except Exception:  # noqa: BLE001
            return ""

    def _page_error(self) -> str:
        try:
            node = self.page.locator(ERROR_SELECTOR).first
            if node.count():
                return re.sub(r"\s+", " ", node.inner_text(timeout=800) or "").strip()
        except Exception:  # noqa: BLE001
            pass
        return ""

    def cookies(self) -> list[dict[str, str]]:
        try:
            jar = self._context.cookies()
        except Exception:  # noqa: BLE001
            return []
        return [
            {
                "name": str(c.get("name", "")),
                "value": str(c.get("value", "")),
                "domain": str(c.get("domain", "")),
                "path": str(c.get("path", "/")),
            }
            for c in jar
        ]

    def add_cookies(self, cookies: list[dict[str, str]]) -> None:
        if not cookies:
            return
        try:
            self._context.add_cookies(
                [
                    {
                        "name": str(c.get("name", "")),
                        "value": str(c.get("value", "")),
                        "domain": str(c.get("domain") or "") or None,
                        "path": str(c.get("path") or "/"),
                    }
                    for c in cookies
                    if c.get("name")
                ]
            )
        except Exception as exc:  # noqa: BLE001
            self.log(f"注入 cookie 失败：{exc}")

    # ------------------------------------------------------------ 登录态判定
    def is_signed_in(self) -> bool:
        """登录态判定：URL 落在 dashboard 系，或页面上有 accesskey 入口。"""
        url = self.page.url or ""
        if any(part in url for part in ("/dashboard", "/accesskey", "/history", "/top-up")):
            return True
        text = self._body_text().lower()
        return any(token in text for token in ("sign out", "log out", "api keys", "accesskey", "dashboard"))

    # ---------------------------------------------------------------- OAuth
    def open_sign_in(self) -> None:
        self.page.goto(f"{self.base}{SIGN_IN_PATH}", wait_until="domcontentloaded", timeout=int(self.timeout * 1000))
        text = self._body_text().lower()
        if "just a moment" in text or "checking your browser" in text:
            raise Blocked("sign-in 页被 Cloudflare 挑战挡住")
        self._shot(f"signin-{self.stack}")

    def click_oauth(self, provider: str) -> None:
        """点 Google / GitHub / AWS 登录按钮。"""
        pattern = OAUTH_BUTTON_TEXTS.get(provider.lower())
        if not pattern:
            raise OAuthUnavailable(f"未知 OAuth 提供方：{provider}")
        # 按钮文案可能只是 "Sign in with Google"，也可能只有 logo，所以先试文本再试 href。
        try:
            button = self.page.get_by_role("button", name=pattern).first
            if button.count():
                button.click(timeout=5000)
                self._pause(300, 800)
                return
        except Exception:  # noqa: BLE001
            pass
        try:
            link = self.page.get_by_role("link", name=pattern).first
            if link.count():
                link.click(timeout=5000)
                self._pause(300, 800)
                return
        except Exception:  # noqa: BLE001
            pass
        # 最后一招：页面上任何含 provider 名的可点元素
        try:
            node = self.page.locator(f"button:has-text('{provider}'), a:has-text('{provider}')").first
            if node.count():
                node.click(timeout=5000)
                self._pause(300, 800)
                return
        except Exception:  # noqa: BLE001
            pass
        raise OAuthUnavailable(f"sign-in 页上找不到 {provider} 登录按钮")

    def fill_oauth_credentials(self, provider: str) -> bool:
        """在第三方登录页替人填账号密码。

        Google 对自动化登录拦截很凶，通常走不通；GitHub 宽松一些。
        返回 True 表示表单已提交（不代表授权成功）。
        """
        creds = self.oauth_credentials or {}
        email = creds.get("email") or creds.get("username") or ""
        password = creds.get("password") or ""
        if not (email and password):
            return False

        if provider == "google":
            try:
                self.page.wait_for_selector(GOOGLE_EMAIL, timeout=15000)
                self._human_type(self.page.locator(GOOGLE_EMAIL).first, email)
                self.page.get_by_role("button", name=re.compile(r"next", re.I)).first.click()
                self.page.wait_for_selector(GOOGLE_PASSWORD, timeout=20000)
                self._human_type(self.page.locator(GOOGLE_PASSWORD).first, password)
                self.page.get_by_role("button", name=re.compile(r"next", re.I)).first.click()
                return True
            except Exception as exc:  # noqa: BLE001
                self.log(f"Google 表单填充失败（{exc.__class__.__name__}）")
                return False

        if provider == "github":
            try:
                self.page.wait_for_selector(GITHUB_LOGIN, timeout=15000)
                self._human_type(self.page.locator(GITHUB_LOGIN).first, email)
                self._human_type(self.page.locator(GITHUB_PASSWORD).first, password)
                self.page.get_by_role("button", name=re.compile(r"sign in|log in", re.I)).first.click()
                return True
            except Exception as exc:  # noqa: BLE001
                self.log(f"GitHub 表单填充失败（{exc.__class__.__name__}）")
                return False
        return False

    def wait_oauth_callback(self, timeout: float | None = None) -> bool:
        """等 OAuth 回调落回面板。返回 True 表示已登录。"""
        deadline = time.time() + (timeout or self.oauth_wait)
        while time.time() < deadline:
            url = self.page.url or ""
            if "accounts.google.com" in url or "github.com/login" in url or "signin.aws.amazon.com" in url:
                # 还停在第三方页：如果配了凭据就填，否则等（人可能在外面点）
                for provider in ("google", "github", "aws"):
                    host = {"google": "accounts.google.com", "github": "github.com", "aws": "signin.aws.amazon.com"}[provider]
                    if host in url:
                        self.fill_oauth_credentials(provider)
                        break
                self.page.wait_for_timeout(2500)
                continue
            if self.is_signed_in():
                self._shot("oauth-ok")
                return True
            text = self._body_text().lower()
            if "verify you are human" in text or "just a moment" in text:
                self._wait_turnstile()
                continue
            message = self._page_error()
            if message and ("denied" in message.lower() or "failed" in message.lower()):
                self._shot("oauth-error")
                raise OAuthUnavailable(f"OAuth 授权失败：{message}")
            self.page.wait_for_timeout(1200)
        self._shot("oauth-timeout")
        return False

    def _wait_turnstile(self) -> str:
        """托管挑战通常自己过；过不去就点一下复选框，再不行交回上层。"""
        deadline = time.time() + self.turnstile_timeout
        clicked = False
        while time.time() < deadline:
            state = self.page.evaluate(
                """() => {
                  const input = document.querySelector("input[name='cf-turnstile-response']");
                  const frame = document.querySelector("iframe[src*='challenges.cloudflare.com']");
                  return { token: input ? input.value : '', widget: Boolean(input || frame) };
                }"""
            )
            if state.get("token"):
                self.log("turnstile：拿到 token")
                return str(state["token"])
            if state.get("widget") and not clicked:
                try:
                    frame = self.page.frame_locator("iframe[src*='challenges.cloudflare.com']").first
                    box = frame.locator("input[type='checkbox'], label").first
                    if box.count():
                        box.click(timeout=1500)
                        clicked = True
                        self.log("turnstile：点击了人机校验复选框")
                except Exception:  # noqa: BLE001 - 托管模式没有可点元素
                    clicked = True
            self.page.wait_for_timeout(900)
        self.log("turnstile：等待超时，交给上层")
        return ""

    # ------------------------------------------------------- /accesskey 取 key
    def generate_key(self, name: str = "") -> str:
        """打开 /accesskey，点 Generate，读一次性明文。

        明文可能出现在三个地方，按可靠性排序：
          1. 页面上显式的 key 展示区（含 "copy" 按钮）；
          2. 一次性弹窗 / toast；
          3. 剪贴板（点了 Copy 之后）。
        """
        self.page.goto(f"{self.base}{ACCESS_KEY_PATH}", wait_until="domcontentloaded", timeout=int(self.timeout * 1000))
        if "sign-in" in (self.page.url or "") or "/login" in (self.page.url or ""):
            raise OAuthUnavailable("/accesskey 跳回登录页，会话无效")
        self._shot(f"accesskey-{self.stack}")

        before = set(KEY_RE.findall(self._body_text()))

        # 1) 填名字（可选）
        if name:
            for selector in KEY_NAME_INPUTS:
                try:
                    locator = self.page.locator(selector).first
                    if locator.count() and locator.is_visible():
                        self._human_type(locator, name)
                        break
                except Exception:  # noqa: BLE001
                    continue

        # 2) 点 Generate
        clicked = False
        for getter in (
            lambda: self.page.get_by_role("button", name=GENERATE_TEXTS).first,
            lambda: self.page.locator("button:has-text('Generate'), button:has-text('Create')").first,
        ):
            try:
                button = getter()
                if button.count():
                    button.click(timeout=6000)
                    clicked = True
                    self._pause(500, 1200)
                    break
            except Exception:  # noqa: BLE001
                continue
        if not clicked:
            raise NoApiKey("/accesskey 页上找不到 Generate 按钮（页面结构可能变了）")

        # 3) 读明文
        deadline = time.time() + self.accesskey_wait
        while time.time() < deadline:
            text = self._body_text()
            fresh = [k for k in KEY_RE.findall(text) if k not in before]
            # 过滤掉明显不是 key 的长串（URL 片段、哈希、日期）
            fresh = [k for k in fresh if not re.fullmatch(r"[0-9a-f]{32,64}", k) or len(k) >= 40]
            if fresh:
                candidate = max(fresh, key=len)
                self._shot(f"accesskey-key-{self.stack}")
                self.log(f"读到 key 候选（{len(candidate)} 字符）")
                return candidate
            # 试着点 Copy，然后读剪贴板
            try:
                copy_btn = self.page.get_by_role("button", name=REVEAL_TEXTS).first
                if copy_btn.count():
                    copy_btn.click(timeout=2000)
                    self._pause(200, 500)
                    clip = self.page.evaluate("async () => { try { return await navigator.clipboard.readText(); } catch (e) { return ''; } }")
                    if clip and KEY_RE.fullmatch(str(clip).strip()):
                        self._shot(f"accesskey-clip-{self.stack}")
                        return str(clip).strip()
            except Exception:  # noqa: BLE001
                pass
            self.page.wait_for_timeout(1000)

        self._shot("accesskey-timeout")
        raise KeyNotObtainable(
            f"/accesskey 点了 Generate，但 {self.accesskey_wait:.0f}s 内没读到明文；"
            "WaveSpeed 的 key 只显示一次，可能已被其他标签页消费"
        )

    # ------------------------------------------------------------- 复合流程
    def signup_flow(
        self,
        *,
        oauth_provider: str = "google",
        key_name: str = "",
        existing_cookies: list[dict[str, str]] | None = None,
        log: Callable[[str], None] | None = None,
    ) -> dict[str, Any]:
        """完整走一遍：登录态 → OAuth → /accesskey 取 key。"""
        say = log or self.log
        if existing_cookies:
            self.add_cookies(existing_cookies)
            self.page.goto(f"{self.base}{DASHBOARD_PATH}", wait_until="domcontentloaded", timeout=int(self.timeout * 1000))
            if self.is_signed_in():
                say("复用已有 cookie 会话，跳过 OAuth")
            else:
                say("cookie 已失效，重新走 OAuth")
                self.open_sign_in()
                self.click_oauth(oauth_provider)
                if not self.wait_oauth_callback():
                    raise OAuthUnavailable("OAuth 回调没有落到面板（会话未建立）")
        else:
            self.open_sign_in()
            self.click_oauth(oauth_provider)
            if not self.wait_oauth_callback():
                raise OAuthUnavailable("OAuth 回调没有落到面板（会话未建立）")

        api_key = self.generate_key(key_name)
        return {
            "api_key": api_key,
            "api_key_source": "accesskey-ui",
            "cookies": self.cookies(),
            "browser": self.stack,
            "oauth_provider": oauth_provider,
        }


def _patch_js(fp: dict[str, Any]) -> str:
    """最小反检测 patch + 指纹对齐（只在 Playwright 兜底路径用）。"""
    cores = int(fp.get("hardware_concurrency") or 8)
    memory = int(fp.get("device_memory") or 8)
    vendor = str(fp.get("webgl_vendor") or "Google Inc. (NVIDIA)")
    renderer = str(fp.get("webgl_renderer") or "")
    return f"""(() => {{
  Object.defineProperty(navigator, 'webdriver', {{ get: () => undefined }});
  Object.defineProperty(navigator, 'hardwareConcurrency', {{ get: () => {cores} }});
  Object.defineProperty(navigator, 'deviceMemory', {{ get: () => {memory} }});
  Object.defineProperty(navigator, 'plugins', {{ get: () => [1, 2, 3, 4, 5] }});
  Object.defineProperty(navigator, 'languages', {{ get: () => ['{fp.get("locale") or "en-US"}', 'en'] }});
  const orig = window.chrome ?? {{}};
  window.chrome = {{ ...orig, runtime: orig.runtime ?? {{}} }};
  try {{
    const patchGL = (proto) => {{
      if (!proto) return;
      const getParameter = proto.getParameter;
      proto.getParameter = function (p) {{
        if (p === 37445) return '{vendor}';
        if (p === 37446) return {renderer!r};
        return getParameter.apply(this, arguments);
      }};
    }};
    patchGL(window.WebGLRenderingContext && window.WebGLRenderingContext.prototype);
    patchGL(window.WebGL2RenderingContext && window.WebGL2RenderingContext.prototype);
  }} catch (e) {{}}
}})();"""


def available_stacks() -> dict[str, str]:
    """`doctor` 用：报告本机装了哪些内核。"""
    import importlib.util

    out: dict[str, str] = {}
    for module in ("camoufox", "patchright", "playwright"):
        out[module] = "ok" if importlib.util.find_spec(module) else "missing"
    return out

