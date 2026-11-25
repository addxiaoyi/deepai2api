"""运行配置 —— 字段名与网关侧 `DEFAULT_ANTI_BAN`（src/provider-register.mjs）一致。

网关把 `--anti-ban '<json>'` 原样透传下来，这里解析成 dataclass；缺项用默认值补齐，
所以任意一侧更新都不会炸。
"""

from __future__ import annotations

import json
import os
from dataclasses import dataclass, field, asdict
from typing import Any

# ---------------------------------------------------------------- 站点常量
# 全部来自线上实测（取证表见 docs/ZO-POOL.md）；原始抓取物在 tools/zo-reg/_re/。
PANEL_BASE = "https://www.zo.computer"      # Next.js 面板：魔法链、登录态、Turnstile
API_BASE = "https://api.zo.computer"        # FastAPI：/zo/ask、/models、/signup、/api-keys

# 面板侧（cookie 域 .zo.computer）
EMAIL_LOGIN_PATH = "/api/email-login/request"
CONFIRM_PATH = "/api/email-login/confirm"
VERIFY_PAGE_PATH = "/email-login/verify"
LOGIN_STATE_PATH = "/api/login-state"
LOGOUT_PATH = "/logout"
SETTINGS_APIS: tuple[str, ...] = (
    "/api/settings",
    "/api/settings/advanced",
    "/api/settings/tokens",
)

# API 侧（Bearer access token）
SIGNUP_AVAILABLE_PATH = "/signup/{handle}/available"
SIGNUP_PATH = "/signup/"
API_KEYS_PATH = "/api-keys/"
MODELS_PATH = "/models/available"
CATALOG_PATH = "/models/catalog"
PERSONAS_PATH = "/personas/available"
ASK_PATH = "/zo/ask"
MCP_PATH = "/mcp"
BYOK_PATH = "/byok/"
SETTINGS_API_PATH = "/settings/"
CAPABILITIES_PATH = "/capabilities/"

DEFAULT_MODEL = "zo:openai/gpt-6.2-astra"

# Turnstile sitekey（chunk 28feu2t9fugjg.js: s="0x4AAAAAADTRT4Uvmz3G7YH5"）
TURNSTILE_SITE_KEY = "0x4AAAAAADTRT4Uvmz3G7YH5"

# 邮件里魔法链的样子（tracker 包装在其次，先直匹配）
VERIFY_LINK_RE = r"https://(?:www\.)?zo\.computer/email-login/verify\?token=[A-Za-z0-9_\-%\.]{8,}"
VERIFY_REL_LINK_RE = r"/email-login/verify\?token=[A-Za-z0-9_\-%\.]{8,}"

SUBJECT_HINT = ("sign up", "sign-up", "sign in", "sign-in", "magic", "login", "log in", "zo computer", "verify")

