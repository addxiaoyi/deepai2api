"""Config for the Poe factory (YAML + env + CLI overrides)."""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml

DEFAULT_FILES = ("config.yaml", "config.example.yaml")


@dataclass(slots=True)
class SmsConfig:
    """Phone leg. `off` leaves accounts at phone_required (the default)."""

    provider: str = "off"          # off | manual | sms_activate | smshub | custom
    api_key: str = ""
    service: str = "poe"           # provider-side service code
    country: str = "0"             # provider-side country id
    country_code: str = "+1"       # e-number dialling code for the UI picker
    max_price: float = 0.5
    wait_seconds: float = 180.0
    # manual: the operator pastes the SMS code on stdin when asked

    @property
    def enabled(self) -> bool:
        return self.provider not in ("", "off", "none")


@dataclass(slots=True)
class Config:
    engine: str = "auto"           # protocol | browser | auto
    count: int = 1
    concurrency: int = 1
    attempts: int = 2
    retry_rounds: int = 1
    delay: tuple[float, float] = (25.0, 55.0)   # pacing between accounts (anti-ban)
    account_cooldown: float = 120.0             # per-proxy cool-down after a signup
    timeout: float = 60.0
    headless: bool = True
    language: str = "en-US"
    proxies: list[str] = field(default_factory=list)
    proxy_rotate: bool = True
    geoip: bool = True
    output_dir: Path = Path("out")
    db_path: Path | None = None
    device_os: list[str] = field(default_factory=lambda: ["windows"])
    keep_cookies: bool = True      # store p-b so the session survives
    mail: dict[str, Any] = field(default_factory=dict)
    sms: SmsConfig = field(default_factory=SmsConfig)
    user_agent: str = ""
    dry_run: bool = False

    # ------------------------------------------------------------------ load
    @classmethod
    def load(cls, path: str | Path | None = None, overrides: dict[str, Any] | None = None) -> "Config":
        chosen = Path(path) if path else cls._discover()
        data: dict[str, Any] = {}
        if chosen and chosen.exists():
            data = yaml.safe_load(chosen.read_text(encoding="utf-8")) or {}

        base = cls()
        cfg = cls(
            engine=str(data.get("engine", base.engine)),
            count=int(data.get("count", base.count)),
            concurrency=int(data.get("concurrency", base.concurrency)),
            attempts=int(data.get("attempts", base.attempts)),
            retry_rounds=int(data.get("retry_rounds", base.retry_rounds)),
            account_cooldown=float(data.get("account_cooldown", base.account_cooldown)),
            timeout=float(data.get("timeout", base.timeout)),
            headless=bool(data.get("headless", base.headless)),
            language=str(data.get("language", base.language)),
            proxy_rotate=bool(data.get("proxy_rotate", base.proxy_rotate)),
            geoip=bool(data.get("geoip", base.geoip)),
            output_dir=Path(str(data.get("output_dir", "out"))),
            keep_cookies=bool(data.get("keep_cookies", base.keep_cookies)),
            mail=dict(data.get("mail") or {}),
            device_os=list(data.get("device_os") or base.device_os),
        )
        delay = data.get("delay") or list(base.delay)
        if isinstance(delay, (int, float)):
            cfg.delay = (float(delay), float(delay))
        elif isinstance(delay, (list, tuple)) and len(delay) == 2:
            cfg.delay = (float(delay[0]), float(delay[1]))

        proxies = data.get("proxies") or []
        if isinstance(proxies, str):
            proxies = [proxies]
        cfg.proxies = [p.strip() if "://" in str(p) else f"http://{str(p).strip()}" for p in proxies if str(p).strip()]
        if not cfg.proxies:
            env = os.environ.get("HTTPS_PROXY") or os.environ.get("ALL_PROXY") or ""
            if env.strip():
                cfg.proxies = [env.strip()]

        sms_raw = data.get("sms") or {}
        cfg.sms = SmsConfig(
            provider=str(os.environ.get("POE_SMS_PROVIDER") or sms_raw.get("provider") or "off"),
            api_key=str(os.environ.get("POE_SMS_API_KEY") or sms_raw.get("api_key") or ""),
            service=str(sms_raw.get("service") or "poe"),
            country=str(sms_raw.get("country") or "0"),
            country_code=str(sms_raw.get("country_code") or "+1"),
            max_price=float(sms_raw.get("max_price") or 0.5),
            wait_seconds=float(sms_raw.get("wait_seconds") or 180.0),
        )

        for key, value in (overrides or {}).items():
            if value is None:
                continue
            if key == "mail_provider":
                cfg.mail["provider"] = str(value)
            elif key == "mail_domain":
                cfg.mail["domain"] = str(value)
            elif key == "mail_domains":
                cfg.mail["domains"] = list(value)
            elif key == "sms_provider":
                cfg.sms.provider = str(value)
            elif hasattr(cfg, key):
                setattr(cfg, key, value)

        cfg.output_dir = Path(cfg.output_dir)
        cfg.db_path = Path(cfg.db_path) if cfg.db_path else cfg.output_dir / "poe-pool.sqlite3"
        return cfg

    @staticmethod
    def _discover() -> Path | None:
        for name in DEFAULT_FILES:
            candidate = Path(name)
            if candidate.exists():
                return candidate
        here = Path(__file__).resolve().parent.parent
        for name in DEFAULT_FILES:
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
            "engine": self.engine,
            "count": self.count,
            "concurrency": self.concurrency,
            "proxies": len(self.proxies),
            "sms": self.sms.provider,
            "delay": self.delay,
            "db": str(self.db_path),
        }
