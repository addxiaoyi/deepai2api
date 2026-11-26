"""Config loading: YAML + env + CLI overrides（所有线共用，前缀 LINE_*）。"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml

DEFAULT_CONFIG_FILES = ("config.yaml", "config.example.yaml")
DEFAULT_BASE = ""
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
    provider: str = "imap"
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
class AntiBanConfig:
    min_delay: float = 20.0
    max_delay: float = 50.0
    per_proxy_concurrency: int = 1
    proxy_cooldown: float = 900.0
    max_failures_per_proxy: int = 2


@dataclass(slots=True)
class Config:
    base_url: str = DEFAULT_BASE
    count: int = 1
    concurrency: int = 1
    attempts: int = 3
    retry_rounds: int = 1
    delay: tuple[float, float] = (20.0, 50.0)
    timeout: float = 45.0
    language: str = "en-US"
    user_agent: str = ""
    verify_email: bool = True
    checkin: bool = True
    key_name: str = ""
    proxies: list[str] = field(default_factory=list)
    output_dir: Path = Path("out")
    db_path: Path | None = None
    mail: MailConfig = field(default_factory=MailConfig)
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
        antiban_raw = data.pop("antiban", None) or {}
        base = cls()
        cfg = cls(
            base_url=str(data.get("base_url") or os.environ.get("LINE_BASE_URL") or base.base_url),
            count=int(data.get("count", base.count)),
            concurrency=int(data.get("concurrency", base.concurrency)),
            attempts=int(data.get("attempts", base.attempts)),
            retry_rounds=int(data.get("retry_rounds", base.retry_rounds)),
            timeout=float(data.get("timeout", base.timeout)),
            language=str(data.get("language", base.language)),
            user_agent=str(data.get("user_agent", "") or ""),
            verify_email=bool(data.get("verify_email", base.verify_email)),
            checkin=bool(data.get("checkin", base.checkin)),
            key_name=str(data.get("key_name", "") or ""),
            output_dir=Path(str(data.get("output_dir", "out"))),
            push_url=str(data.get("push_url") or os.environ.get("LINE_GATEWAY_URL") or ""),
            push_token=str(data.get("push_token") or os.environ.get("LINE_GATEWAY_TOKEN") or ""),
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
            domain=str(mail_raw.get("domain", "") or os.environ.get("LINE_MAIL_DOMAIN", "") or ""),
            domains=[str(d).strip().lower() for d in (mail_raw.get("domains") or []) if str(d).strip()],
            address=str(mail_raw.get("address", "") or ""),
            prefix=str(mail_raw.get("prefix", "") or ""),
            imap_host=str(mail_raw.get("imap_host", "") or os.environ.get("LINE_IMAP_HOST", "")),
            imap_port=int(mail_raw.get("imap_port", 993) or 993),
            imap_user=str(mail_raw.get("imap_user", "") or os.environ.get("LINE_IMAP_USER", "")),
            imap_password=str(mail_raw.get("imap_password", "") or os.environ.get("LINE_IMAP_PASSWORD", "")),
            verify_timeout=float(mail_raw.get("verify_timeout", 180.0) or 180.0),
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
            "count": self.count,
            "concurrency": self.concurrency,
            "verify_email": self.verify_email,
            "checkin": self.checkin,
            "proxies": len(self.proxies),
            "mail_provider": self.mail.provider,
            "mail_domains": self.domains(),
            "output_dir": str(self.output_dir),
            "push": bool(self.push_url and self.push_token),
        }
