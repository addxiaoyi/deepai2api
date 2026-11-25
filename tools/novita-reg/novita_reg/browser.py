"""指纹浏览器引擎：邮箱注册表单 + Turnstile + Key 管理页建 key 读明文。

Novita 有完整的邮箱注册表单，所以浏览器腿要做的比 WaveSpeed 多：

    /user/register
      → 点 "Create with an Email"（默认折叠）
      → 填 Email / Password / 邀请码（可选）
      → 勾 "I agree to the Terms..."（必勾，否则按钮点了没反应）
      → Turnstile（托管挑战）
      → 点 "Create an account"
      → 成功提示 "Activation email sent"
    邮件里的激活链接 → 打开
    /settings/key-management
      → Create API Key → 命名 → 选有效期 → 确认
      → 读一次性明文

四条内核，按指纹质量从高到低自动选：camoufox / patchright / playwright / 外部 CDP。

"同设备多账号"是 Novita 明确会风控的点（官方 FAQ：检测到同设备多账号会自动撤销
voucher），所以每个账号必须：
  * 独立 profile 目录（`isolate_profile`，profile_dir 下按 email hash 建子目录）；
  * 独立 canvas/WebGL 噪声种子（来自 persona.profile_seed）；
  * 上下文之间清 storage（cookies / localStorage / IndexedDB 由新 context 保证）。
"""

from __future__ import annotations

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
    KeyNotObtainable,
    NoApiKey,
    SignupError,
    VerificationPending,
)

# ---- 选择器 ---------------------------------------------------------------
#
# 面板是 Next.js + antd 风格；按钮文案有 i18n 包裹，所以用正则 + role 组合，
# 并对每个字段给多个候选，避免一次改版就全线失效。
EMAIL_LABEL = re.compile(r"email", re.I)
PASSWORD_LABEL = re.compile(r"password", re.I)
INVITE_LABEL = re.compile(r"invitation\s*code", re.I)
SHOW_EMAIL_TEXTS = re.compile(r"create\s*with\s*an?\s*email|sign\s*up.*email|use\s*email", re.I)
SUBMIT_TEXTS = re.compile(r"create\s*an?\s*account|sign\s*up|create\s*account", re.I)
LOGIN_TEXTS = re.compile(r"log\s*in|sign\s*in", re.I)
AGREE_TEXTS = re.compile(r"i\s*agree|terms\s*of\s*service", re.I)
CREATE_KEY_TEXTS = re.compile(r"create\s*(api\s*)?key|new\s*key|add\s*key", re.I)
CONFIRM_TEXTS = re.compile(r"confirm|create|ok|submit|save", re.I)

EMAIL_INPUTS = (
    "input[name='email']",
    "input[type='email']",
    "input[placeholder*='email' i]",
    "input[id='email']",
)
PASSWORD_INPUTS = (
    "input[name='password']",
    "input[type='password']",
    "input[placeholder*='password' i]",
    "input[id='password']",
)
INVITE_INPUTS = (
    "input[name='invite-code']",
    "input[name*='invite' i]",
    "input[placeholder*='invitation' i]",
)
KEY_NAME_INPUTS = (
    "input[placeholder*='name' i]",
    "input[name*='name' i]",
    "input[id*='name' i]",
)
ERROR_SELECTOR = "[role='alert'], .ant-form-item-explain-error, .text-red-500, [data-sonner-toast], .error"

TURNSTILE_IFRAME = "iframe[src*='challenges.cloudflare.com']"
KEY_RE = re.compile(r"\bsk_[A-Za-z0-9_-]{16,120}\b")

