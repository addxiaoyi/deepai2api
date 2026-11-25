"""指纹浏览器引擎：协议被拦时，用真实浏览器把同一套注册走完。

三条腿，按"指纹质量"从高到低自动选：

  1. Camoufox  —— Firefox 内核 + C++ 层指纹改写，每次启动一套自洽的
     canvas/WebGL/fonts/screen/时区/语言；Turnstile 托管挑战基本能自己过。
     https://github.com/daijro/camoufox
  2. Patchright —— Playwright 的源码级反检测分支（去掉 Runtime 域、隔离执行上下文），
     Chromium 指纹 + 真实 TLS/HTTP2。https://github.com/Kaliiiiiiiiii-Vinyzu/patchright
  3. Playwright Chromium —— 兜底；用 browserforge 风格的 UA/视口/时区对齐，
     并对 navigator.webdriver 之类做最小修补。https://github.com/daijro/browserforge

另外支持"外部指纹浏览器"：AdsPower / BitBrowser / Morelogin 这类工具都会开一个
本地 CDP 端口，`--cdp http://127.0.0.1:PORT` 直接接管它的 profile —— 指纹、代理、
登录态全用那边的，我们只负责填表、点按钮、读 key。

产出记录的形状和协议引擎完全一致，所以 store / 导出 / 号池不需要区分来源。
"""

from __future__ import annotations

import json
import random
import re
import time
from pathlib import Path
from typing import Any, Callable
from urllib.parse import urlparse

from .errors import (
    Blocked,
    CaptchaRequired,
    EmailRejected,
    EmailTaken,
    NoApiKey,
    SignupError,
    VerificationPending,
)

# ---- 页面选择器 -----------------------------------------------------------
#
# 面板是 shadcn/ui 表单：placeholder 和 autocomplete 比 id 稳定得多。
NAME_SELECTORS = ("input[autocomplete='name']", "input[placeholder='Ada Lovelace']")
EMAIL_SELECTORS = ("input[type='email']", "input[autocomplete='email']")
PASSWORD_SELECTORS = ("input[autocomplete='new-password']", "input[type='password']")
SUBMIT_TEXT = re.compile(r"create account|sign\s*up|create your account", re.I)
ERROR_SELECTOR = "[role='alert'], .text-destructive, [data-sonner-toast]"
TURNSTILE_IFRAME = "iframe[src*='challenges.cloudflare.com']"
TURNSTILE_INPUT = "input[name='cf-turnstile-response']"

