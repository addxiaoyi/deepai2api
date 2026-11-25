"""Config loading: YAML file + env + CLI overrides, with sane defaults."""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml

DEFAULT_CONFIG_FILES = ("config.yaml", "config.example.yaml")

# A pool entry may be "http://user:pass@host:port", "socks5://host:port", or bare "host:port".
_SCHEME_DEFAULT = "http"
DEFAULT_BASE = "https://cleverai.app"
DEFAULT_API = "https://api.cleverai.app"


def normalize_proxy(raw: str) -> str:
    value = raw.strip()
    if not value:
        return ""
    if "://" not in value:
        value = f"{_SCHEME_DEFAULT}://{value}"
    return value


def env_proxy() -> str:
    for key in ("CLEVERAI_PROXY", "HTTPS_PROXY", "https_proxy", "ALL_PROXY", "all_proxy"):
        raw = os.environ.get(key, "").strip()
        if raw:
            return normalize_proxy(raw)
    return ""


@dataclass(slots=True)
class MailConfig:
    """域名邮箱: one catch-all domain is the durable path."""

    provider: str = "imap"          # imap | fixed | mailtm | tempmail_lol
    domain: str = ""
    domains: list[str] = field(default_factory=list)
    address: str = ""               # provider=fixed: reuse one address (plus-addressing)
    prefix: str = ""                # imap: local part prefix
    imap_host: str = ""
    imap_port: int = 993
    imap_user: str = ""
    imap_password: str = ""
    verify_timeout: float = 180.0
    verify_pattern: str = r"https?://[^\s\"'<>]+"


@dataclass(slots=True)
class AppCheckConfig:
    """How to mint the Firebase App Check JWT."""

    provider: str = "browser"       # browser | capsolver | manual
    api_key: str = ""
    token: str = ""                 # manual: a literal token, or one minted elsewhere
    ttl: int = 3300
    timeout: float = 180.0


@dataclass(slots=True)
class BrowserConfig:
    """指纹浏览器引擎（协议被拦时的第二条腿，也是取 App Check 令牌的手）。"""

    stack: str = "auto"          # auto | camoufox | patchright | playwright
    headless: bool = True
    cdp: str = ""                # 外部指纹浏览器的 CDP 地址，如 http://127.0.0.1:50325
    humanize: float = 1.0        # 打字/移动/停顿的倍率
    geoip: bool = True           # 时区/语言跟随代理出口
    screenshots: bool = True
    profile_dir: str = ""        # 留空 = 每次全新上下文（推荐）
    timeout: float = 120.0


@dataclass(slots=True)
class AntiBanConfig:
    """防封策略：节奏、并发、域名轮换与冷却。"""

    min_delay: float = 25.0          # 两个账号之间的最小间隔（秒）
    max_delay: float = 70.0
    per_proxy_concurrency: int = 1    # 同一出口同时只跑一个 signup
    per_domain_burst: int = 5         # 同一邮箱域名连续最多成功几个就换域
    domain_cooldown: float = 900.0    # 域名被打回后的冷却（秒）
    max_failures_per_proxy: int = 3   # 同一出口连续失败几次就拉黑
    proxy_cooldown: float = 1800.0
    warmup: bool = True               # 注册后先跑一次真实读请求再入池
    session_ttl: float = 3000.0       # App Check 令牌提前多久续期（秒）