BLOCKED_MARKERS = ("just a moment", "checking your browser", "verify you are human", "attention required")


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
    """一次注册 = 一个独立 profile = 一套指纹 + 一份 cookie。"""

    def __init__(
        self,
        *,
        panel: str = "https://novita.ai",
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
        isolate_profile: bool = True,
        clear_storage: bool = True,
        signup_wait: float = 90.0,
        verify_wait: float = 180.0,
        keygen_wait: float = 60.0,
        log: Callable[[str], None] | None = None,
    ) -> None:
        self.panel = panel.rstrip("/")
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
        self.profile_root = Path(profile_dir) if profile_dir else None
        self.isolate_profile = isolate_profile
        self.clear_storage = clear_storage
        self.signup_wait = signup_wait
        self.verify_wait = verify_wait
        self.keygen_wait = keygen_wait
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

    def _profile_for(self, email: str) -> str:
        """同一台机器上给每个账号一个独立 profile 目录。"""
        if not self.profile_root or not self.isolate_profile:
            return str(self.profile_root or "")
        tag = re.sub(r"[^a-zA-Z0-9._-]", "_", email or "anon")[:48]
        path = self.profile_root / tag
        path.mkdir(parents=True, exist_ok=True)
        return str(path)

    def start(self, *, profile_key: str = "") -> None:
        self._profile = self._profile_for(profile_key)
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
            from camoufox.fingerprints import Screen
        except Exception as exc:  # noqa: BLE001 - 可选依赖
            self.log(f"camoufox 不可用（{exc.__class__.__name__}），换下一个内核")
            return False
        fp = self.fingerprint
        launch: dict[str, Any] = {
            "headless": self.headless,
            "humanize": 2.2 * max(0.2, self.humanize),
            "os": ["windows", "macos"],
            "locale": [fp.get("locale") or self.locale],
            "block_webrtc": True,
            "fingerprint_preset": True,
            "screen": Screen(min_width=1280, min_height=720, max_width=1920, max_height=1080),
            "window": (1366, 768),
            "pin_cpu_cores": True,
            "enable_cache": True,
            "geoip": bool(self.geoip and self.proxy),
            "i_know_what_im_doing": True,
        }
        if fp.get("timezone"):
            launch["timezone"] = fp["timezone"]
        elif self.timezone_id:
            launch["timezone"] = self.timezone_id
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
            if self._profile:
                self._context = self._pw.chromium.launch_persistent_context(self._profile, **launch)
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
            self._context = self._browser.new_context(
                locale=fp.get("locale") or self.locale,
                timezone_id=fp.get("timezone") or self.timezone_id or None,
                user_agent=fp.get("user_agent")
                or (
                    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                    f"(KHTML, like Gecko) Chrome/{self.rng.choice([131, 132, 133, 134])}.0.0.0 Safari/537.36"
                ),
                viewport=fp.get("viewport") or {"width": 1440, "height": 900},
                screen=fp.get("screen") or {"width": 1920, "height": 1080},
                device_scale_factor=fp.get("device_scale_factor") or 1,
                extra_http_headers={"Accept-Language": fp.get("accept_language") or "en-US,en;q=0.9"},
            )
            self._context.add_init_script(_patch_js(fp))
        self.page = self._context.pages[0] if self._context.pages else self._context.new_page()
        if self.clear_storage:
            try:
                self.page.add_init_script(
                    "try { localStorage.clear(); sessionStorage.clear(); } catch (e) {}"
                )
            except Exception:  # noqa: BLE001
                pass
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

    def _classify(self, message: str) -> None:
        lowered = (message or "").lower()
        if not lowered:
            return
        if "already" in lowered or "exist" in lowered:
            raise EmailTaken(message)
        if "invalid email" in lowered or "not allowed" in lowered or "disposable" in lowered:
            raise EmailRejected(message)
        if "password" in lowered:
            raise SignupError(message)
        if "captcha" in lowered or "verification failed" in lowered:
            raise CaptchaRequired(message)

    def _fill_first(self, selectors: tuple[str, ...], value: str, label: str) -> bool:
        for selector in selectors:
            try:
                locator = self.page.locator(selector).first
                if locator.count() and locator.is_visible():
                    self._human_type(locator, value)
                    return True
            except Exception:  # noqa: BLE001
                continue
        self.log(f"找不到 {label} 输入框（选择器都没命中）")
        return False

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

    # ---------------------------------------------------------------- 注册
    def open_register(self) -> None:
        self.page.goto(f"{self.panel}/user/register", wait_until="domcontentloaded", timeout=int(self.timeout * 1000))
        text = self._body_text().lower()
        if any(marker in text for marker in BLOCKED_MARKERS):
            raise Blocked("注册页被 Cloudflare 挑战挡住")
        self._shot(f"register-{self.stack}")

    def _reveal_email_form(self) -> None:
        """表单默认折叠在 "Create with an Email" 按钮后面。"""
        if self.page.locator(", ".join(EMAIL_INPUTS)).count():
            return
        for getter in (
            lambda: self.page.get_by_role("button", name=SHOW_EMAIL_TEXTS).first,
            lambda: self.page.locator("button:has-text('Email')").first,
        ):
            try:
                button = getter()
                if button.count():
                    button.click(timeout=5000)
                    self._pause(250, 600)
                    return
            except Exception:  # noqa: BLE001
                continue
        self.log("没找到 'Create with an Email' 按钮，假设表单已展开")

    def fill_register(self, *, email: str, password: str, invite_code: str = "") -> None:
        self._reveal_email_form()
        if not self._fill_first(EMAIL_INPUTS, email, "email"):
            raise SignupError("注册表单里找不到邮箱输入框")
        if not self._fill_first(PASSWORD_INPUTS, password, "password"):
            raise SignupError("注册表单里找不到密码输入框")
        if invite_code:
            self._fill_first(INVITE_INPUTS, invite_code, "invite code")

        # 协议勾选框必勾，否则提交按钮点了无反应
        try:
            checkbox = self.page.get_by_role("checkbox").first
            if checkbox.count() and not checkbox.is_checked():
                checkbox.click(timeout=4000)
                self._pause(150, 400)
        except Exception:  # noqa: BLE001
            self.log("没找到协议勾选框")
        self._wait_turnstile()

    def submit_register(self) -> None:
        button = None
        for getter in (
            lambda: self.page.get_by_role("button", name=SUBMIT_TEXTS).first,
            lambda: self.page.locator("button:has-text('Create an account')").first,
        ):
            try:
                candidate = getter()
                if candidate.count():
                    button = candidate
                    break
            except Exception:  # noqa: BLE001
                continue
        if button is None:
            raise SignupError("找不到 'Create an account' 按钮")
        button.click()
        self._pause(600, 1400)

    def _wait_turnstile(self) -> str:
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
                    frame = self.page.frame_locator(TURNSTILE_IFRAME).first
                    box = frame.locator("input[type='checkbox'], label").first
                    if box.count():
                        box.click(timeout=1500)
                        clicked = True
                        self.log("turnstile：点击了人机校验复选框")
                except Exception:  # noqa: BLE001
                    clicked = True
            self.page.wait_for_timeout(900)
        self.log("turnstile：等待超时，交给上层")
        return ""

    def wait_register_result(self, timeout: float | None = None) -> str:
        """等注册结果：成功返回 'ok'，失败抛对应错误。"""
        deadline = time.time() + (timeout or self.signup_wait)
        while time.time() < deadline:
            text = self._body_text()
            lowered = text.lower()
            if "activation email sent" in lowered or "check your email" in lowered:
                self._shot("register-ok")
                return "ok"
            if "/user/login" in (self.page.url or ""):
                return "ok"
            message = self._page_error()
            if message:
                self._shot("register-error")
                self._classify(message)
                raise SignupError(f"注册失败：{message}")
            self.page.wait_for_timeout(900)
        self._shot("register-timeout")
        raise SignupError("提交后既没看到成功提示，也没看到错误（超时）")

    def verify(self, link: str) -> None:
        self.page.goto(link, wait_until="domcontentloaded", timeout=int(self.timeout * 1000))
        text = self._body_text().lower()
        if "expired" in text or "invalid" in text:
            raise VerificationPending(f"激活链接不可用：{self.page.url}")
        self._pause(400, 900)

    # ------------------------------------------------------------ 建 key
    def create_key(self, *, name: str, expire_label: str = "Permanent") -> str:
        """在 Key 管理页建一把 key 并读明文。"""
        self.page.goto(f"{self.panel}/settings/key-management", wait_until="domcontentloaded", timeout=int(self.timeout * 1000))
        if "/user/login" in (self.page.url or ""):
            raise NoApiKey("会话失效：key 管理页跳回登录")
        self._shot(f"keys-{self.stack}")

        before = set(KEY_RE.findall(self._body_text()))

        clicked = False
        for getter in (
            lambda: self.page.get_by_role("button", name=CREATE_KEY_TEXTS).first,
            lambda: self.page.locator("button:has-text('Create API Key'), button:has-text('Create Key')").first,
        ):
            try:
                button = getter()
                if button.count():
                    button.click(timeout=6000)
                    clicked = True
                    self._pause(400, 900)
                    break
            except Exception:  # noqa: BLE001
                continue
        if not clicked:
            raise NoApiKey("Key 管理页上找不到 'Create API Key' 按钮")

        # 弹窗：填名字 → 选有效期 → 确认
        self._fill_first(KEY_NAME_INPUTS, name, "key name")
        try:
            select = self.page.locator(".ant-select, select").first
            if select.count():
                select.click(timeout=3000)
                self._pause(200, 500)
                option = self.page.get_by_text(re.compile(expire_label, re.I)).first
                if option.count():
                    option.click(timeout=3000)
        except Exception:  # noqa: BLE001 - 默认值也能用
            pass
        try:
            confirm = self.page.get_by_role("button", name=CONFIRM_TEXTS).last
            if confirm.count():
                confirm.click(timeout=5000)
                self._pause(400, 900)
        except Exception:  # noqa: BLE001
            pass

        deadline = time.time() + self.keygen_wait
        while time.time() < deadline:
            found = [k for k in KEY_RE.findall(self._body_text()) if k not in before]
            if found:
                key = max(found, key=len)
                self._shot(f"keys-created-{self.stack}")
                return key
            try:
                copy_btn = self.page.get_by_role("button", name=re.compile(r"copy", re.I)).first
                if copy_btn.count():
                    copy_btn.click(timeout=2000)
                    self._pause(200, 400)
                    clip = self.page.evaluate(
                        "async () => { try { return await navigator.clipboard.readText(); } catch (e) { return ''; } }"
                    )
                    if clip and KEY_RE.fullmatch(str(clip).strip()):
                        return str(clip).strip()
            except Exception:  # noqa: BLE001
                pass
            self.page.wait_for_timeout(900)

        self._shot("keys-timeout")
        raise KeyNotObtainable(f"建 key 后 {self.keygen_wait:.0f}s 内没读到 sk_ 明文")

    # ------------------------------------------------------------- 复合流程
    def signup_flow(
        self,
        email: str,
        password: str,
        *,
        invite_code: str = "",
        verify: bool = True,
        provider: Any = None,
        box: Any = None,
        verify_timeout: float = 180.0,
        key_name: str = "",
        keys: int = 1,
        expire_label: str = "Permanent",
        log: Callable[[str], None] | None = None,
    ) -> dict[str, Any]:
        say = log or self.log
        self.open_register()
        self.fill_register(email=email, password=password, invite_code=invite_code)
        self.submit_register()
        self.wait_register_result()

        email_verified: bool | None = None
        if verify and provider is not None and box is not None:
            link = provider.wait_for_link(
                box,
                timeout=verify_timeout,
                must_contain=("verify", "activat", "token", "confirm"),
            )
            if not link:
                raise VerificationPending(f"{email} 在 {verify_timeout:.0f}s 内没收到激活邮件")
            self.verify(link)
            email_verified = True
            say(f"邮箱已验证 {email}")

        api_key = self.create_key(name=key_name or f"{email.split('@')[0]}-{int(time.time())}", expire_label=expire_label)
        extra_keys: list[dict[str, str]] = []
        for index in range(max(0, keys - 1)):
            try:
                more = self.create_key(
                    name=f"{key_name or email.split('@')[0]}-{index + 2}",
                    expire_label=expire_label,
                )
            except SignupError:
                break
            extra_keys.append({"key": more, "source": "created"})

        return {
            "email": email,
            "password": password,
            "name": email.split("@")[0],
            "api_key": api_key,
            "api_key_source": "key-management-ui",
            "extra_keys": extra_keys,
            "key_id": "",
            "key_prefix": api_key[:8],
            "key_last4": api_key[-4:],
            "expire_time": expire_label,
            "email_verified": email_verified,
            "cookies": self.cookies(),
            "browser": self.stack,
        }


def _patch_js(fp: dict[str, Any]) -> str:
    """最小反检测 patch + 指纹对齐（Playwright 兜底路径）。"""
    cores = int(fp.get("hardware_concurrency") or 8)
    memory = int(fp.get("device_memory") or 8)
    vendor = str(fp.get("webgl_vendor") or "Google Inc. (NVIDIA)")
    renderer = str(fp.get("webgl_renderer") or "")
    locale = str(fp.get("locale") or "en-US")
    return f"""(() => {{
  Object.defineProperty(navigator, 'webdriver', {{ get: () => undefined }});
  Object.defineProperty(navigator, 'hardwareConcurrency', {{ get: () => {cores} }});
  Object.defineProperty(navigator, 'deviceMemory', {{ get: () => {memory} }});
  Object.defineProperty(navigator, 'plugins', {{ get: () => [1, 2, 3, 4, 5] }});
  Object.defineProperty(navigator, 'languages', {{ get: () => ['{locale}', 'en'] }});
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
