"""Concurrent signup loop with per-failure policy (rotate address / engine / proxy)."""

from __future__ import annotations

import random
import threading
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path
from typing import Any, Callable

from .browser import register_with_browser
from .config import Config
from .errors import Blocked, EmailRejected, EmailTaken, RateLimited, SignupError
from .identity import make_persona
from .mailbox import Provider, build_provider
from .mail_pool import DomainPool, apex_domain
from .net import build_client, request
from .protocol import DeepAI
from .store import Store

_print_lock = threading.Lock()


class Runner:
    def __init__(
        self,
        cfg: Config,
        store: Store,
        *,
        on_event: Callable[[dict[str, Any]], None] | None = None,
        quiet: bool = False,
    ) -> None:
        self.cfg = cfg
        self.store = store
        self.on_event = on_event
        self.quiet = quiet
        self.cfg.output_dir.mkdir(parents=True, exist_ok=True)
        self.log_path = self.cfg.output_dir / "run.log"
        self.screens = self.cfg.output_dir / "screenshots"
        self.pool = DomainPool(cfg.db_path) if cfg.mail.auto_domains else None
        self._counter = 0
        self._counter_lock = threading.Lock()

    # ------------------------------------------------------------------ utils
    def emit(self, event: dict[str, Any]) -> None:
        if not self.on_event:
            return
        event.setdefault("at", time.strftime("%Y-%m-%dT%H:%M:%S%z"))
        try:
            self.on_event(event)
        except Exception:  # noqa: BLE001 - a broken sink must not kill a signup
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

    # ------------------------------------------------------------------- work
    def _domains_for(self, provider: Provider) -> list[str]:
        """Configured pool first, then whatever the provider is serving now."""
        configured = list(self.cfg.mail.domains)
        if self.cfg.mail.domain:
            configured.insert(0, self.cfg.mail.domain)
        if self.cfg.mail.auto_domains:
            for domain in provider.domains():
                if domain not in configured:
                    configured.append(domain)
        if self.pool is not None and configured:
            return self.pool.candidates(provider.name, configured)
        return configured

    def run(self, count: int | None = None) -> dict[str, Any]:
        target = count or self.cfg.count
        started = time.time()
        created: list[dict[str, Any]] = []
        failures: list[str] = []

        self.emit(
            {
                "type": "start",
                "engine": self.cfg.engine,
                "count": target,
                "concurrency": self.cfg.concurrency,
                "mail_provider": self.cfg.mail.provider,
                "mail_domains": list(self.cfg.mail.domains),
                "proxies": len(self.cfg.proxies),
            }
        )
        self.log(
            "start engine=%s count=%d concurrency=%d proxies=%d mail=%s"
            % (
                self.cfg.engine,
                target,
                self.cfg.concurrency,
                len(self.cfg.proxies),
                self.cfg.mail.provider,
            )
        )
        if self.cfg.proxies and self.cfg.concurrency > len(self.cfg.proxies):
            self.log(
                f"concurrency {self.cfg.concurrency} > {len(self.cfg.proxies)} proxies: "
                "several signups will share an egress IP and may hit 429",
                level="warn",
            )

        with ThreadPoolExecutor(max_workers=max(1, self.cfg.concurrency)) as pool:
            futures = {pool.submit(self.signup, index): index for index in range(target)}
            for future in as_completed(futures):
                index = futures[future]
                try:
                    account = future.result()
                    created.append(account)
                    self.log(
                        f"#{index + 1} ok {account['email']} key={account['api_key'][:8]}… "
                        f"({self.store.count()} total)"
                    )
                except Exception as exc:  # noqa: BLE001 - reported, never fatal
                    failures.append(f"#{index + 1} {type(exc).__name__}: {exc}")
                    self.log(f"#{index + 1} failed {type(exc).__name__}: {exc}", level="error")

        jsonl = self.cfg.output_dir / "accounts.jsonl"
        csv_path = self.cfg.output_dir / "accounts.csv"
        exported_jsonl = self.store.export_jsonl(jsonl)
        self.store.export_csv(csv_path)

        summary = {
            "requested": target,
            "created": len(created),
            "failed": len(failures),
            "stored": self.store.count(),
            "seconds": round(time.time() - started, 1),
            "jsonl": str(jsonl),
            "csv": str(csv_path),
            "jsonl_rows": exported_jsonl,
            "failures": failures,
            "mail_domains": self.pool.rows() if self.pool else [],
        }
        self.emit({"type": "done", "summary": summary})
        self.log(
            f"done created={summary['created']} failed={summary['failed']} "
            f"stored={summary['stored']} in {summary['seconds']}s"
        )
        return summary

    # --------------------------------------------------------------- one unit
    def signup(self, index: int) -> dict[str, Any]:
        last_error: Exception | None = None
        for attempt in range(1, self.cfg.attempts + 1):
            persona = make_persona(random.Random())
            proxy = self._proxy() if self.cfg.proxy_rotate else (self.cfg.proxies[0] if self.cfg.proxies else "")
            provider = build_provider(self.cfg.mail, proxy=proxy)
            engine = "protocol"
            email = ""
            domain = ""
            try:
                candidates = self._domains_for(provider)
                if candidates:
                    domain = candidates[(attempt - 1) % len(candidates)]
                box = provider.allocate(persona.local_part, domain=domain)
                email = box.address
                domain = apex_domain(email) or domain
                self.emit(
                    {
                        "type": "attempt",
                        "index": index + 1,
                        "attempt": attempt,
                        "email": email,
                        "domain": domain,
                        "provider": provider.name,
                    }
                )
                self.log(f"#{index + 1}.{attempt} try {email} via {self.cfg.engine}")

                if self.cfg.engine in ("protocol", "auto"):
                    try:
                        account = self._protocol_signup(email, persona.password, proxy, provider, box)
                        engine = "protocol"
                    except Blocked as exc:
                        if self.cfg.engine != "auto":
                            raise
                        self.log(f"protocol blocked ({exc}); escalating to Camoufox", level="warn")
                        account = self._browser_signup(email, persona.password, proxy)
                        engine = "browser"
                else:
                    account = self._browser_signup(email, persona.password, proxy)
                    engine = "browser"

                account.setdefault("email", email)
                account["persona"] = persona.as_dict()
                if self.pool is not None and domain:
                    self.pool.record_ok(provider.name, domain)
                self.emit(
                    {
                        "type": "account",
                        "engine": engine,
                        "domain": domain,
                        "provider": provider.name,
                        "account": {
                            "email": email,
                            "username": account.get("username", ""),
                            "api_key": account.get("api_key", ""),
                            "password": account.get("password", ""),
                            "is_pro": bool(account.get("is_pro")),
                            "plan": account.get("plan", ""),
                        },
                    }
                )
                if self.store.add(account, engine=engine, proxy=proxy):
                    self.store.attempt(email, engine, proxy, "created")
                    return account
                # Duplicate address or key: treat as skip, not failure.
                self.store.attempt(email, engine, proxy, "duplicate")
                self.log(f"#{index + 1} {email} already in the store, skipped", level="warn")
                return account

            except EmailTaken as exc:
                last_error = exc
                self.store.attempt(email if "email" in locals() else "", engine, proxy, "email_taken", str(exc))
                self.log(f"#{index + 1}.{attempt} {email} already registered, rotating", level="warn")
            except EmailRejected as exc:
                last_error = exc
                self.store.attempt(email if "email" in locals() else "", engine, proxy, "email_rejected", str(exc))
                if self.pool is not None and domain:
                    self.pool.record_bad(provider.name, domain, str(exc))
                self.log(f"#{index + 1}.{attempt} address rejected ({exc}), rotating", level="warn")
            except RateLimited as exc:
                last_error = exc
                self.store.attempt(email if "email" in locals() else "", engine, proxy, "rate_limited", str(exc))
                wait = random.uniform(20, 45)
                self.log(f"#{index + 1}.{attempt} rate limited, sleeping {wait:.0f}s", level="warn")
                time.sleep(wait)
            except SignupError as exc:
                last_error = exc
                self.store.attempt(email if "email" in locals() else "", engine, proxy, "error", str(exc))
                self.log(f"#{index + 1}.{attempt} {type(exc).__name__}: {exc}", level="warn")
                self._sleep(2, 5)
            finally:
                provider.close()
                if attempt < self.cfg.attempts:
                    self._sleep(*self.cfg.delay)

        raise last_error or SignupError("signup failed without a recorded cause")

    # ------------------------------------------------------------- internals
    def _protocol_signup(
        self,
        email: str,
        password: str,
        proxy: str,
        provider: Provider,
        box: Any,
    ) -> dict[str, Any]:
        with DeepAI(proxy=proxy, timeout=self.cfg.timeout, language=self.cfg.language) as client:
            account = client.signup(email, password)
            if self.cfg.verify_email:
                link = provider.wait_for_link(box, timeout=self.cfg.mail.verify_timeout)
                if link:
                    self.log(f"verification mail found, confirming: {link[:80]}")
                    try:
                        request(client.client, "GET", link, follow_redirects=True)
                        account["email_verified"] = True
                    except SignupError as exc:
                        self.log(f"verification GET failed: {exc}", level="warn")
                else:
                    # DeepAI currently signs accounts up without a mail loop; not fatal.
                    account["email_verified"] = None
            return account

    def _browser_signup(self, email: str, password: str, proxy: str) -> dict[str, Any]:
        return register_with_browser(
            email,
            password,
            proxy=proxy,
            headless=self.cfg.headless,
            geoip=self.cfg.geoip,
            locale=self.cfg.language,
            timeout=self.cfg.timeout * 2,
            screenshots=self.screens,
            log=lambda message: self.log(message, level="debug"),
        )


