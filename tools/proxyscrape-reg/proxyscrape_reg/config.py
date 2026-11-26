"""Config loading: YAML + env + CLI overrides."""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml

DEFAULT_CONFIG_FILES = ("config.yaml", "config.example.yaml")
DEFAULT_BASE = "https://dashboard.proxyscrape.com"
_SCHEME_DEFAULT = "http"


def normalize_proxy(raw: str) -> str:
    value = (raw or "").strip()
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
    provider: str = "imap"          # imap | fixed | mailtm | tempmail_lol
    domain: str = ""
    domains: list[str] = field(default_factory=list)
    address: str = ""
    prefix: str = ""
    imap_host: str = ""
    imap_port: int = 993
    imap_user: str = ""
    imap_password: str = ""
    verify_timeout: float = 180.0


@dataclass(slots=True)
class CaptchaConfig:
    provider: str = "browser"       # browser | capsolver | manual | none
    api_key: str = ""
    timeout: float = 150.0
    poll: float = 3.0


@dataclass(slots=True)
class BrowserConfig:
    stack: str = "auto"
    headless: bool = True
    cdp: str = ""
    humanize: float = 1.0
    geoip: bool = True
    timeout: float = 120.0


@dataclass(slots=True)
class AntiBanConfig:
    min_delay: float = 20.0
    max_delay: float = 55.0
    per_proxy_concurrency: int = 1
    proxy_cooldown: float = 900.0
    max_failures_per_proxy: int = 2


