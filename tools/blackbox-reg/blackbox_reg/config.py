"""配置 —— 字段名与网关侧 `DEFAULT_ANTI_BAN`（src/provider-register.mjs）一一对应。

网关会把 `--anti-ban '<json>'`、`--key-policy '<json>'` 原样透传下来；
这里解析成 dataclass，缺项用默认值补齐，两边任何一侧新增字段都不会炸。

站点常量全部来自线上实测（证据见 docs/BLACKBOX-POOL.md 取证表）。
"""

from __future__ import annotations

import json
import os
from dataclasses import dataclass, field, asdict
from typing import Any

# ---------------------------------------------------------------- 站点常量
APP_BASE = "https://app.blackbox.ai"
CONSOLE_BASE = "https://console.blackbox.ai"
# OpenAI 兼容推理入口（实测 /chat/completions 与 /v1/chat/completions 都返回
# {"error":{"message":"blackbox.Error: Invalid API key","type":"auth_error"}}）
API_BASE = "https://enterprise.blackbox.ai"
DOCS_BASE = "https://docs.blackbox.ai"

# Auth.js（NextAuth v5）路径
CSRF_PATH = "/api/auth/csrf"
PROVIDERS_PATH = "/api/auth/providers"
SIGNIN_PATH = "/api/auth/signin/google"
CALLBACK_PATH = "/api/auth/callback/google"
SESSION_PATH = "/api/auth/session"
CHALLENGE_PATH = "/api/auth/session-challenge"

COOKIE_PREFIX = "__Secure-authjs"
SESSION_COOKIE = f"{COOKIE_PREFIX}.session-token"
CALLBACK_COOKIE = f"{COOKIE_PREFIX}.callback-url"
STATE_COOKIE = f"{COOKIE_PREFIX}.state"
PKCE_COOKIE = f"{COOKIE_PREFIX}.pkce.code_verifier"

# 从 302 Location 读出来的常量（app 自己的 Google OAuth client）
GOOGLE_CLIENT_ID = "985058387028-iaa60lr5e586kvhjp085qcojv3jv2p7h.apps.googleusercontent.com"
GOOGLE_SCOPE = "openid profile email"
GOOGLE_AUTH_BASE = "https://accounts.google.com/o/oauth2/v2/auth"

# 面板 Key 接口没有公开文档：候选探测，命中即缓存（--keys-path 可覆盖）
KEY_PATHS = [
    "/api/keys",
    "/api/v1/keys",
    "/api/user/api-keys",
    "/api/user/keys",
    "/api/agent-api/keys",
    "/api/dashboard/keys",
    "/api/me/keys",
    "/api/api-keys",
    "/api/settings/api-keys",
    "/api/v1/api-keys",
    "/api/account/api-keys",
]

DEFAULT_MODEL = "blackboxai/blackbox-pro"
# 上游没有 /models 接口，这是描述符里的白名单（与 src/provider-catalog.mjs 对齐）
KNOWN_MODELS = [
    "blackboxai/blackbox-pro",
    "blackboxai/blackbox-ai",
    "blackboxai/blackbox-search",
    "blackboxai/openai/gpt-4o",
    "blackboxai/openai/gpt-4o-mini",
    "blackboxai/anthropic/claude-sonnet-4",
    "blackboxai/google/gemini-2.5-pro",
]

# 邮箱里可能出现的链接/验证码
VERIFY_LINK_RE = r"https://app\.blackbox\.ai/[^\s\"'<>]*(?:verify|confirm|magic)[^\s\"'<>]*"
OTP_CODE_RE = r"(?<!\d)(\d{6})(?!\d)"