def doctor(cfg: Config) -> dict[str, Any]:
    """Environment report; safe to run anywhere, touches each dependency once."""
    report: dict[str, Any] = {"config": cfg.describe(), "checks": {}}
    checks = report["checks"]

    try:
        import camoufox  # noqa: F401

        checks["camoufox_python"] = "ok"
    except Exception as exc:  # noqa: BLE001
        checks["camoufox_python"] = f"missing: {exc}"

    try:
        from camoufox.pkgman import installed_verstr

        checks["camoufox_browser"] = installed_verstr() or "not installed (python -m camoufox fetch)"
    except Exception as exc:  # noqa: BLE001
        checks["camoufox_browser"] = f"unknown: {exc}"

    proxy = cfg.proxies[0] if cfg.proxies else ""
    client = build_client(proxy=proxy, timeout=25)
    try:
        home = request(client, "GET", "https://deepai.org/")
        checks["deepai_home"] = f"HTTP {home.status_code}, {len(home.text)} bytes"
        lookup = request(
            client,
            "POST",
            "https://api.deepai.org/get_user_login_type",
            data={"email": f"probe{int(time.time())}@example.com"},
        )
        checks["deepai_lookup"] = f"HTTP {lookup.status_code} {lookup.text[:80]}"
        ip = request(client, "GET", "https://api.ipify.org?format=json")
        checks["egress_ip"] = ip.text[:64]
    except SignupError as exc:
        checks["deepai"] = f"unreachable: {exc}"
    finally:
        client.close()

    try:
        provider = build_provider(cfg.mail, proxy=proxy)
        box = provider.allocate()
        checks["mailbox"] = f"{box.provider}: {box.address}"
        provider.close()
    except Exception as exc:  # noqa: BLE001
        checks["mailbox"] = f"failed: {exc}"

    return report