FETCH_JS = """
async ({ path, method, body }) => {
  const init = { method, credentials: 'include' };
  if (body !== null && body !== undefined) {
    init.headers = { 'content-type': 'application/json' };
    init.body = JSON.stringify(body);
  }
  const response = await fetch(path, init);
  const text = await response.text();
  let json = null;
  try { json = text ? JSON.parse(text) : null; } catch (e) {}
  return { status: response.status, json, text };
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
    """没有任何可用的浏览器内核（缺依赖 / 缺二进制）。"""

    kind = "browser_unavailable"


class FingerprintBrowser:
    """一次注册 = 一个浏览上下文 = 一套指纹 + 一份 cookie。"""

    def __init__(
        self,
        *,
        base_url: str = "https://dash.allmodels.io",
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
        """接管外部指纹浏览器的 profile（AdsPower/BitBrowser/Morelogin…）。"""
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
            from camoufox.fingerprints import Screen
        except Exception as exc:  # noqa: BLE001 - 可选依赖
            self.log(f"camoufox 不可用（{exc.__class__.__name__}），换下一个内核")
            return False
        launch: dict[str, Any] = {
            "headless": self.headless,
            "humanize": 2.2 * max(0.2, self.humanize),
            "os": ["windows", "macos", "linux"],
            "locale": [self.locale],
            "block_webrtc": True,
            "fingerprint_preset": True,
            "screen": Screen(min_width=1280, min_height=720, max_width=1920, max_height=1080),
            "window": (1366, 768),
            "pin_cpu_cores": True,
            "enable_cache": True,
            "geoip": bool(self.geoip and self.proxy),
            # The warning exists because a preset OS/GPU different from the host
            # implies canvas noise; that is exactly what we asked for here.
            "i_know_what_im_doing": True,
        }
        if self.proxy:
            launch["proxy"] = _proxy_dict(self.proxy)
        try:
            self._camoufox = Camoufox(**launch)
            self._browser = self._camoufox.__enter__()
        except Exception as exc:  # noqa: BLE001 - 二进制没下 / 启动失败
            self.log(f"camoufox 启动失败（{exc}），换下一个内核")
            return False
        contexts = list(getattr(self._browser, "contexts", []) or [])
        self._context = contexts[0] if contexts else self._browser.new_context()
        self.page = self._context.new_page()
        self.stack = "camoufox"
        self.log("指纹内核：camoufox（Firefox + 指纹预设）")
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
            self._browser = self._pw.chromium.launch(**launch)
        except Exception as exc:  # noqa: BLE001 - 二进制缺失
            self.log(f"{flavour} 启动失败（{exc}）")
            try:
                self._pw.stop()
            except Exception:  # noqa: BLE001
                pass
            self._pw = None
            return False

        viewport = self.rng.choice(
            [{"width": 1366, "height": 768}, {"width": 1440, "height": 900}, {"width": 1536, "height": 864}]
        )
        self._context = self._browser.new_context(
            locale=self.locale,
            timezone_id=None,
            user_agent=(
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                f"(KHTML, like Gecko) Chrome/{self.rng.choice([131, 132, 133, 134])}.0.0.0 Safari/537.36"
            ),
            viewport=viewport,
            device_scale_factor=self.rng.choice([1, 1, 1.25]),
        )
        self._context.add_init_script(
            """(() => {
              Object.defineProperty(navigator, 'webdriver', { get: () => undefined });
              Object.defineProperty(navigator, 'languages', { get: () => ['en-US', 'en'] });
              Object.defineProperty(navigator, 'plugins', { get: () => [1, 2, 3, 4, 5] });
              const orig = window.chrome ?? {};
              window.chrome = { ...orig, runtime: orig.runtime ?? {} };
            })();"""
        )
        self.page = self._context.new_page()
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
            if getattr(self, "_camoufox", None) is not None:
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

    def _fill_first(self, selectors: tuple[str, ...], value: str, label: str) -> None:
        for selector in selectors:
            locator = self.page.locator(selector).first
            try:
                if locator.count() and locator.is_visible():
                    self._human_type(locator, value)
                    return
            except Exception:  # noqa: BLE001 - 选择器不匹配继续试下一个
                continue
        raise SignupError(f"注册表单里找不到 {label} 输入框")

    def _fetch(self, path: str, *, method: str = "GET", body: Any = None) -> dict[str, Any]:
        result = self.page.evaluate(FETCH_JS, {"path": path, "method": method, "body": body})
        status = int(result.get("status") or 0)
        if status >= 400:
            payload = result.get("json") or {}
            message = str(payload.get("message") or payload.get("error") or result.get("text") or "")[:300]
            if "captcha" in message.lower():
                raise CaptchaRequired(f"{path}: turnstile rejected the session ({message})")
            raise SignupError(f"{path}: HTTP {status} {message}")
        payload = result.get("json")
        return payload if isinstance(payload, dict) else {}

    def _page_error(self) -> str:
        try:
            node = self.page.locator(ERROR_SELECTOR).first
            if node.count():
                return re.sub(r"\s+", " ", node.inner_text(timeout=800) or "").strip()
        except Exception:  # noqa: BLE001
            pass
        return ""

    def _classify(self, message: str) -> None:
        lowered = (message or "").lower()
        if not lowered:
            return
        if "already" in lowered or "exists" in lowered:
            raise EmailTaken(message)
        if "invalid email" in lowered or "not allowed" in lowered or "disposable" in lowered:
            raise EmailRejected(message)
        if "captcha" in lowered or "verification failed" in lowered:
            raise CaptchaRequired(message)

    # ---------------------------------------------------------------- signup
    def open_signup(self) -> None:
        self.page.goto(f"{self.base}/signup", wait_until="domcontentloaded", timeout=int(self.timeout * 1000))
        if re.search(r"just a moment|checking your browser", (self.page.content() or "")[:3000], re.I):
            raise Blocked("signup 页被 Cloudflare 挑战挡住")
        self._shot(f"signup-{self.stack}")

    def fill_and_submit(self, *, name: str, email: str, password: str) -> None:
        self._fill_first(NAME_SELECTORS, name, "Name")
        self._fill_first(EMAIL_SELECTORS, email, "Email")
        passwords = self.page.locator(", ".join(PASSWORD_SELECTORS))
        count = passwords.count()
        if count < 2:
            raise SignupError("注册表单里密码/确认密码输入框不足")
        self._human_type(passwords.nth(0), password)
        self._human_type(passwords.nth(1), password)

        self._wait_turnstile()

        button = self.page.get_by_role("button", name=SUBMIT_TEXT).first
        if not button.count():
            raise SignupError("找不到 Create account 按钮")
        button.click()
        self._pause(400, 900)

    def _wait_turnstile(self) -> None:
        """托管挑战通常自己过；过不去就把复选框点一下，再不行交回上层。"""
        deadline = time.time() + self.turnstile_timeout
        clicked = False
        while time.time() < deadline:
            state = self.page.evaluate(TURNSTILE_STATE_JS)
            if state.get("token"):
                self.log("turnstile：拿到 token")
                return
            if state.get("widget") and not clicked:
                try:
                    frame = self.page.frame_locator(TURNSTILE_IFRAME).first
                    box = frame.locator("input[type='checkbox'], label").first
                    if box.count():
                        box.click(timeout=1500)
                        clicked = True
                        self.log("turnstile：点击了人机校验复选框")
                except Exception:  # noqa: BLE001 - 托管模式没有可点的元素
                    clicked = True
            self.page.wait_for_timeout(900)
        self.log("turnstile：等待超时，交给上层（解题器 / 协议回落）")

    def wait_signup_result(self, *, timeout: float = 60.0) -> str:
        """等注册结果：成功返回 'ok'，失败抛出对应错误。"""
        deadline = time.time() + timeout
        while time.time() < deadline:
            url = self.page.url or ""
            if "/verify-email" in url:
                return "ok"
            text = self.page.locator("body").inner_text(timeout=1500) or ""
            if "check your email" in text.lower():
                return "ok"
            message = self._page_error()
            if message:
                self._shot("signup-error")
                self._classify(message)
                raise SignupError(f"注册失败：{message}")
            self.page.wait_for_timeout(700)
        self._shot("signup-timeout")
        raise SignupError("注册提交后没有进入验证页（超时）")

    def verify(self, link: str) -> None:
        self.page.goto(link, wait_until="domcontentloaded", timeout=int(self.timeout * 1000))
        if "error=" in (self.page.url or ""):
            raise VerificationPending(f"验证链接不可用：{self.page.url}")

    def session(self) -> dict[str, Any]:
        payload = self._fetch("/api/auth/get-session")
        return payload if isinstance(payload, dict) else {}

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

    # ------------------------------------------------------------- composite
    def signup_flow(
        self,
        email: str,
        password: str,
        *,
        name: str = "",
        verify: bool = True,
        provider: Any = None,
        box: Any = None,
        verify_timeout: float = 180.0,
        key_name: str = "",
        keys: int = 1,
        log: Callable[[str], None] | None = None,
    ) -> dict[str, Any]:
        say = log or self.log
        self.open_signup()
        self.fill_and_submit(name=name or email.split("@")[0], email=email, password=password)
        self.wait_signup_result()

        email_verified: bool | None = None
        if verify and provider is not None and box is not None:
            link = provider.wait_for_link(box, timeout=verify_timeout, must_contain=("verify-email", "token="))
            if not link:
                raise VerificationPending(f"{email} 在 {verify_timeout:.0f}s 内没收到验证邮件")
            self.verify(link)
            say(f"邮箱已验证 {email}")

        session = self.session()
        user = (session or {}).get("user") or {}
        email_verified = user.get("emailVerified") if user else None

        overview = self._fetch("/api/me/overview")
        org = overview.get("organization") or {}
        projects = overview.get("projects") or []
        guardrails = overview.get("guardrails") or []
        project_id = next((p.get("id") for p in projects if p.get("isDefault")), None) or (
            projects[0].get("id") if projects else ""
        )
        guardrail_id = next((g.get("id") for g in guardrails if g.get("isDefault")), None) or (
            guardrails[0].get("id") if guardrails else ""
        )

        signup_key = (self._fetch("/api/me/signup-key") or {}).get("key") or ""
        api_key = str(signup_key)
        key_source = "signup-key" if api_key else ""
        if not api_key:
            created = self._fetch(
                "/api/keys",
                method="POST",
                body={
                    "displayName": key_name or f"{email.split('@')[0]}-{time.strftime('%Y-%m-%d')}",
                    "projectId": project_id or "",
                    "guardrailId": guardrail_id or "",
                    "environment": "live",
                },
            )
            api_key = str(created.get("key") or "")
            key_source = "created" if api_key else ""
        if not api_key:
            raise NoApiKey("浏览器注册完成，但拿不到面板 Key")

        extra_keys: list[dict[str, str]] = []
        for index in range(max(0, keys - 1)):
            try:
                created = self._fetch(
                    "/api/keys",
                    method="POST",
                    body={
                        "displayName": f"{key_name or email.split('@')[0]}-{index + 2}",
                        "projectId": project_id or "",
                        "guardrailId": guardrail_id or "",
                        "environment": "live",
                    },
                )
            except SignupError:
                break
            token = str(created.get("key") or "")
            if token:
                extra_keys.append({"key": token, "source": "created"})

        listed = (self._fetch("/api/keys") or {}).get("keys") or []
        record = next(
            (k for k in listed if api_key and str(api_key).startswith(str(k.get("keyPrefix") or "\0"))), None
        )
        self._shot(f"signed-{email.split('@')[0]}")

        return {
            "email": email,
            "password": password,
            "name": name,
            "api_key": api_key,
            "api_key_source": key_source,
            "extra_keys": extra_keys,
            "key_id": str((record or {}).get("id") or ""),
            "key_prefix": str((record or {}).get("keyPrefix") or ""),
            "key_last4": str((record or {}).get("last4") or ""),
            "project_id": project_id or "",
            "guardrail_id": guardrail_id or "",
            "organization_id": str(org.get("id") or ""),
            "organization_slug": str(org.get("slug") or ""),
            "user_id": str(user.get("id") or ""),
            "email_verified": email_verified,
            "cookies": self.cookies(),
            "browser": self.stack,
        }


def available_stacks() -> dict[str, str]:
    """`doctor` 用：报告本机装了哪些内核。"""
    import importlib.util

    out: dict[str, str] = {}
    for module, label in (
        ("camoufox", "camoufox"),
        ("patchright", "patchright"),
        ("playwright", "playwright"),
    ):
        out[label] = "ok" if importlib.util.find_spec(module) else "missing"
    return out
