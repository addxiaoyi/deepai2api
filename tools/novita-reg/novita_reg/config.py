"""Config: YAML + env(NOVITA_*) + CLI 覆盖。

四块主配置（主 / 邮箱 / 人机校验 / 浏览器）+ 两块策略（防封 / key 策略）。
`--anti-ban`、`--key-policy` 从命令行进来时是 JSON 字符串，直接覆盖对应块。
"""

from __future__ import annotations

import json
import os
from dataclasses import dataclass, field, fields
from pathlib import Path
from typing import Any

import yaml

DEFAULT_CONFIG_FILES = ("config.yaml", "config.example.yaml")

_SCHEME_DEFAULT = "http"
DEFAULT_PANEL = "https://novita.ai"
DEFAULT_PANEL_API = "https://api-server.novita.ai"
DEFAULT_API_BASE = "https://api.novita.ai"
DEFAULT_OPENAI_BASE = "https://api.novita.ai/openai"
DEFAULT_TURNSTILE_SITE_KEY = "0x4AAAAAAAaG28VfN_OxkED8"


def normalize_proxy(raw: str) -> str:
    value = (raw or "").strip()
    if not value:
        return ""
    if "://" not in value:
        value = f"{_SCHEME_DEFAULT}://{value}"
    return value


def env_proxy() -> str:
    """隐式代理来源。

    只认 `NOVITA_PROXY`；通用 `HTTPS_PROXY/ALL_PROXY` 需要显式开关
    （`NOVITA_USE_ENV_PROXY=1`）才会采用 —— 否则机器上一套公司代理会**静默**
    把所有注册流量带走，doctor 还会显示"站点不可达"，排查方向直接被带偏。
    """
    explicit = os.environ.get("NOVITA_PROXY", "").strip()
    if explicit:
        return normalize_proxy(explicit)
    if str(os.environ.get("NOVITA_USE_ENV_PROXY", "")).strip().lower() not in ("1", "true", "yes"):
        return ""
    for key in ("HTTPS_PROXY", "https_proxy", "ALL_PROXY", "all_proxy"):
        raw = os.environ.get(key, "").strip()
        if raw:
            return normalize_proxy(raw)
    return ""


def _env(*names: str, default: str = "") -> str:
    for name in names:
        value = os.environ.get(name, "").strip()
        if value:
            return value
    return default


@dataclass(slots=True)
class MailConfig:
    """注册地址从哪来。域名邮箱（自有 catch-all）是首选。"""

    provider: str = "imap"          # imap | domain | mailtm | tempmail_lol | fixed
    domain: str = ""
    domains: list[str] = field(default_factory=list)
    address: str = ""
    prefix: str = ""
    imap_host: str = ""
    imap_port: int = 993
    imap_user: str = ""
    imap_password: str = ""
    verify_timeout: float = 180.0
    verify_pattern: str = r"https?://[^\s\"'<>]+"


@dataclass(slots=True)
class CaptchaConfig:
    """Turnstile：sitekey 已反查得到，token 字段是 `cloudflareToken`。"""

    provider: str = "auto"          # auto | none | capsolver | 2captcha | browser | manual
    api_key: str = ""
    site_key: str = DEFAULT_TURNSTILE_SITE_KEY
    action: str = "signup"
    timeout: float = 150.0
    poll: float = 3.0


@dataclass(slots=True)
class BrowserConfig:
    """指纹浏览器（协议被拦时的第二条腿）。"""

    stack: str = "auto"             # auto | camoufox | patchright | playwright
    headless: bool = True
    cdp: str = ""
    humanize: float = 1.0
    geoip: bool = True
    turnstile_timeout: float = 50.0
    screenshots: bool = True
    profile_dir: str = ""
    signup_wait: float = 90.0
    verify_wait: float = 180.0
    keygen_wait: float = 60.0


@dataclass(slots=True)
class KeyPolicyConfig:
    """每个账号建几把 key、有效期多久、要不要放开 model-access。"""

    per_account: int = 1
    expire_time: str = "permanent"      # permanent | 90d | 30d | 24h
    name_template: str = "{local}-{date}"
    open_model_access: bool = False     # 建完 key 后 PUT 放开 model-access
    model_ids: list[str] = field(default_factory=list)

    def as_dict(self) -> dict[str, Any]:
        return {f.name: getattr(self, f.name) for f in fields(self)}