@dataclass(slots=True)
class Config:
    base_url: str = DEFAULT_BASE
    api_base: str = DEFAULT_API
    engine: str = "auto"            # protocol | browser | auto
    count: int = 1
    concurrency: int = 2
    attempts: int = 3
    retry_rounds: int = 1
    delay: tuple[float, float] = (25.0, 70.0)
    timeout: float = 60.0
    language: str = "en-US"
    user_agent: str = ""
    verify_email: bool = True
    chat_engine: str = "openai-gpt-6-luna"
    probes_per_account: int = 1
    proxies: list[str] = field(default_factory=list)
    proxy_rotate: bool = True
    output_dir: Path = Path("out")
    db_path: Path | None = None
    mail: MailConfig = field(default_factory=MailConfig)
    appcheck: AppCheckConfig = field(default_factory=AppCheckConfig)
    browser: BrowserConfig = field(default_factory=BrowserConfig)
    antiban: AntiBanConfig = field(default_factory=AntiBanConfig)
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
        appcheck_raw = data.pop("appcheck", None) or {}
        browser_raw = data.pop("browser", None) or {}
        antiban_raw = data.pop("antiban", None) or {}
        base = cls()
        cfg = cls(
            base_url=str(data.get("base_url") or os.environ.get("CLEVERAI_BASE_URL") or base.base_url),
            api_base=str(data.get("api_base") or os.environ.get("CLEVERAI_API_BASE") or base.api_base),
            engine=str(data.get("engine") or os.environ.get("CLEVERAI_ENGINE") or base.engine),
            count=int(data.get("count", base.count)),
            concurrency=int(data.get("concurrency", base.concurrency)),
            attempts=int(data.get("attempts", base.attempts)),
            retry_rounds=int(data.get("retry_rounds", base.retry_rounds)),
            timeout=float(data.get("timeout", base.timeout)),
            language=str(data.get("language", base.language)),
            user_agent=str(data.get("user_agent", "") or ""),
            proxy_rotate=bool(data.get("proxy_rotate", base.proxy_rotate)),
            verify_email=bool(data.get("verify_email", base.verify_email)),
            chat_engine=str(data.get("chat_engine") or os.environ.get("CLEVERAI_CHAT_ENGINE") or base.chat_engine),
            probes_per_account=max(0, int(data.get("probes_per_account", base.probes_per_account))),
            output_dir=Path(str(data.get("output_dir", "out"))),
            push_url=str(data.get("push_url") or os.environ.get("CLEVERAI_GATEWAY_URL") or ""),
            push_token=str(data.get("push_token") or os.environ.get("CLEVERAI_GATEWAY_TOKEN") or ""),
            gateway_pool_file=str(data.get("gateway_pool_file") or ""),
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
        cfg.mail.imap_host = cfg.mail.imap_host or os.environ.get("CLEVERAI_IMAP_HOST", "")
        cfg.mail.imap_user = cfg.mail.imap_user or os.environ.get("CLEVERAI_IMAP_USER", "")
        cfg.mail.imap_password = cfg.mail.imap_password or os.environ.get("CLEVERAI_IMAP_PASSWORD", "")
        cfg.mail.domain = cfg.mail.domain or os.environ.get("CLEVERAI_MAIL_DOMAIN", "")

        cfg.appcheck = AppCheckConfig(
            provider=str(
                appcheck_raw.get("provider")
                or os.environ.get("CLEVERAI_APPCHECK_PROVIDER")
                or cfg.appcheck.provider
            ),
            api_key=str(appcheck_raw.get("api_key") or os.environ.get("CLEVERAI_CAPTCHA_KEY") or ""),
            token=str(appcheck_raw.get("token") or os.environ.get("CLEVERAI_APPCHECK_TOKEN") or ""),
            ttl=int(appcheck_raw.get("ttl", 3300) or 3300),
            timeout=float(appcheck_raw.get("timeout", 180.0) or 180.0),
        )

        cfg.browser = BrowserConfig(
            stack=str(browser_raw.get("stack") or os.environ.get("CLEVERAI_BROWSER_STACK") or "auto"),
            headless=bool(browser_raw.get("headless", True)),
            cdp=str(browser_raw.get("cdp") or os.environ.get("CLEVERAI_CDP") or ""),
            humanize=float(browser_raw.get("humanize", 1.0) or 1.0),
            geoip=bool(browser_raw.get("geoip", True)),
            screenshots=bool(browser_raw.get("screenshots", True)),
            profile_dir=str(browser_raw.get("profile_dir") or ""),
            timeout=float(browser_raw.get("timeout", 120.0) or 120.0),
        )

        cfg.antiban = AntiBanConfig(
            min_delay=float(antiban_raw.get("min_delay", cfg.delay[0]) or cfg.delay[0]),
            max_delay=float(antiban_raw.get("max_delay", cfg.delay[1]) or cfg.delay[1]),
            per_proxy_concurrency=max(1, int(antiban_raw.get("per_proxy_concurrency", 1) or 1)),
            per_domain_burst=max(1, int(antiban_raw.get("per_domain_burst", 5) or 5)),
            domain_cooldown=float(antiban_raw.get("domain_cooldown", 900.0) or 900.0),
            max_failures_per_proxy=max(1, int(antiban_raw.get("max_failures_per_proxy", 3) or 3)),
            proxy_cooldown=float(antiban_raw.get("proxy_cooldown", 1800.0) or 1800.0),
            warmup=bool(antiban_raw.get("warmup", True)),
            session_ttl=float(antiban_raw.get("session_ttl", 3000.0) or 3000.0),
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
            elif key == "appcheck_provider":
                cfg.appcheck.provider = str(value)
            elif key == "captcha_key":
                cfg.appcheck.api_key = str(value)
            elif key == "appcheck_token":
                cfg.appcheck.token = str(value)
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

    # -------------------------------------------------------------- accessors
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
            "api_base": self.api_base,
            "engine": self.engine,
            "chat_engine": self.chat_engine,
            "count": self.count,
            "concurrency": self.concurrency,
            "attempts": self.attempts,
            "retry_rounds": self.retry_rounds,
            "delay": self.delay,
            "verify_email": self.verify_email,
            "proxies": len(self.proxies),
            "mail_provider": self.mail.provider,
            "mail_domains": self.domains(),
            "appcheck": self.appcheck.provider,
            "browser": {"stack": self.browser.stack, "headless": self.browser.headless, "cdp": bool(self.browser.cdp)},
            "antiban": {
                "per_proxy_concurrency": self.antiban.per_proxy_concurrency,
                "per_domain_burst": self.antiban.per_domain_burst,
                "domain_cooldown": self.antiban.domain_cooldown,
            },
            "output_dir": str(self.output_dir),
            "db": str(self.db_path),
            "push": bool(self.push_url and self.push_token),
        }
