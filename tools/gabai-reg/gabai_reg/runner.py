"""NDJSON-driven concurrent signup runner for gab.ai."""

from __future__ import annotations

import random
import threading
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from typing import Any, Callable, Protocol

from .browser import BrowserUnavailable, FingerprintBrowser
from .captcha import solve_turnstile
from .config import Config
from .errors import (
    Blocked,
    CaptchaRequired,
    EmailRejected,
    EmailTaken,
    NoApiKey,
    PlusRequired,
    RateLimited,
    SignupError,
    Transport,
    VerificationPending,
)
from .identity import Persona, make_persona
from .mail_pool import DomainPool
from .mailbox import Provider, build_provider
from .protocol import TURNSTILE_SITE_KEY, TURNSTILE_ACTION, GabSession
from .store import Store

_print_lock = threading.Lock()


class MailboxFactory(Protocol):
    def __call__(self, cfg: Config, proxy: str) -> Provider: ...


class Runner:
    def __init__(
        self,
        cfg: Config,
        store: Store,
        *,
        on_event: Callable[[dict[str, Any]], None] | None = None,
        quiet: bool = False,
        mail_factory: MailboxFactory | None = None,
        captcha_solver: Callable[..., str] | None = None,
        session_factory: Callable[..., GabSession] | None = None,
        browser_factory: Callable[..., Any] | None = None,
    ) -> None:
        self.cfg = cfg
        self.store = store
        self.on_event = on_event
        self.quiet = quiet
        self.mail_factory = mail_factory or (lambda c, proxy: build_provider(c.mail, proxy=proxy))
        self.captcha_solver = captcha_solver or solve_turnstile
        self.session_factory = session_factory or (lambda **kw: GabSession(**kw))
        self.browser_factory = browser_factory or (lambda **kw: FingerprintBrowser(**kw))
        self.cfg.output_dir.mkdir(parents=True, exist_ok=True)
        self.log_path = self.cfg.output_dir / "run.log"
        # Domain scorecard: same SQLite file as the accounts, so nothing extra
        # to keep in sync with the gateway pool.
        try:
            self.domains = DomainPool(self.cfg.db_path)
        except Exception:  # noqa: BLE001 - a broken scorecard must not stop a run
            self.domains = None
        self._counter = 0
        self._counter_lock = threading.Lock()

    def emit(self, event: dict[str, Any]) -> None:
        if not self.on_event:
            return
        event.setdefault("at", time.strftime("%Y-%m-%dT%H:%M:%S%z"))
        try:
            self.on_event(event)
        except Exception:
            pass

    def log(self, message: str, *, level: str = "info") -> None:
        line = f"[{time.strftime('%H:%M:%S')}] {level.upper():5} {message}"
        with _print_lock:
            if not self.quiet:
                print(line, flush=True)
            try:
                with self.log_path.open("a", encoding="utf-8") as handle:
                    handle.write(line + "\n")
            except OSError:
                pass
        self.emit({"type": "log", "level": level, "message": message})

    def _proxy(self) -> str:
        with self._counter_lock:
            index = self._counter
            self._counter += 1
        return self.cfg.proxy_for(index)

    @staticmethod
    def _sleep(low: float, high: float) -> None:
        time.sleep(random.uniform(low, high))

    def _domains_for(self, provider: Provider) -> list[str]:
        """Configured domains first, then the provider's, ordered by the scorecard."""
        configured: list[str] = []
        if self.cfg.mail.domain:
            configured.append(self.cfg.mail.domain)
        for domain in list(self.cfg.mail.domains) + list(provider.domains()):
            if domain and domain not in configured:
                configured.append(domain)
        if self.domains is None:
            return configured
        try:
            return self.domains.candidates(provider.name, configured)
        except Exception:  # noqa: BLE001
            return configured

    def _domain_ok(self, provider: str, domain: str) -> None:
        if self.domains is None or not domain:
            return
        try:
            self.domains.record_ok(provider, domain)
        except Exception:  # noqa: BLE001
            pass

    def _domain_bad(self, provider: str, domain: str, reason: str) -> None:
        if self.domains is None or not domain:
            return
        try:
            self.domains.record_bad(provider, domain, reason)
        except Exception:  # noqa: BLE001
            pass

    def turnstile(self, proxy: str) -> str:
        return self.captcha_solver(
            provider=self.cfg.captcha.provider,
            api_key=self.cfg.captcha.api_key,
            site_url=f"{self.cfg.base_url.rstrip('/')}/signup",
            site_key=TURNSTILE_SITE_KEY,
            action=self.cfg.captcha.action or TURNSTILE_ACTION,
            proxy=proxy,
            timeout=self.cfg.captcha.timeout,
        )

    def run(self, count: int | None = None) -> dict[str, Any]:
        target = count or self.cfg.count
        started = time.time()
        created: list[dict[str, Any]] = []
        failures: list[str] = []
        pending = list(range(target))
        retries = 0

        self.emit(
            {
                "type": "start",
                "provider": "gabai",
                "base_url": self.cfg.base_url,
                "engine": self.cfg.engine,
                "browser_stack": self.cfg.browser.stack,
                "count": target,
                "concurrency": self.cfg.concurrency,
                "mail_provider": self.cfg.mail.provider,
                "mail_domains": list(self.cfg.mail.domains),
                "captcha": self.cfg.captcha.provider,
                "proxies": len(self.cfg.proxies),
                "retry_rounds": self.cfg.retry_rounds,
                "verify_email": self.cfg.verify_email,
                "create_api_key": self.cfg.create_api_key,
            }
        )

        for round_no in range(self.cfg.retry_rounds + 1):
            if not pending:
                break
            if round_no:
                retries += len(pending)
                self.emit({"type": "retry", "round": round_no, "count": len(pending)})
                self.log(f"retry round {round_no}/{self.cfg.retry_rounds}: {len(pending)} account(s)", level="warn")
                failures.clear()
                self._sleep(2, 6)

            still_failing: list[int] = []
            with ThreadPoolExecutor(max_workers=max(1, self.cfg.concurrency)) as pool:
                futures = {pool.submit(self.signup, index, round_no): index for index in pending}
                for future in as_completed(futures):
                    index = futures[future]
                    try:
                        account = future.result()
                        created.append(account)
                        self.log(f"#{index + 1} ok {account['email']} key={account.get('api_key','')[:10]}…")
                    except Exception as exc:
                        still_failing.append(index)
                        failures.append(f"#{index + 1} {type(exc).__name__}: {exc}")
                        self.log(f"#{index + 1} failed {type(exc).__name__}: {exc}", level="error")
            pending = sorted(still_failing)

        jsonl = self.cfg.output_dir / "accounts.jsonl"
        csv_path = self.cfg.output_dir / "accounts.csv"
        rows = self.store.export_jsonl(jsonl)
        self.store.export_csv(csv_path)

        summary = {
            "provider": "gabai",
            "requested": target,
            "created": len(created),
            "failed": len(pending),
            "retries": retries,
            "stored": self.store.count(),
            "seconds": round(time.time() - started, 1),
            "jsonl": str(jsonl),
            "csv": str(csv_path),
            "jsonl_rows": rows,
            "failures": failures,
            "failure_reasons": self.store.failure_summary(),
        }
        self.emit({"type": "done", "summary": summary})
        return summary

    def signup(self, index: int, round_no: int = 0) -> dict[str, Any]:
        last_error: Exception | None = None
        persona: Persona = make_persona(random.Random())
        email_addr = ""
        domain = ""
        proxy = ""

        for attempt in range(1, self.cfg.attempts + 1):
            if attempt > 1:
                self._sleep(*self.cfg.delay)
            persona = make_persona(random.Random())
            proxy = self._proxy() if self.cfg.proxy_rotate else (self.cfg.proxies[0] if self.cfg.proxies else "")
            provider = self.mail_factory(self.cfg, proxy)
            engine = "protocol"
            try:
                candidates = self._domains_for(provider)
                chosen_domain = candidates[(attempt - 1) % len(candidates)] if candidates else ""
                box = provider.allocate(persona.local_part, domain=chosen_domain)
                email_addr = box.address
                domain = email_addr.rsplit("@", 1)[-1]
                self.emit({"type": "attempt", "index": index + 1, "attempt": attempt, "email": email_addr, "domain": domain, "provider": provider.name})

                if self.cfg.engine == "browser":
                    account = self._browser_signup(email_addr, persona, proxy)
                    engine = "browser"
                else:
                    try:
                        token = self.turnstile(proxy)
                        with self.session_factory(base_url=self.cfg.base_url, proxy=proxy, timeout=self.cfg.timeout, language=self.cfg.language, user_agent=self.cfg.user_agent) as client:
                            # signup
                            payload = client.signup(email=email_addr, password=persona.password, turnstile=token, client_info={"platform": "web"})
                            user = payload.get("user") or {}
                            plus = bool(((user.get("flags") or {}).get("isPlus")))
                            email_verified = ((user.get("flags") or {}).get("isEmailVerified"))

                            api_key = ""
                            api_key_id = ""
                            api_key_name = ""
                            api_blocked = ""

                            if self.cfg.create_api_key:
                                created = client.api_key_create(name=f"gw-{time.strftime('%Y%m%d')}")
                                api_key_obj = (created.get("apiKey") or {})
                                api_key = str(api_key_obj.get("key") or "")
                                api_key_id = str(api_key_obj.get("id") or "")
                                api_key_name = str(api_key_obj.get("name") or "")
                                try:
                                    client.developer_api_check(api_key)
                                except PlusRequired:
                                    api_blocked = "plus_required"

                            account = {
                                "email": email_addr,
                                "password": persona.password,
                                "api_key": api_key,
                                "api_key_id": api_key_id,
                                "api_key_name": api_key_name,
                                "api_blocked": api_blocked,
                                "access_token": client.access_token,
                                "refresh_token": client.refresh_token,
                                "expires_at": client.expires_at,
                                "email_verified": email_verified,
                                "plus": plus,
                                "cookies": [],
                            }
                        engine = "protocol"
                    except (CaptchaRequired, Blocked) as exc:
                        if self.cfg.engine != "auto":
                            raise
                        self.log(f"#{index + 1}.{attempt} protocol blocked ({type(exc).__name__}: {exc}), switching browser", level="warn")
                        account = self._browser_signup(email_addr, persona, proxy)
                        engine = "browser"

                account.setdefault("email", email_addr)
                account["persona"] = persona.as_dict()
                account["provider"] = "gabai"
                account["domain"] = domain
                account["engine"] = engine

                if self.store.add(account, engine=engine, proxy=proxy):
                    self.store.attempt(email_addr, engine, proxy, "created")
                    self._domain_ok(provider.name, domain)
                    self.emit({"type": "account", "engine": engine, "domain": domain, "provider": provider.name, "account": {
                        "email": email_addr,
                        "password": persona.password,
                        "api_key": account.get("api_key", ""),
                        "api_key_id": account.get("api_key_id", ""),
                        "api_key_name": account.get("api_key_name", ""),
                        "api_blocked": account.get("api_blocked", ""),
                        "email_verified": account.get("email_verified"),
                        "plus": account.get("plus"),
                        "engine": engine,
                        "cookies": account.get("cookies") or [],
                        "created_at": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
                    }})
                    return account

                self.store.attempt(email_addr, engine, proxy, "duplicate")
                return account

            except EmailTaken as exc:
                last_error = exc
                self.store.attempt(email_addr, engine, proxy, "email_taken", str(exc))
            except EmailRejected as exc:
                last_error = exc
                self.store.attempt(email_addr, engine, proxy, "email_rejected", str(exc))
                self._domain_bad(provider.name, domain, str(exc))
            except NoApiKey as exc:
                last_error = exc
                self.store.attempt(email_addr, engine, proxy, "no_api_key", str(exc))
            except RateLimited as exc:
                last_error = exc
                self.store.attempt(email_addr, engine, proxy, "rate_limited", str(exc))
                if attempt < self.cfg.attempts:
                    time.sleep(random.uniform(10, 25))
            except (Blocked, CaptchaRequired, Transport, SignupError) as exc:
                last_error = exc
                self.store.attempt(email_addr, engine, proxy, getattr(exc, "kind", "error"), str(exc))
                self._sleep(2, 5)
            finally:
                provider.close()

        kind = getattr(last_error, "kind", "error") if last_error else "unknown"
        reason = str(last_error) if last_error else "signup failed"
        self.store.record_failure(email=email_addr, domain=domain, kind=str(kind), reason=reason, round_no=round_no)
        self.emit({"type": "failed", "index": index + 1, "email": email_addr, "domain": domain, "kind": str(kind), "reason": reason, "round": round_no})
        raise last_error or SignupError(reason)

    def _browser_signup(self, email: str, persona: Persona, proxy: str) -> dict[str, Any]:
        screenshots = (self.cfg.output_dir / "screenshots") if self.cfg.browser.screenshots else None
        try:
            with self.browser_factory(
                base_url=self.cfg.base_url,
                headless=self.cfg.browser.headless,
                proxy=proxy,
                cdp=self.cfg.browser.cdp,
                stack=self.cfg.browser.stack,
                locale=self.cfg.language,
                geoip=self.cfg.browser.geoip,
                timeout=max(self.cfg.timeout, 60.0),
                humanize=self.cfg.browser.humanize,
                turnstile_timeout=self.cfg.browser.turnstile_timeout,
                screenshots=screenshots,
                log=lambda msg: self.log(msg, level="debug"),
            ) as browser:
                out = browser.signup_flow(email=email, password=persona.password, key_name=f"gw-{time.strftime('%Y%m%d')}")
        except BrowserUnavailable as exc:
            raise SignupError(f"browser unavailable: {exc}") from exc
        # browser flow returns api_key only; mark plus_required by probing quickly (optional)
        out.setdefault("api_blocked", "")
        out.setdefault("plus", None)
        out.setdefault("email_verified", None)
        out["api_key"] = out.get("api_key") or out.get("apiKey") or ""
        return {
            "email": email,
            "password": persona.password,
            "api_key": out.get("api_key", ""),
            "api_key_id": "",
            "api_key_name": "",
            "api_blocked": out.get("api_blocked", ""),
            "access_token": "",
            "refresh_token": "",
            "expires_at": "",
            "email_verified": out.get("email_verified"),
            "plus": out.get("plus"),
            "cookies": out.get("cookies") or [],
        }