@dataclass(slots=True)
class AntiBanConfig:
    """防封策略 —— 默认值偏向"慢但稳"。"""

    # 域名邮箱
    mail_domains: list[str] = field(default_factory=list)
    domain_rpm: int = 3
    domain_rph: int = 40
    domain_health_floor: float = 0.25
    # 出口
    ip_concurrency: int = 1
    proxy_rotate: bool = True
    # 节奏
    jitter_ms: list[int] = field(default_factory=lambda: [1200, 4200])
    batch_pause: list[int] = field(default_factory=lambda: [0, 0])
    # 指纹
    geo_align: bool = True
    fresh_profile: bool = True
    # 会话
    reuse_session: bool = True
    session_ttl_hours: int = 72
    # 同设备多账号：每账号独立 profile 目录 + 独立噪声种子 + 清 storage
    isolate_profile: bool = True
    clear_storage_between: bool = True
    # 失败退避（秒），按 errors.kind 索引
    backoff: dict[str, list[int]] = field(
        default_factory=lambda: {
            "rate_limited": [20, 45],
            "blocked": [20, 40],
            "captcha_required": [15, 30],
            "verification_pending": [5, 10],
            "key_not_obtainable": [0, 0],
            "email_taken": [0, 0],
            "email_rejected": [0, 0],
            "verify_required": [0, 0],
            "quota_exhausted": [0, 0],
            "transport": [3, 8],
            "no_api_key": [5, 10],
            "password_rejected": [0, 0],
        }
    )

    #: 网关（src/provider-register.mjs 的 DEFAULT_ANTI_BAN）用 camelCase 下发，
    #: 本文件用 snake_case 存。没有这张表，网关传来的策略会因为 hasattr 失败被
    #: **静默丢弃** —— 面板显示"防封已就绪"，实际全跑默认值。这是最容易踩的整合坑。
    CAMEL_ALIASES = {
        "mailDomains": "mail_domains",
        "domainRpm": "domain_rpm",
        "domainRph": "domain_rph",
        "domainHealthFloor": "domain_health_floor",
        "ipConcurrency": "ip_concurrency",
        "proxyRotate": "proxy_rotate",
        "jitterMs": "jitter_ms",
        "cooldownAfterBatch": "batch_pause",
        "geoAlign": "geo_align",
        "freshProfile": "fresh_profile",
        "reuseSession": "reuse_session",
        "sessionTtlHours": "session_ttl_hours",
        "isolateProfile": "isolate_profile",
        "clearStorageBetween": "clear_storage_between",
        "keysPerAccount": "keys_per_account",
        "keyNameTemplate": "key_name_template",
        "backoff": "backoff",
    }

    @classmethod
    def from_dict(cls, raw: dict[str, Any] | None) -> "AntiBanConfig":
        cfg = cls()
        normalized: dict[str, Any] = {}
        for key, value in (raw or {}).items():
            target = cls.CAMEL_ALIASES.get(key)
            if target is None:
                if key in {f.name for f in cls.__dataclass_fields__.values()}:  # type: ignore[attr-defined]
                    target = key
                else:
                    # 通用 camelCase → snake_case 兜底（未来加字段不必改这张表）
                    snake = "".join(
                        f"_{ch.lower()}" if ch.isupper() else ch for ch in str(key)
                    ).lstrip("_")
                    target = snake
            normalized[target] = value
        for key, value in normalized.items():
            if not hasattr(cfg, key) or value is None:
                continue
            current = getattr(cfg, key)
            if isinstance(current, list) and isinstance(value, (list, tuple)):
                rows = list(value)
                # 数值窗（jitter_ms / batch_pause）转 int；字符串列表（mail_domains）原样保留
                if rows and all(isinstance(v, (int, float)) or str(v).strip().lstrip("-").isdigit() for v in rows):
                    rows = [int(v) for v in rows]
                else:
                    rows = [str(v).strip() for v in rows if str(v).strip()]
                setattr(cfg, key, rows[: len(current)] if current else rows)
            elif isinstance(current, list) and isinstance(value, (int, float)):
                # 网关把 cooldownAfterBatch 传成标量 0，别让它把 list 字段污染成 int
                setattr(cfg, key, [int(value), int(value)])
            elif isinstance(current, dict) and isinstance(value, dict):
                merged = dict(current)
                merged.update({str(k): [int(x) for x in v] for k, v in value.items() if isinstance(v, (list, tuple))})
                setattr(cfg, key, merged)
            elif isinstance(current, bool):
                setattr(cfg, key, bool(value))
            elif isinstance(current, int):
                setattr(cfg, key, int(value))
            elif isinstance(current, float):
                setattr(cfg, key, float(value))
            else:
                setattr(cfg, key, value)
        return cfg

    def backoff_for(self, kind: str) -> tuple[float, float]:
        pair = self.backoff.get(kind) or self.backoff.get("error") or [5, 15]
        return float(pair[0]), float(pair[1])

    def as_dict(self) -> dict[str, Any]:
        return {f.name: getattr(self, f.name) for f in fields(self)}


