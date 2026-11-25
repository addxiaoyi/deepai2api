"""Config loading for the gab.ai worker: YAML + env + CLI overrides."""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml

DEFAULT_CONFIG_FILES = ("config.yaml", "config.example.yaml")

_SCHEME_DEFAULT = "http"
DEFAULT_BASE = "https://gab.ai"


def normalize_proxy(raw: str) -> str:
    value = raw.strip()
    if not value:
        return ""
    if "://" not in value:
        value = f"{_SCHEME_DEFAULT}://{value}"
    return value


def env_proxy() -> str:
    for key in ("HTTPS_PROXY", "https_proxy", "ALL_PROXY", "all_proxy"):
        raw = os.environ.get(key, "").strip()
        if raw:
            return normalize_proxy(raw)
    return ""


@dataclass(slots=True)
class MailConfig:
    provider: str = "tempmail_lol"  # tempmail_lol | mailtm | imap | fixed
    domain: str = ""
    domains: list[str] = field(default_factory=list)
    address: str = ""               # provider=fixed
    prefix: str = ""                # imap: local part prefix
    imap_host: str = ""
    imap_port: int = 993
    imap_user: str = ""
    imap_password: str = ""
    verify_timeout: float = 180.0
    verify_pattern: str = r"https?://[^\s\"'<>]+"


@dataclass(slots=True)
class CaptchaConfig:
    provider: str = "none"          # none | browser | manual
    api_key: str = ""              # manual: token
    action: str = "signup"
    timeout: float = 120.0


@dataclass(slots=True)
class BrowserConfig:
    stack: str = "auto"          # auto | camoufox | playwright
    headless: bool = True
    cdp: str = ""
    humanize: float = 1.0
    geoip: bool = True
    turnstile_timeout: float = 50.0
    screenshots: bool = True
    profile_dir: str = ""


@dataclass(slots=True)
class Config:
    base_url: str = DEFAULT_BASE
    engine: str = "auto"            # protocol | browser | auto
    count: int = 1
    concurrency: int = 2
    attempts: int = 3
    retry_rounds: int = 1
    delay: tuple[float, float] = (3.0, 7.0)
    timeout: float = 45.0
    language: str = "en-US"
    user_agent: str = ""

    # control plane
    verify_email: bool = False
    create_api_key: bool = True

    proxies: list[str] = field(default_factory=list)
    proxy_rotate: bool = True

    output_dir: Path = Path("out")
    db_path: Path | None = None

    mail: MailConfig = field(default_factory=MailConfig)
    captcha: CaptchaConfig = field(default_factory=CaptchaConfig)
    browser: BrowserConfig = field(default_factory=BrowserConfig)

    push_url: str = ""
    push_token: str = ""

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

        base = cls()
        cfg = cls(
            base_url=str(data.get("base_url") or os.environ.get("GABAI_BASE_URL") or base.base_url),
            engine=str(data.get("engine") or os.environ.get("GABAI_ENGINE") or base.engine),
            count=int(data.get("count", base.count)),
            concurrency=int(data.get("concurrency", base.concurrency)),
            attempts=int(data.get("attempts", base.attempts)),
            retry_rounds=int(data.get("retry_rounds", base.retry_rounds)),
            timeout=float(data.get("timeout", base.timeout)),
            language=str(data.get("language", base.language)),
            user_agent=str(data.get("user_agent", "") or ""),
            verify_email=bool(data.get("verify_email", base.verify_email)),
            create_api_key=bool(data.get("create_api_key", base.create_api_key)),
            proxy_rotate=bool(data.get("proxy_rotate", base.proxy_rotate)),
            output_dir=Path(str(data.get("output_dir", "out"))),
            push_url=str(data.get("push_url") or os.environ.get("GABAI_GATEWAY_URL") or ""),
            push_token=str(data.get("push_token") or os.environ.get("GABAI_GATEWAY_TOKEN") or ""),
        )

        delay = data.get("delay") or list(base.delay)
        if isinstance(delay, (int, float)):
            cfg.delay = (float(delay), float(delay))
        elif isinstance(delay, (list, tuple)) and len(delay) == 2:
            cfg.delay = (float(delay[0]), float(delay[1]))

        proxies = data.get("proxies") or []
        if isinstance(proxies, str):
            proxies = [proxies]
        cfg.proxies = [p for p in (normalize_proxy(str(x)) for x in proxies) if p]

        cfg.mail = MailConfig(
            provider=str(mail_raw.get("provider", cfg.mail.provider)),
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
        cfg.mail.imap_host = cfg.mail.imap_host or os.environ.get("GABAI_IMAP_HOST", "")
        cfg.mail.imap_user = cfg.mail.imap_user or os.environ.get("GABAI_IMAP_USER", "")
        cfg.mail.imap_password = cfg.mail.imap_password or os.environ.get("GABAI_IMAP_PASSWORD", "")

        cfg.captcha = CaptchaConfig(
            provider=str(captcha_raw.get("provider") or os.environ.get("GABAI_CAPTCHA_PROVIDER") or cfg.captcha.provider),
            api_key=str(captcha_raw.get("api_key") or os.environ.get("GABAI_CAPTCHA_KEY") or ""),
            action=str(captcha_raw.get("action") or "signup"),
            timeout=float(captcha_raw.get("timeout", 120.0) or 120.0),
        )

        cfg.browser = BrowserConfig(
            stack=str(browser_raw.get("stack") or os.environ.get("GABAI_BROWSER_STACK") or "auto"),
            headless=bool(browser_raw.get("headless", True)),
            cdp=str(browser_raw.get("cdp") or os.environ.get("GABAI_CDP") or ""),
            humanize=float(browser_raw.get("humanize", 1.0) or 1.0),
            geoip=bool(browser_raw.get("geoip", True)),
            turnstile_timeout=float(browser_raw.get("turnstile_timeout", 50.0) or 50.0),
            screenshots=bool(browser_raw.get("screenshots", True)),
            profile_dir=str(browser_raw.get("profile_dir") or ""),
        )

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
            elif hasattr(cfg, key):
                setattr(cfg, key, value)

        if not cfg.proxies:
            fallback = env_proxy()
            if fallback:
                cfg.proxies = [fallback]

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

    def proxy_for(self, index: int) -> str:
        if not self.proxies:
            return ""
        return self.proxies[index % len(self.proxies)]

    def describe(self) -> dict[str, Any]:
        return {
            "base_url": self.base_url,
            "engine": self.engine,
            "count": self.count,
            "concurrency": self.concurrency,
            "attempts": self.attempts,
            "retry_rounds": self.retry_rounds,
            "delay": self.delay,
            "verify_email": self.verify_email,
            "create_api_key": self.create_api_key,
            "proxies": len(self.proxies),
            "mail_provider": self.mail.provider,
            "mail_domains": self.mail.domains,
            "captcha": self.captcha.provider,
            "browser": {"stack": self.browser.stack, "headless": self.browser.headless, "cdp": bool(self.browser.cdp)},
            "output_dir": str(self.output_dir),
            "db": str(self.db_path),
            "push": bool(self.push_url and self.push_token),
        }