# handle 生成：站点允许字母数字与短横，长度 3-30（先探测占用，占用就换）
HANDLE_RE = r"^[a-z0-9][a-z0-9-]{1,28}[a-z0-9]$"


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
    # ---- 节奏 ----
    jitter_ms: list[int] = field(default_factory=lambda: [1200, 4200])
    cooldown_after_batch: int = 0
    # ---- 指纹 ----
    geo_align: bool = True             # 时区 / 语言跟随出口 IP
    fresh_profile: bool = True         # 每账号全新 profile
    # ---- 会话 ----
    reuse_session: bool = True         # 复用 cookie jar，避免重复过魔法链
    session_ttl_hours: int = 72
    # ---- 失败退避（秒）----
    backoff: dict[str, list[int]] = field(default_factory=lambda: {
        "rate_limited": [20, 45],
        "blocked": [20, 40],
        "captcha_required": [15, 30],
        "verification_pending": [5, 10],
        "handle_taken": [0, 0],
        "no_token": [5, 10],
        "no_api_key": [0, 0],
        "email_taken": [0, 0],
        "email_rejected": [0, 0],
        "transport": [3, 8],
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
        known = set(cls.__dataclass_fields__)
        kwargs: dict[str, Any] = {}
        for key, value in data.items():
            snake = _to_snake(key)
            if snake in known and value is not None:
                kwargs[snake] = value
        return cls(**kwargs)

    def to_json(self) -> dict[str, Any]:
        return asdict(self)


def _to_snake(name: str) -> str:
    out: list[str] = []
    for ch in str(name):
        if ch.isupper():
            out.append("_")
            out.append(ch.lower())
        else:
            out.append(ch)
    return "".join(out).lstrip("_")


@dataclass
class RunConfig:
    count: int = 1
    concurrency: int = 2
    engine: str = "auto"              # auto | protocol | browser | hybrid
    browser_stack: str = "auto"       # auto | camoufox | patchright | playwright
    cdp: str = ""                     # 接管 AdsPower / BitBrowser 的 CDP
    headful: bool = False
    humanize: float = 0.0             # 0 = 不刻意放慢；>0 是倍率

    mail_provider: str = "imap"       # imap | domain | mailtm | fixed
    mail_domains: list[str] = field(default_factory=list)
    email: str = ""                   # fixed 时直接用这个地址
    captcha: str = "none"             # none | capsolver | 2captcha | browser | manual | auto
    captcha_key: str = ""             # 解题平台 key；manual 时就是 token

    base_url: str = PANEL_BASE        # 面板（Next.js）
    api_base: str = API_BASE          # FastAPI
    handle_prefix: str = ""           # 工作区 handle 前缀（空则按 persona 生成）
    keys_per_account: int = 1         # 每个账号建几个 access token
    token_name: str = "gateway"
    verify_email: bool = True
    verify_timeout: float = 240.0     # 等魔法链邮件的上限（秒）
    model: str = DEFAULT_MODEL
    probe: bool = True                # 建完 token 后打一次 /models/available
    mcp_probe: bool = False           # 顺带 tools/list 验证 MCP 可用
    desktop_ua: bool = False          # 用桌面端 UA（Zo/…）+ 深链，走 desktop 分支

    push_url: str = ""
    push_token: str = ""
    session_file: str = ""
    out_dir: str = "out"
    json_events: bool = False
    timeout: float = 45.0
    retries: int = 3

    anti_ban: AntiBanConfig = field(default_factory=AntiBanConfig)
    key_policy: dict[str, Any] = field(default_factory=dict)
    proxies: list[str] = field(default_factory=list)

    @classmethod
    def from_namespace(cls, ns: Any) -> "RunConfig":
        anti = AntiBanConfig.from_json(getattr(ns, "anti_ban", None))
        domains = [d.strip().lower() for d in (getattr(ns, "mail_domains", None) or []) if d and d.strip()]
        if domains:
            anti.mail_domains = domains
        key_policy: dict[str, Any] = {}
        raw_policy = getattr(ns, "key_policy", None)
        if raw_policy:
            try:
                parsed = json.loads(raw_policy)
                key_policy = parsed if isinstance(parsed, dict) else {}
            except json.JSONDecodeError:
                key_policy = {}
        return cls(
            count=max(1, int(getattr(ns, "count", 1) or 1)),
            concurrency=max(1, int(getattr(ns, "concurrency", 2) or 2)),
            engine=str(getattr(ns, "engine", "auto") or "auto"),
            browser_stack=str(getattr(ns, "browser_stack", "auto") or "auto"),
            cdp=str(getattr(ns, "cdp", "") or ""),
            headful=bool(getattr(ns, "headful", False)),
            humanize=float(getattr(ns, "humanize", 0) or 0),
            mail_provider=str(getattr(ns, "mail_provider", "imap") or "imap"),
            mail_domains=domains,
            email=str(getattr(ns, "email", "") or ""),
            captcha=str(getattr(ns, "captcha", "none") or "none"),
            captcha_key=str(getattr(ns, "captcha_key", "") or os.environ.get("ZO_CAPTCHA_KEY", "")),
            base_url=str(getattr(ns, "base_url", PANEL_BASE) or PANEL_BASE).rstrip("/"),
            api_base=str(getattr(ns, "api_base", API_BASE) or API_BASE).rstrip("/"),
            handle_prefix=str(getattr(ns, "handle_prefix", "") or ""),
            keys_per_account=max(1, int(getattr(ns, "keys_per_account", 1) or 1)),
            token_name=str(getattr(ns, "token_name", "gateway") or "gateway"),
            verify_email=not bool(getattr(ns, "no_verify", False)),
            verify_timeout=float(getattr(ns, "verify_timeout", 240.0) or 240.0),
            model=str(getattr(ns, "model", DEFAULT_MODEL) or DEFAULT_MODEL),
            probe=not bool(getattr(ns, "no_probe", False)),
            mcp_probe=bool(getattr(ns, "mcp_probe", False)),
            desktop_ua=bool(getattr(ns, "desktop_ua", False)),
            push_url=str(getattr(ns, "push_url", "") or ""),
            push_token=str(getattr(ns, "push_token", "") or ""),
            session_file=str(getattr(ns, "session_file", "") or ""),
            out_dir=str(getattr(ns, "out_dir", "out") or "out"),
            json_events=bool(getattr(ns, "json", False)),
            timeout=float(getattr(ns, "timeout", 45.0) or 45.0),
            retries=int(getattr(ns, "retries", 3) or 3),
            anti_ban=anti,
            key_policy=key_policy,
            proxies=[p.strip() for p in (getattr(ns, "proxy", None) or []) if p and p.strip()],
        )

    def delay_ms(self) -> tuple[int, int]:
        low, high = (self.anti_ban.jitter_ms + [1200, 4200])[:2]
        low, high = int(low), int(high)
        if self.humanize > 0:
            low = int(low * self.humanize)
            high = int(high * self.humanize)
        return (max(0, min(low, high)), max(0, max(low, high)))