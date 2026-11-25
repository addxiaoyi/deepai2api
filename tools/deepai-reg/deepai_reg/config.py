"""Config loading: YAML file + env + CLI overrides, with sane defaults."""

from __future__ import annotations

import os
from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import Any

import yaml

DEFAULT_CONFIG_FILES = ("config.yaml", "config.example.yaml")

# A pool entry may be "http://user:pass@host:port", "socks5://host:port", or bare "host:port".
_SCHEME_DEFAULT = "http"


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
    """Where the signup address comes from."""

    provider: str = "tempmail_lol"  # tempmail_lol | mailtm | imap | fixed
    domain: str = ""                # force a provider domain / catch-all domain for imap
    domains: list[str] = field(default_factory=list)  # rotation pool for the provider
    auto_domains: bool = True       # learn which domains survived DeepAI's blocklist
    address: str = ""               # provider=fixed: reuse one address (plus-addressing)
    prefix: str = ""                # imap: local part prefix
    imap_host: str = ""
    imap_port: int = 993
    imap_user: str = ""
    imap_password: str = ""
    verify_timeout: float = 180.0   # seconds to wait for a verification mail
    verify_pattern: str = r"https?://[^\s\"'<>]+"


@dataclass(slots=True)
class Config:
    engine: str = "auto"            # protocol | browser | auto
    count: int = 1
    concurrency: int = 2
    attempts: int = 3
    delay: tuple[float, float] = (4.0, 9.0)
    timeout: float = 45.0
    headless: bool = True
    language: str = "en-US"
    proxies: list[str] = field(default_factory=list)
    proxy_rotate: bool = True
    geoip: bool = True
    output_dir: Path = Path("out")
    db_path: Path | None = None
    mail: MailConfig = field(default_factory=MailConfig)
    user_agent: str = ""            # empty -> generated Firefox UA
    verify_email: bool = True       # poll the mailbox for a confirmation link
    screenshot_on_fail: bool = True

    # ---------------------------------------------------------------- loading
    @classmethod
    def load(cls, path: str | Path | None = None, overrides: dict[str, Any] | None = None) -> "Config":
        data: dict[str, Any] = {}
        chosen = Path(path) if path else cls._discover()
        if chosen and chosen.exists():
            data = yaml.safe_load(chosen.read_text(encoding="utf-8")) or {}
            data.setdefault("__source__", str(chosen))

        mail_raw = data.pop("mail", None) or {}
        base = cls()
        cfg = cls(
            engine=str(data.get("engine", base.engine)),
            count=int(data.get("count", base.count)),
            concurrency=int(data.get("concurrency", base.concurrency)),
            attempts=int(data.get("attempts", base.attempts)),
            timeout=float(data.get("timeout", base.timeout)),
            headless=bool(data.get("headless", base.headless)),
            language=str(data.get("language", base.language)),
            proxy_rotate=bool(data.get("proxy_rotate", base.proxy_rotate)),
            geoip=bool(data.get("geoip", base.geoip)),
            user_agent=str(data.get("user_agent", "") or ""),
            verify_email=bool(data.get("verify_email", base.verify_email)),
            screenshot_on_fail=bool(data.get("screenshot_on_fail", base.screenshot_on_fail)),
            output_dir=Path(str(data.get("output_dir", "out"))),
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
            auto_domains=bool(mail_raw.get("auto_domains", True)),
            address=str(mail_raw.get("address", "") or ""),
            prefix=str(mail_raw.get("prefix", "") or ""),
            imap_host=str(mail_raw.get("imap_host", "") or ""),
            imap_port=int(mail_raw.get("imap_port", 993) or 993),
            imap_user=str(mail_raw.get("imap_user", "") or ""),
            imap_password=str(mail_raw.get("imap_password", "") or ""),
            verify_timeout=float(mail_raw.get("verify_timeout", 180.0) or 180.0),
            verify_pattern=str(mail_raw.get("verify_pattern", MailConfig.verify_pattern)),
        )
        # env fills the IMAP secrets so they need not live in the YAML
        cfg.mail.imap_host = cfg.mail.imap_host or os.environ.get("DEEPAI_IMAP_HOST", "")
        cfg.mail.imap_user = cfg.mail.imap_user or os.environ.get("DEEPAI_IMAP_USER", "")
        cfg.mail.imap_password = cfg.mail.imap_password or os.environ.get("DEEPAI_IMAP_PASSWORD", "")

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
            elif key == "mail_auto_domains":
                cfg.mail.auto_domains = bool(value)
            elif key == "mail_address":
                cfg.mail.address = str(value)
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

    # -------------------------------------------------------------- accessors
    def proxy_for(self, index: int) -> str:
        if not self.proxies:
            return ""
        return self.proxies[index % len(self.proxies)]

    def with_overrides(self, **kwargs: Any) -> "Config":
        return replace(self, **kwargs)

    def describe(self) -> dict[str, Any]:
        return {
            "engine": self.engine,
            "count": self.count,
            "concurrency": self.concurrency,
            "attempts": self.attempts,
            "delay": self.delay,
            "proxies": len(self.proxies),
            "mail_provider": self.mail.provider,
            "mail_domains": self.mail.domains,
            "output_dir": str(self.output_dir),
            "db": str(self.db_path),
        }
