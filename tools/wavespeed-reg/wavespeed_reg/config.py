"""Config: YAML + env(WAVESPEED_*) + CLI 覆盖，默认值即生产可用。

分四块：主配置、邮箱、人机校验、浏览器，外加独立的 `AntiBanConfig`（防封）。
`--anti-ban` / `--key-policy` 从命令行传进来时是 JSON 字符串，直接覆盖对应块。
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
DEFAULT_BASE = "https://wavespeed.ai"
DEFAULT_API_BASE = "https://llm.wavespeed.ai/v1"
DEFAULT_TASK_BASE = "https://api.wavespeed.ai/api/v3"


def normalize_proxy(raw: str) -> str:
    value = (raw or "").strip()
    if not value:
        return ""
    if "://" not in value:
        value = f"{_SCHEME_DEFAULT}://{value}"
    return value


def env_proxy() -> str:
    for key in ("WAVESPEED_PROXY", "HTTPS_PROXY", "https_proxy", "ALL_PROXY", "all_proxy"):
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
    """注册地址从哪来。域名邮箱（自有 catch-all）是首选，最耐用。"""

    provider: str = "imap"          # imap | mailtm | tempmail_lol | fixed
    domain: str = ""
    domains: list[str] = field(default_factory=list)
    address: str = ""               # provider=fixed：复用一个地址（plus 分号）
    prefix: str = ""                # imap：local part 前缀
    imap_host: str = ""
    imap_port: int = 993
    imap_user: str = ""
    imap_password: str = ""
    verify_timeout: float = 180.0
    verify_pattern: str = r"https?://[^\s\"'<>]+"


@dataclass(slots=True)
class CaptchaConfig:
    """Turnstile token（OAuth 回调 / accesskey 页可能带）。

    WaveSpeed 文档没提 CAPTCHA，但登录页有 "Preparing secure sign-in..." 的
    中间态，实际是否弹挑战取决于出口 IP 评分 —— 所以这里默认 `auto`：
    先用无 token，被拦再解题。
    """

    provider: str = "auto"          # auto | none | capsolver | 2captcha | browser | manual
    api_key: str = ""
    site_key: str = ""              # 空 = 运行时从页面抓
    action: str = "login"
    timeout: float = 150.0
    poll: float = 3.0


@dataclass(slots=True)
class BrowserConfig:
    """指纹浏览器（OAuth 与 /accesskey 只能靠真实浏览器）。"""

    stack: str = "auto"             # auto | camoufox | patchright | playwright
    headless: bool = True
    cdp: str = ""                   # 外部指纹浏览器 CDP，如 http://127.0.0.1:50325
    humanize: float = 1.0
    geoip: bool = True
    turnstile_timeout: float = 50.0
    screenshots: bool = True
    profile_dir: str = ""           # 空 = 每次全新上下文（推荐）
    oauth_wait: float = 120.0       # 等 OAuth 回调落地的上限
    accesskey_wait: float = 60.0    # 等 /accesskey 生成出明文的上限


@dataclass(slots=True)
class AntiBanConfig:
    """防封策略 —— 全部可调，默认值偏向"慢但稳"。"""

    # 域名邮箱
    mail_domains: list[str] = field(default_factory=list)
    domain_rpm: int = 3             # 单域名每分钟最多建几个账号
    domain_rph: int = 40            # 单域名每小时上限
    domain_health_floor: float = 0.25   # 健康分低于此值暂停该域名
    # 出口
    ip_concurrency: int = 1         # 同一出口 IP 同时只跑 1 个
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
            "oauth_unavailable": [0, 0],
            "transport": [3, 8],
            "no_api_key": [5, 10],
        }
    )

    @classmethod
    def from_dict(cls, raw: dict[str, Any] | None) -> "AntiBanConfig":
        cfg = cls()
        for key, value in (raw or {}).items():
            if not hasattr(cfg, key) or value is None:
                continue
            current = getattr(cfg, key)
            if isinstance(current, list) and isinstance(value, (list, tuple)):
                setattr(cfg, key, [int(v) for v in value][: len(current)] or current)
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
    base_url: str = DEFAULT_BASE
    api_base: str = DEFAULT_API_BASE
    task_base: str = DEFAULT_TASK_BASE
    engine: str = "auto"            # protocol | browser | auto | hybrid
    count: int = 1
    concurrency: int = 1            # OAuth 流程重，默认串行
    attempts: int = 3
    retry_rounds: int = 1
    delay: tuple[float, float] = (6.0, 14.0)
    timeout: float = 45.0
    language: str = "en-US"
    user_agent: str = ""
    verify_email: bool = True
    keys_per_account: int = 1
    key_name_template: str = "{local}-{date}"
    key_policy: dict[str, Any] = field(default_factory=dict)
    proxies: list[str] = field(default_factory=list)
    output_dir: Path = Path("out")
    db_path: Path | None = None
    session_file: str = ""
    mail: MailConfig = field(default_factory=MailConfig)
    captcha: CaptchaConfig = field(default_factory=CaptchaConfig)
    browser: BrowserConfig = field(default_factory=BrowserConfig)
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
        anti_raw = data.pop("anti_ban", None) or data.pop("antiban", None) or {}

        cfg = cls(
            base_url=str(data.get("base_url") or _env("WAVESPEED_BASE_URL", "WAVESPEED_PANEL_BASE") or DEFAULT_BASE),
            api_base=str(data.get("api_base") or _env("WAVESPEED_API_BASE") or DEFAULT_API_BASE),
            task_base=str(data.get("task_base") or _env("WAVESPEED_TASK_API_BASE") or DEFAULT_TASK_BASE),
            engine=str(data.get("engine") or _env("WAVESPEED_ENGINE") or "auto"),
            count=int(data.get("count", 1)),
            concurrency=int(data.get("concurrency", 1)),
            attempts=int(data.get("attempts", 3)),
            retry_rounds=int(data.get("retry_rounds", 1)),
            timeout=float(data.get("timeout", 45.0)),
            language=str(data.get("language", "en-US")),
            user_agent=str(data.get("user_agent", "") or ""),
            verify_email=bool(data.get("verify_email", True)),
            keys_per_account=max(1, int(data.get("keys_per_account", 1))),
            key_name_template=str(data.get("key_name_template", "{local}-{date}")),
            key_policy=dict(data.get("key_policy") or {}),
            output_dir=Path(str(data.get("output_dir", "out"))),
            session_file=str(data.get("session_file") or ""),
            push_url=str(data.get("push_url") or _env("WAVESPEED_GATEWAY_URL")),
            push_token=str(data.get("push_token") or _env("WAVESPEED_GATEWAY_TOKEN")),
            gateway_pool_file=str(data.get("gateway_pool_file") or ""),
        )

        delay = data.get("delay") or [6.0, 14.0]
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
        cfg.mail.imap_host = cfg.mail.imap_host or _env("WAVESPEED_IMAP_HOST")
        cfg.mail.imap_user = cfg.mail.imap_user or _env("WAVESPEED_IMAP_USER")
        cfg.mail.imap_password = cfg.mail.imap_password or _env("WAVESPEED_IMAP_PASSWORD")

        cfg.captcha = CaptchaConfig(
            provider=str(captcha_raw.get("provider") or _env("WAVESPEED_CAPTCHA_PROVIDER") or "auto"),
            api_key=str(captcha_raw.get("api_key") or _env("WAVESPEED_CAPTCHA_KEY")),
            site_key=str(captcha_raw.get("site_key") or _env("WAVESPEED_CAPTCHA_SITE_KEY")),
            action=str(captcha_raw.get("action") or "login"),
            timeout=float(captcha_raw.get("timeout", 150.0) or 150.0),
            poll=float(captcha_raw.get("poll", 3.0) or 3.0),
        )

        cfg.browser = BrowserConfig(
            stack=str(browser_raw.get("stack") or _env("WAVESPEED_BROWSER_STACK") or "auto"),
            headless=bool(browser_raw.get("headless", True)),
            cdp=str(browser_raw.get("cdp") or _env("WAVESPEED_CDP")),
            humanize=float(browser_raw.get("humanize", 1.0) or 1.0),
            geoip=bool(browser_raw.get("geoip", True)),
            turnstile_timeout=float(browser_raw.get("turnstile_timeout", 50.0) or 50.0),
            screenshots=bool(browser_raw.get("screenshots", True)),
            profile_dir=str(browser_raw.get("profile_dir") or ""),
            oauth_wait=float(browser_raw.get("oauth_wait", 120.0) or 120.0),
            accesskey_wait=float(browser_raw.get("accesskey_wait", 60.0) or 60.0),
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
                cfg.key_policy = json.loads(value) if isinstance(value, str) else dict(value or {})
            elif key == "session_file":
                cfg.session_file = str(value)
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

            return self.key_name_template.format(local=local, date=_time.strftime("%Y-%m-%d"))
        except (KeyError, IndexError):
            return local

    def describe(self) -> dict[str, Any]:
        return {
            "base_url": self.base_url,
            "api_base": self.api_base,
            "task_base": self.task_base,
            "engine": self.engine,
            "count": self.count,
            "concurrency": self.concurrency,
            "attempts": self.attempts,
            "retry_rounds": self.retry_rounds,
            "delay": list(self.delay),
            "verify_email": self.verify_email,
            "keys_per_account": self.keys_per_account,
            "proxies": len(self.proxies),
            "mail_provider": self.mail.provider,
            "mail_domains": self.mail.domains,
            "captcha": self.captcha.provider,
            "browser": {"stack": self.browser.stack, "headless": self.browser.headless, "cdp": bool(self.browser.cdp)},
            "anti_ban": self.anti_ban.as_dict(),
            "output_dir": str(self.output_dir),
            "db": str(self.db_path),
            "push": bool(self.push_url and self.push_token),
            "session_file": self.session_file or None,
        }