def doctor(cfg: Config) -> dict[str, Any]:
    report: dict[str, Any] = {"config": cfg.describe(), "checks": {}}
    checks = report["checks"]
    proxy = cfg.proxies[0] if cfg.proxies else ""

    try:
        with GabSession(base_url=cfg.base_url, proxy=proxy, timeout=20.0) as s:
            r = s.client.get(f"{cfg.base_url.rstrip('/')}/signup")
            checks["signup_page"] = f"HTTP {r.status_code} {len(r.text or '')} bytes"
    except Exception as exc:
        checks["signup_page"] = f"failed: {exc}"

    try:
        provider = build_provider(cfg.mail, proxy=proxy)
        box = provider.allocate()
        checks["mailbox"] = f"{box.provider}: {box.address}"
        provider.close()
    except Exception as exc:
        checks["mailbox"] = f"failed: {exc}"

    try:
        import playwright  # noqa: F401

        checks["playwright"] = "ok"
    except Exception as exc:
        checks["playwright"] = f"missing: {exc}"

    try:
        import camoufox  # noqa: F401

        checks["camoufox"] = "ok"
    except Exception as exc:
        checks["camoufox"] = f"missing: {exc}"

    checks["engine"] = cfg.engine
    checks["cdp"] = cfg.browser.cdp or "(launch own)"
    checks["captcha_provider"] = cfg.captcha.provider
    checks["captcha_key"] = "set" if cfg.captcha.api_key else "missing"
    return report