@dataclass
class AntiBanConfig:
    """防封策略。网关侧同名默认值见 src/provider-register.mjs。"""

    # ---- 域名邮箱 ----
    mail_domains: list[str] = field(default_factory=list)
    domain_rpm: int = 3                # 单域名每分钟最多建几个
    domain_rph: int = 40               # 单域名每小时上限
    domain_health_floor: float = 0.25  # 健康分低于此值暂停该域名
    # ---- 出口 ----
    ip_concurrency: int = 1            # 同一出口同时只跑 1 个注册
    proxy_rotate: bool = True
    proxy_sticky_per_identity: bool = True  # 同一 Google 身份全程同一个出口（Google 最忌中途换 IP）
    # ---- 节奏 ----
    jitter_ms: list[int] = field(default_factory=lambda: [1200, 4200])
    oauth_pause_ms: list[int] = field(default_factory=lambda: [800, 2400])  # 授权页停留
    cooldown_after_batch: int = 0
    # ---- 指纹 ----
    geo_align: bool = True             # 时区/语言跟随出口 IP
    fresh_profile: bool = True         # 每账号全新 profile
    ua_consistency: bool = True        # UA / sec-ch-ua / 时区 自洽
    # ---- 会话 ----
    reuse_session: bool = True         # 复用 cookie jar，避免重复过 OAuth
    session_ttl_hours: int = 72
    # ---- Key ----
    key_create_rpm: int = 2            # 单账号每分钟最多建几个 Key（面板有频控）
    probe_after_key: bool = True       # 建完 Key 打一次 chat 验证可用
    # ---- 失败退避（秒）----
    backoff: dict[str, list[int]] = field(default_factory=lambda: {
        "rate_limited": [20, 45],
        "blocked": [20, 40],
        "captcha_required": [15, 30],
        "verification_pending": [5, 10],
        "session_expired": [0, 0],
        "oauth_denied": [10, 25],
        "plan_required": [0, 0],
        "key_not_obtainable": [0, 0],
        "no_api_key": [0, 0],
        "email_taken": [0, 0],
        "email_rejected": [0, 0],
        "transport": [3, 8],
        "upstream": [5, 12],
    })

    @classmethod
    def from_json(cls, raw: str | None) -> "AntiBanConfig":
        if not raw:
            return cls()
        try:
            data = json.loads(raw)
        except json.JSONDecodeError:
            return cls()
        if not isinstance(data, dict):
            return cls()
        known = {f for f in cls.__dataclass_fields__}
        kwargs: dict[str, Any] = {}
        for key, value in data.items():
            snake = to_snake(key)
            if snake in known and value is not None:
                kwargs[snake] = value
        return cls(**kwargs)

    def to_json(self) -> dict[str, Any]:
        return asdict(self)


def to_snake(name: str) -> str:
    out = []
    for ch in str(name):
        if ch.isupper():
            out.append("_")
            out.append(ch.lower())
        else:
            out.append(ch)
    return "".join(out).lstrip("_")


@dataclass
class GoogleIdentity:
    """一个 Google 身份 = 一个 Blackbox 账号的"钥匙"。

    Blackbox 只认 Google 登录，所以注册机真正池化的是 Google 身份：
      email / password 必填；totp_secret 有就自动算 6 位码；
      也可以只给 cookies（已经登录过的 Google 会话，最稳的一条腿）。
    """

    email: str = ""
    password: str = ""
    totp_secret: str = ""
    cookies: list[dict] = field(default_factory=list)
    note: str = ""

    @property
    def usable(self) -> bool:
        return bool(self.email) and bool(self.password or self.cookies)