@dataclass(slots=True)
class Config:
    base_url: str = DEFAULT_BASE
    engine: str = "auto"            # protocol | browser | auto
    count: int = 1
    concurrency: int = 1
    attempts: int = 3
    retry_rounds: int = 1
    delay: tuple[float, float] = (20.0, 55.0)
    timeout: float = 60.0
    language: str = "en-US"
    user_agent: str = ""
    verify_email: bool = True
    claim_trial: bool = True
    proxies: list[str] = field(default_factory=list)
    output_dir: Path = Path("out")
    db_path: Path | None = None
    mail: MailConfig = field(default_factory=MailConfig)
    captcha: CaptchaConfig = field(default_factory=CaptchaConfig)
    browser: BrowserConfig = field(default_factory=BrowserConfig)
    antiban: AntiBanConfig = field(default_factory=AntiBanConfig)
    push_url: str = ""
    push_token: str = ""

    @classmethod
    def load(cls, path: str | Path | None = None, overrides: dict[str, Any] | None = None) -> "Config":
        data: dict[str, Any] = {}
        chosen = Path(path) if path else cls._discover()
        if chosen and chosen.exists():
            data = yaml.safe_load(chosen.read_text(encoding="utf-8")) or {}

        mail_raw = data.pop("mail", None) or {}
        captcha_raw = data.pop("captcha", None) or {}
        browser_raw = data.pop("browser", None) or {}
        antiban_raw = data.pop("antiban", None) or {}
        base = cls()
        cfg = cls(
            base_url=str(data.get("base_url") or os.environ.get("PROXYSCRAPE_BASE_URL") or base.base_url),
            engine=str(data.get("engine") or os.environ.get("PROXYSCRAPE_ENGINE") or base.engine),
            count=int(data.get("count", base.count)),
            concurrency=int(data.get("concurrency", base.concurrency)),
            attempts=int(data.get("attempts", base.attempts)),
            retry_rounds=int(data.get("retry_rounds", base.retry_rounds)),
            timeout=float(data.get("timeout", base.timeout)),
            language=str(data.get("language", base.language)),
            user_agent=str(data.get("user_agent", "") or ""),
            verify_email=bool(data.get("verify_email", base.verify_email)),
            claim_trial=bool(data.get("claim_trial", base.claim_trial)),
            output_dir=Path(str(data.get("output_dir", "out"))),
            push_url=str(data.get("push_url") or os.environ.get("PROXYSCRAPE_GATEWAY_URL") or ""),
            push_token=str(data.get("push_token") or os.environ.get("PROXYSCRAPE_GATEWAY_TOKEN") or ""),
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
            domain=str(mail_raw.get("domain", "") or os.environ.get("PROXYSCRAPE_MAIL_DOMAIN", "") or ""),
            domains=[str(d).strip().lower() for d in (mail_raw.get("domains") or []) if str(d).strip()],
            address=str(mail_raw.get("address", "") or ""),
            prefix=str(mail_raw.get("prefix", "") or ""),
            imap_host=str(mail_raw.get("imap_host", "") or os.environ.get("PROXYSCRAPE_IMAP_HOST", "")),
            imap_port=int(mail_raw.get("imap_port", 993) or 993),
            imap_user=str(mail_raw.get("imap_user", "") or os.environ.get("PROXYSCRAPE_IMAP_USER", "")),
            imap_password=str(mail_raw.get("imap_password", "") or os.environ.get("PROXYSCRAPE_IMAP_PASSWORD", "")),
            verify_timeout=float(mail_raw.get("verify_timeout", 180.0) or 180.0),
        )

        cfg.captcha = CaptchaConfig(
            provider=str(captcha_raw.get("provider") or os.environ.get("PROXYSCRAPE_CAPTCHA_PROVIDER") or cfg.captcha.provider),
            api_key=str(captcha_raw.get("api_key") or os.environ.get("PROXYSCRAPE_CAPTCHA_KEY") or ""),
            timeout=float(captcha_raw.get("timeout", 150.0) or 150.0),
            poll=float(captcha_raw.get("poll", 3.0) or 3.0),
        )

        cfg.browser = BrowserConfig(
            stack=str(browser_raw.get("stack") or os.environ.get("PROXYSCRAPE_BROWSER_STACK") or "auto"),
            headless=bool(browser_raw.get("headless", True)),
            cdp=str(browser_raw.get("cdp") or os.environ.get("PROXYSCRAPE_CDP") or ""),
            humanize=float(browser_raw.get("humanize", 1.0) or 1.0),
            geoip=bool(browser_raw.get("geoip", True)),
            timeout=float(browser_raw.get("timeout", 120.0) or 120.0),
        )

        cfg.antiban = AntiBanConfig(
            min_delay=float(antiban_raw.get("min_delay", cfg.delay[0]) or cfg.delay[0]),
            max_delay=float(antiban_raw.get("max_delay", cfg.delay[1]) or cfg.delay[1]),
            per_proxy_concurrency=max(1, int(antiban_raw.get("per_proxy_concurrency", 1) or 1)),
            proxy_cooldown=float(antiban_raw.get("proxy_cooldown", 900.0) or 900.0),
            max_failures_per_proxy=max(1, int(antiban_raw.get("max_failures_per_proxy", 2) or 2)),
        )

        for key, value in (overrides or {}).items():
            if value is None:
                continue
            if key == "mail_provider":
                cfg.mail.provider = str(value)
            elif key == "mail_domain":
                cfg.mail.domain = str(value)
            elif key == "mail_domains":
                cfg.mail.domains = [str(x).strip().lower() for x in (value or []) if str(x).strip()]
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

    def domains(self) -> list[str]:
        out = list(self.mail.domains)
        if self.mail.domain and self.mail.domain not in out:
            out.insert(0, self.mail.domain)
        return out

    def describe(self) -> dict[str, Any]:
        return {
            "base_url": self.base_url,
            "engine": self.engine,
            "count": self.count,
            "concurrency": self.concurrency,
            "claim_trial": self.claim_trial,
            "verify_email": self.verify_email,
            "proxies": len(self.proxies),
            "mail_provider": self.mail.provider,
            "mail_domains": self.domains(),
            "captcha": self.captcha.provider,
            "browser": {"stack": self.browser.stack, "headless": self.browser.headless, "cdp": bool(self.browser.cdp)},
            "output_dir": str(self.output_dir),
            "push": bool(self.push_url and self.push_token),
        }
