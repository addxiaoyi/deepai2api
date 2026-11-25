"""运行配置 —— 字段名与网关侧 `DEFAULT_ANTI_BAN`（src/provider-register.mjs）一致。

网关会把 `--anti-ban '<json>'` 原样透传下来，这里解析成 dataclass；
缺项用这里的默认值补齐，所以两边任何一侧更新都不会炸。
"""

from __future__ import annotations

import json
import os
from dataclasses import dataclass, field, asdict
from typing import Any

# ---------------------------------------------------------------- 站点常量
# 全部来自线上实测（docs/MOONAI-POOL.md 的取证表），不是猜的。
AUTH_BASE = "https://auth.mooonai.com"
CORE_BASE = "https://core.mooonai.com"
CONSOLE_BASE = "https://dashboard.mooonai.com"

SIGNUP_PATH = "/auth/signup/"
SIGNIN_PATH = "/auth/signin/"
VERIFY_PATH = "/auth/verify-email/?token="
TOKEN_PATH = "/api/v1/auth/token"
REFRESH_PATH = "/api/v1/auth/refresh"
USERINFO_PATH = "/api/v1/auth/userinfo"
ORGS_PATH = "/api/v1/organizations/"
ORG_CREATE_PATH = "/api/v1/organizations"
API_KEYS_PATH = "/api/v1/organizations/{org}/api-keys"
LLM_PROXY_PATH = "/api/v1/services/llm-proxy/chat/completions"

# 上游只认这 6 个模型（拿别的会 400 并回显白名单）
ALLOWED_MODELS = ["gpt-4.1", "gpt-4o", "gpt-4o-mini", "gpt-5-nano", "o3-mini", "o4-mini"]
DEFAULT_MODEL = "gpt-4o-mini"

VERIFY_LINK_RE = r"https://auth\.mooonai\.com/auth/verify-email/\?token=[0-9a-fA-F-]{36}"


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
    reuse_session: bool = True         # 复用 cookie jar，避免重复过 OAuth
    session_ttl_hours: int = 72
    # ---- 失败退避（秒）----
    backoff: dict[str, list[int]] = field(default_factory=lambda: {
        "rate_limited": [20, 45],
        "blocked": [20, 40],
        "captcha_required": [15, 30],
        "verification_pending": [5, 10],
        "key_not_obtainable": [0, 0],
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
        known = {f for f in cls.__dataclass_fields__}
        kwargs: dict[str, Any] = {}
        for key, value in data.items():
            snake = _to_snake(key)
            if snake in known and value is not None:
                kwargs[snake] = value
        return cls(**kwargs)

    def to_json(self) -> dict[str, Any]:
        return asdict(self)


def _to_snake(name: str) -> str:
    out = []
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

    mail_provider: str = "imap"       # mailtm | imap | domain | fixed
    mail_domains: list[str] = field(default_factory=list)
    email: str = ""                   # fixed 时直接用这个地址
    captcha: str = "none"             # moonai 目前不需要人机校验，保留钩子
    captcha_key: str = ""

    base_url: str = AUTH_BASE
    core_url: str = CORE_BASE
    api_base: str = CORE_BASE
    keys_per_account: int = 1
    verify_email: bool = True
    model: str = DEFAULT_MODEL
    probe: bool = True                # 建完 key 后打一次 llm-proxy 验证可用

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
        key_policy = {}
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
            captcha_key=str(getattr(ns, "captcha_key", "") or os.environ.get("MOONAI_CAPTCHA_KEY", "")),
            base_url=str(getattr(ns, "base_url", AUTH_BASE) or AUTH_BASE).rstrip("/"),
            core_url=str(getattr(ns, "core_url", CORE_BASE) or CORE_BASE).rstrip("/"),
            api_base=str(getattr(ns, "api_base", CORE_BASE) or CORE_BASE).rstrip("/"),
            keys_per_account=max(1, int(getattr(ns, "keys_per_account", 1) or 1)),
            verify_email=not bool(getattr(ns, "no_verify", False)),
            model=str(getattr(ns, "model", DEFAULT_MODEL) or DEFAULT_MODEL),
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