@dataclass
class RunConfig:
    count: int = 1
    concurrency: int = 2
    engine: str = "auto"              # auto | protocol | browser | hybrid | console
    browser_stack: str = "auto"       # auto | camoufox | patchright | playwright
    cdp: str = ""                     # 接管 AdsPower / BitBrowser / Morelogin 的 CDP
    headful: bool = False
    humanize: float = 0.0             # 0 = 不刻意放慢；>0 是倍率

    mail_provider: str = "domain"     # mailtm | imap | domain | fixed
    mail_domains: list[str] = field(default_factory=list)
    email: str = ""                   # fixed 时直接用这个地址
    captcha: str = "none"             # none | auto | capsolver | 2captcha | browser | manual
    captcha_key: str = ""

    base_url: str = APP_BASE
    api_base: str = API_BASE
    console_url: str = CONSOLE_BASE
    # 指纹浏览器（camoufox 首选）
    geo_align: bool = True            # 时区/语言/经纬度跟随出口 IP（camoufox geoip=True）
    disable_coop: bool = True         # 关掉 COOP，才能点 cross-origin iframe 里的 Turnstile
    profile_dir: str = ""             # camoufox persistent_context 的身份档目录（留空=每次全新）
    storage_state: str = ""           # playwright storage_state 落盘路径（复用登录态）
    keys_path: str = ""               # 覆盖面板 Key 接口路径（探测失败时人工指定）
    keys_method: str = "GET"          # 列 Key 的方法（有些面板只给 POST /keys/list）
    keys_create_path: str = ""        # 建 Key 路径
    keys_create_body: str = ""        # 建 Key 请求体 JSON 模板
    model: str = DEFAULT_MODEL
    keys_per_account: int = 1
    verify_email: bool = True
    verify_timeout: float = 180.0

    # Google 身份来源
    google_pool: str = ""             # JSONL 文件：{"email","password","totp_secret"?,"cookies"?}
    google_cookies: str = ""          # 已登录 Google 的 cookies JSON（单身份用）
    session_file: str = ""            # 已登录 app.blackbox.ai 的 cookies JSON（最快的一条腿）
    authorization_code: str = ""      # 手工抓到的 Google 授权码（一次性）

    push_url: str = ""
    push_token: str = ""
    out_dir: str = "out"
    json_events: bool = False
    timeout: float = 45.0
    retries: int = 3
    probe: bool = True

    anti_ban: AntiBanConfig = field(default_factory=AntiBanConfig)
    key_policy: dict[str, Any] = field(default_factory=dict)
    proxies: list[str] = field(default_factory=list)

    @classmethod
    def from_namespace(cls, ns: Any) -> "RunConfig":
        anti = AntiBanConfig.from_json(getattr(ns, "anti_ban", None))
        domains = [d.strip().lower() for d in (getattr(ns, "mail_domains", None) or []) if d and d.strip()]
        if domains:
            anti.mail_domains = domains
        policy: dict[str, Any] = {}
        raw_policy = getattr(ns, "key_policy", None)
        if raw_policy:
            try:
                parsed = json.loads(raw_policy)
                policy = parsed if isinstance(parsed, dict) else {}
            except json.JSONDecodeError:
                policy = {}
        return cls(
            count=max(1, int(getattr(ns, "count", 1) or 1)),
            concurrency=max(1, int(getattr(ns, "concurrency", 2) or 2)),
            engine=str(getattr(ns, "engine", "auto") or "auto"),
            browser_stack=str(getattr(ns, "browser_stack", "auto") or "auto"),
            cdp=str(getattr(ns, "cdp", "") or ""),
            headful=bool(getattr(ns, "headful", False)),
            humanize=float(getattr(ns, "humanize", 0) or 0),
            mail_provider=str(getattr(ns, "mail_provider", "domain") or "domain"),
            mail_domains=domains,
            email=str(getattr(ns, "email", "") or ""),
            captcha=str(getattr(ns, "captcha", "none") or "none"),
            captcha_key=str(getattr(ns, "captcha_key", "") or os.environ.get("BLACKBOX_CAPTCHA_KEY", "")),
            base_url=str(getattr(ns, "base_url", APP_BASE) or APP_BASE).rstrip("/"),
            api_base=str(getattr(ns, "api_base", API_BASE) or API_BASE).rstrip("/"),
            console_url=str(getattr(ns, "console_url", CONSOLE_BASE) or CONSOLE_BASE).rstrip("/"),
            geo_align=bool(getattr(ns, "geo_align", anti.geo_align)),
            disable_coop=not bool(getattr(ns, "no_disable_coop", False)),
            profile_dir=str(getattr(ns, "profile_dir", "") or ""),
            storage_state=str(getattr(ns, "storage_state", "") or ""),
            keys_path=str(getattr(ns, "keys_path", "") or ""),
            keys_method=str(getattr(ns, "keys_method", "GET") or "GET").upper(),
            keys_create_path=str(getattr(ns, "keys_create_path", "") or ""),
            keys_create_body=str(getattr(ns, "keys_create_body", "") or ""),
            model=str(getattr(ns, "model", DEFAULT_MODEL) or DEFAULT_MODEL),
            keys_per_account=max(1, int(getattr(ns, "keys_per_account", 1) or 1)),
            verify_email=not bool(getattr(ns, "no_verify", False)),
            verify_timeout=float(getattr(ns, "verify_timeout", 180.0) or 180.0),
            google_pool=str(getattr(ns, "google_pool", "") or os.environ.get("BLACKBOX_GOOGLE_POOL", "")),
            google_cookies=str(getattr(ns, "google_cookies", "") or os.environ.get("BLACKBOX_GOOGLE_COOKIES", "")),
            session_file=str(getattr(ns, "session_file", "") or ""),
            authorization_code=str(getattr(ns, "authorization_code", "") or ""),
            push_url=str(getattr(ns, "push_url", "") or ""),
            push_token=str(getattr(ns, "push_token", "") or ""),
            out_dir=str(getattr(ns, "out_dir", "out") or "out"),
            json_events=bool(getattr(ns, "json", False)),
            timeout=float(getattr(ns, "timeout", 45.0) or 45.0),
            retries=int(getattr(ns, "retries", 3) or 3),
            probe=not bool(getattr(ns, "no_probe", False)),
            anti_ban=anti,
            key_policy=policy,
            proxies=[p.strip() for p in (getattr(ns, "proxy", None) or []) if p and p.strip()],
        )

    def describe(self) -> dict[str, Any]:
        return {
            "engine": self.engine,
            "count": self.count,
            "concurrency": self.concurrency,
            "base_url": self.base_url,
            "api_base": self.api_base,
            "mail_provider": self.mail_provider,
            "mail_domains": self.anti_ban.mail_domains or self.mail_domains,
            "captcha": self.captcha,
            "browser_stack": self.browser_stack,
            "geo_align": self.geo_align,
            "disable_coop": self.disable_coop,
            "profile_dir": bool(self.profile_dir),
            "storage_state": bool(self.storage_state),
            "model": self.model,
            "proxies": len(self.proxies),
            "google_pool": bool(self.google_pool),
            "session_file": bool(self.session_file),
            "push": bool(self.push_url and self.push_token),
        }