@dataclass(slots=True)
class Config:
    panel: str = DEFAULT_PANEL
    panel_api: str = DEFAULT_PANEL_API
    api_base: str = DEFAULT_API_BASE
    openai_base: str = DEFAULT_OPENAI_BASE
    engine: str = "auto"            # protocol | browser | auto
    count: int = 1
    concurrency: int = 2
    attempts: int = 3
    retry_rounds: int = 1
    delay: tuple[float, float] = (4.0, 9.0)
    timeout: float = 45.0
    language: str = "en-US"
    user_agent: str = ""
    verify_email: bool = True
    invite_code: str = ""
    proxies: list[str] = field(default_factory=list)
    output_dir: Path = Path("out")
    db_path: Path | None = None
    session_file: str = ""
    mail: MailConfig = field(default_factory=MailConfig)
    captcha: CaptchaConfig = field(default_factory=CaptchaConfig)
    browser: BrowserConfig = field(default_factory=BrowserConfig)
    key_policy: KeyPolicyConfig = field(default_factory=KeyPolicyConfig)
    anti_ban: AntiBanConfig = field(default_factory=AntiBanConfig)
    push_url: str = ""
    push_token: str = ""
    gateway_pool_file: str = ""

    # ---------------------------------------------------------------- loading
    @classmethod
    def load(cls, path: str | Path | None = None, overrides: dict[str, Any] | None = None) -> "Config":
        data: dict[str, Any] = {}
        chosen = Path(path) if path else cls._discover()
        if chosen and chosen.exists():
            data = yaml.safe_load(chosen.read_text(encoding="utf-8")) or {}
            data.setdefault("__source__", str(chosen))

        mail_raw = data.pop("mail", None) or {}
        captcha_raw = data.pop("captcha", None) or {}
        browser_raw = data.pop("browser", None) or {}
        key_raw = data.pop("key_policy", None) or {}
        anti_raw = data.pop("anti_ban", None) or data.pop("antiban", None) or {}

        cfg = cls(
            panel=str(data.get("panel") or _env("NOVITA_PANEL", "NOVITA_BASE_URL") or DEFAULT_PANEL),
            panel_api=str(data.get("panel_api") or _env("NOVITA_PANEL_API") or DEFAULT_PANEL_API),
            api_base=str(data.get("api_base") or _env("NOVITA_API_BASE") or DEFAULT_API_BASE),
            openai_base=str(data.get("openai_base") or _env("NOVITA_OPENAI_BASE") or DEFAULT_OPENAI_BASE),
            engine=str(data.get("engine") or _env("NOVITA_ENGINE") or "auto"),
            count=int(data.get("count", 1)),
            concurrency=int(data.get("concurrency", 2)),
            attempts=int(data.get("attempts", 3)),
            retry_rounds=int(data.get("retry_rounds", 1)),
            timeout=float(data.get("timeout", 45.0)),
            language=str(data.get("language", "en-US")),
            user_agent=str(data.get("user_agent", "") or ""),
            verify_email=bool(data.get("verify_email", True)),
            invite_code=str(data.get("invite_code", "") or ""),
            output_dir=Path(str(data.get("output_dir", "out"))),
            session_file=str(data.get("session_file") or ""),
            push_url=str(data.get("push_url") or _env("NOVITA_GATEWAY_URL")),
            push_token=str(data.get("push_token") or _env("NOVITA_GATEWAY_TOKEN")),
            gateway_pool_file=str(data.get("gateway_pool_file") or ""),
        )

        delay = data.get("delay") or [4.0, 9.0]
        if isinstance(delay, (int, float)):
            cfg.delay = (float(delay), float(delay))
        elif isinstance(delay, (list, tuple)) and len(delay) == 2:
            cfg.delay = (float(delay[0]), float(delay[1]))

        proxies = data.get("proxies") or []
        if isinstance(proxies, str):
            proxies = [proxies]
        cfg.proxies = [p for p in (normalize_proxy(str(x)) for x in proxies) if p]

        cfg.mail = MailConfig(
            provider=str(mail_raw.get("provider", "imap")),
            domain=str(mail_raw.get("domain", "") or ""),
            domains=[str(d).strip().lower() for d in (mail_raw.get("domains") or []) if str(d).strip()],
            address=str(mail_raw.get("address", "") or ""),
            prefix=str(mail_raw.get("prefix", "") or ""),
            imap_host=str(mail_raw.get("imap_host", "") or ""),
            imap_port=int(mail_raw.get("imap_port", 993) or 993),
            imap_user=str(mail_raw.get("imap_user", "") or ""),
            imap_password=str(mail_raw.get("imap_password", "") or ""),
            verify_timeout=float(mail_raw.get("verify_timeout", 180.0) or 180.0),
            verify_pattern=str(mail_raw.get("verify_pattern", MailConfig.verify_pattern)),
        )
        cfg.mail.imap_host = cfg.mail.imap_host or _env("NOVITA_IMAP_HOST")
        cfg.mail.imap_user = cfg.mail.imap_user or _env("NOVITA_IMAP_USER")
        cfg.mail.imap_password = cfg.mail.imap_password or _env("NOVITA_IMAP_PASSWORD")

        cfg.captcha = CaptchaConfig(
            provider=str(captcha_raw.get("provider") or _env("NOVITA_CAPTCHA_PROVIDER") or "auto"),
            api_key=str(captcha_raw.get("api_key") or _env("NOVITA_CAPTCHA_KEY")),
            site_key=str(captcha_raw.get("site_key") or _env("NOVITA_CAPTCHA_SITE_KEY") or DEFAULT_TURNSTILE_SITE_KEY),
            action=str(captcha_raw.get("action") or "signup"),
            timeout=float(captcha_raw.get("timeout", 150.0) or 150.0),
            poll=float(captcha_raw.get("poll", 3.0) or 3.0),
        )

        cfg.browser = BrowserConfig(
            stack=str(browser_raw.get("stack") or _env("NOVITA_BROWSER_STACK") or "auto"),
            headless=bool(browser_raw.get("headless", True)),
            cdp=str(browser_raw.get("cdp") or _env("NOVITA_CDP")),
            humanize=float(browser_raw.get("humanize", 1.0) or 1.0),
            geoip=bool(browser_raw.get("geoip", True)),
            turnstile_timeout=float(browser_raw.get("turnstile_timeout", 50.0) or 50.0),
            screenshots=bool(browser_raw.get("screenshots", True)),
            profile_dir=str(browser_raw.get("profile_dir") or ""),
            signup_wait=float(browser_raw.get("signup_wait", 90.0) or 90.0),
            verify_wait=float(browser_raw.get("verify_wait", 180.0) or 180.0),
            keygen_wait=float(browser_raw.get("keygen_wait", 60.0) or 60.0),
        )

        cfg.key_policy = KeyPolicyConfig(
            per_account=max(1, int(key_raw.get("per_account", data.get("keys_per_account", 1)) or 1)),
            expire_time=str(key_raw.get("expire_time", "permanent") or "permanent"),
            name_template=str(key_raw.get("name_template", data.get("key_name_template", "{local}-{date}"))),
            open_model_access=bool(key_raw.get("open_model_access", False)),
            model_ids=[str(m) for m in (key_raw.get("model_ids") or []) if str(m).strip()],
        )

        cfg.anti_ban = AntiBanConfig.from_dict(anti_raw)

        for key, value in (overrides or {}).items():
            if value is None:
                continue
            if key == "mail_provider":
                cfg.mail.provider = str(value)
            elif key == "mail_domain":
                cfg.mail.domain = str(value)
            elif key == "mail_domains":
                rows = [str(x).strip().lower() for x in (value or [])]
                cfg.mail.domains = [x for x in rows if x]
                cfg.anti_ban.mail_domains = list(cfg.mail.domains)
            elif key == "mail_address":
                cfg.mail.address = str(value)
            elif key == "captcha_provider":
                cfg.captcha.provider = str(value)
            elif key == "captcha_key":
                cfg.captcha.api_key = str(value)
            elif key == "browser_stack":
                cfg.browser.stack = str(value)
            elif key == "headless":
                cfg.browser.headless = bool(value)
            elif key == "cdp":
                cfg.browser.cdp = str(value)
            elif key == "humanize":
                cfg.browser.humanize = float(value)
            elif key == "anti_ban":
                merged = cfg.anti_ban.as_dict()
                if isinstance(value, str):
                    value = json.loads(value or "{}")
                merged.update(value or {})
                cfg.anti_ban = AntiBanConfig.from_dict(merged)
            elif key == "key_policy":
                raw = json.loads(value) if isinstance(value, str) else dict(value or {})
                for k, v in raw.items():
                    if hasattr(cfg.key_policy, k):
                        setattr(cfg.key_policy, k, v)
            elif key == "keys_per_account":
                cfg.key_policy.per_account = max(1, int(value))
            elif key == "key_name_template":
                cfg.key_policy.name_template = str(value)
            elif key == "session_file":
                cfg.session_file = str(value)
            elif key in ("base_url", "panel"):
                cfg.panel = str(value)
            elif key in ("out_dir", "output_dir"):
                cfg.output_dir = Path(str(value))
            elif key == "api_base":
                # 网关传的是 catalog 的 apiBase（`https://api.novita.ai/openai`），
                # 而 worker 的 api_base 是原生根（`https://api.novita.ai`）。
                # 不归一的话会拼出 /openai/openai/v1/... —— 直接把反代打成 404。
                cfg.api_base, cfg.openai_base = _split_api_base(str(value), cfg.openai_base)
            elif key == "openai_base":
                cfg.openai_base = str(value)
            elif hasattr(cfg, key):
                setattr(cfg, key, value)

        if not cfg.proxies:
            fallback = env_proxy()
            if fallback:
                cfg.proxies = [fallback]
        if not cfg.anti_ban.mail_domains:
            cfg.anti_ban.mail_domains = list(cfg.mail.domains)
        cfg.output_dir = Path(cfg.output_dir)
        cfg.db_path = Path(cfg.db_path) if cfg.db_path else cfg.output_dir / "accounts.sqlite3"
        return cfg

    @staticmethod
    def _discover() -> Path | None:
        for name in DEFAULT_CONFIG_FILES:
            candidate = Path(name)
            if candidate.exists():
                return candidate
        here = Path(__file__).resolve().parent.parent
        for name in DEFAULT_CONFIG_FILES:
            candidate = here / name
            if candidate.exists():
                return candidate
        return None

    # -------------------------------------------------------------- accessors
    def proxy_for(self, index: int) -> str:
        if not self.proxies:
            return ""
        return self.proxies[index % len(self.proxies)]

    def key_name(self, email: str) -> str:
        local = email.split("@")[0]
        try:
            import time as _time

            return self.key_policy.name_template.format(local=local, date=_time.strftime("%Y-%m-%d"))
        except (KeyError, IndexError):
            return local

    def describe(self) -> dict[str, Any]:
        return {
            "panel": self.panel,
            "panel_api": self.panel_api,
            "api_base": self.api_base,
            "openai_base": self.openai_base,
            "engine": self.engine,
            "count": self.count,
            "concurrency": self.concurrency,
            "attempts": self.attempts,
            "retry_rounds": self.retry_rounds,
            "delay": list(self.delay),
            "verify_email": self.verify_email,
            "proxies": len(self.proxies),
            "mail_provider": self.mail.provider,
            "mail_domains": self.mail.domains,
            "captcha": self.captcha.provider,
            "captcha_site_key": self.captcha.site_key,
            "browser": {"stack": self.browser.stack, "headless": self.browser.headless, "cdp": bool(self.browser.cdp)},
            "key_policy": self.key_policy.as_dict(),
            "anti_ban": self.anti_ban.as_dict(),
            "output_dir": str(self.output_dir),
            "db": str(self.db_path),
            "push": bool(self.push_url and self.push_token),
        }


# --------------------------------------------------------------------- helpers


def _split_api_base(value: str, fallback_openai: str) -> tuple[str, str]:
    """把网关传来的 apiBase 归一成 (原生根, OpenAI 前缀)。

    网关的 catalog 里 `apiBase = https://api.novita.ai/openai`（反代层直接拼
    `/v1/chat/completions`），而 worker 需要原生根 `https://api.novita.ai`
    （再各自拼 `/openai/v1/...`）。两种写法都接受，避免重复一层前缀。
    """
    raw = (value or "").strip().rstrip("/")
    if not raw:
        return DEFAULT_API_BASE, fallback_openai
    if raw.endswith("/openai"):
        return raw[: -len("/openai")], raw
    return raw, fallback_openai
