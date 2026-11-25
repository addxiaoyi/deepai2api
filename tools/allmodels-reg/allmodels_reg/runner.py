"""Concurrent signup loop with per-failure policy (rotate address / engine / proxy)."""

from __future__ import annotations

import random
import threading
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from typing import Any, Callable, Protocol

from .browser import BrowserUnavailable, FingerprintBrowser, available_stacks
from .captcha import CaptchaError, solve_turnstile
from .config import Config
from .errors import (
    Blocked,
    CaptchaRequired,
    EmailRejected,
    EmailTaken,
    RateLimited,
    SignupError,
    VerificationPending,
)
from .identity import Persona, make_persona
from .mailbox import Provider, build_provider
from .protocol import TURNSTILE_ACTION, TURNSTILE_SITE_KEY, AllModels, probe
from .store import Store

_print_lock = threading.Lock()


class MailboxFactory(Protocol):
    """Test seam: production builds providers from config, tests inject fakes."""

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
        session_factory: Callable[..., AllModels] | None = None,
        browser_factory: Callable[..., Any] | None = None,
    ) -> None:
        self.cfg = cfg
        self.store = store
        self.on_event = on_event
        self.quiet = quiet
        self.mail_factory = mail_factory or (lambda c, proxy: build_provider(c.mail, proxy=proxy))
        self.captcha_solver = captcha_solver or solve_turnstile
        self.session_factory = session_factory or (lambda **kw: AllModels(**kw))
        self.browser_factory = browser_factory or (lambda **kw: FingerprintBrowser(**kw))
        self.cfg.output_dir.mkdir(parents=True, exist_ok=True)
        self.log_path = self.cfg.output_dir / "run.log"
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

    def _domains_for(self, provider: Provider) -> list[str]:
        configured = list(self.cfg.mail.domains)
        if self.cfg.mail.domain:
            configured.insert(0, self.cfg.mail.domain)
        for domain in provider.domains():
            if domain not in configured and domain not in ("gmail.com", "googlemail.com"):
                configured.append(domain)
        return configured

    def turnstile(self, proxy: str) -> str:
        return self.captcha_solver(
            provider=self.cfg.captcha.provider,
            api_key=self.cfg.captcha.api_key,
            site_url=f"{self.cfg.base_url.rstrip('/')}/signup",
            site_key=TURNSTILE_SITE_KEY,
            action=self.cfg.captcha.action or TURNSTILE_ACTION,
            proxy=proxy,
            timeout=self.cfg.captcha.timeout,
            poll=self.cfg.captcha.poll,
        )

    # ------------------------------------------------------------------- work
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
                "provider": "allmodels",
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
                "keys_per_account": self.cfg.keys_per_account,
            }
        )
        self.log(
            "start base=%s engine=%s count=%d concurrency=%d proxies=%d mail=%s captcha=%s keys=%d"
            % (
                self.cfg.base_url,
                self.cfg.engine,
                target,
                self.cfg.concurrency,
                len(self.cfg.proxies),
                self.cfg.mail.provider,
                self.cfg.captcha.provider,
                self.cfg.keys_per_account,
            )
        )
        if self.cfg.proxies and self.cfg.concurrency > len(self.cfg.proxies):
            self.log(
                f"concurrency {self.cfg.concurrency} > {len(self.cfg.proxies)} proxies: "
                "several signups will share an egress IP and may hit 429 / Turnstile",
                level="warn",
            )

        for round_no in range(self.cfg.retry_rounds + 1):
            if not pending:
                break
            if round_no:
                retries += len(pending)
                self.emit({"type": "retry", "round": round_no, "count": len(pending)})
                self.log(
                    f"retry round {round_no}/{self.cfg.retry_rounds}: {len(pending)} account(s) "
                    "with a fresh persona/address/proxy",
                    level="warn",
                )
                failures.clear()
                self._sleep(3, 7)

            still_failing: list[int] = []
            with ThreadPoolExecutor(max_workers=max(1, self.cfg.concurrency)) as pool:
                futures = {pool.submit(self.signup, index, round_no): index for index in pending}
                for future in as_completed(futures):
                    index = futures[future]
                    try:
                        account = future.result()
                        created.append(account)
                        self.log(
                            f"#{index + 1} ok {account['email']} key={account['api_key'][:10]}… "
                            f"({self.store.count()} stored)"
                        )
                    except Exception as exc:  # noqa: BLE001 - reported, never fatal
                        still_failing.append(index)
                        failures.append(f"#{index + 1} {type(exc).__name__}: {exc}")
                        self.log(f"#{index + 1} failed {type(exc).__name__}: {exc}", level="error")
            pending = sorted(still_failing)

        jsonl = self.cfg.output_dir / "accounts.jsonl"
        csv_path = self.cfg.output_dir / "accounts.csv"
        rows = self.store.export_jsonl(jsonl)
        self.store.export_csv(csv_path)

        summary = {
            "provider": "allmodels",
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
        self.log(
            f"done created={summary['created']} failed={summary['failed']} "
            f"stored={summary['stored']} in {summary['seconds']}s"
        )
        return summary

    # --------------------------------------------------------------- one unit
    def signup(self, index: int, round_no: int = 0) -> dict[str, Any]:
        last_error: Exception | None = None
        persona: Persona = make_persona(random.Random())
        email = ""
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
                if candidates:
                    domain = candidates[(attempt - 1) % len(candidates)]
                box = provider.allocate(persona.local_part, domain=domain)
                email = box.address
                domain = email.rsplit("@", 1)[-1]
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
                self.log(f"#{index + 1}.{attempt} try {email} via {self.cfg.base_url}")

                if self.cfg.engine == "browser":
                    account = self._browser_signup(email, persona, proxy, provider, box)
                    engine = "browser"
                else:
                    try:
                        token = self.turnstile(proxy)
                        self.log(
                            f"turnstile token: {'yes' if token else 'none'} ({len(token)} chars)", level="debug"
                        )
                        with self.session_factory(
                            base_url=self.cfg.base_url,
                            proxy=proxy,
                            timeout=self.cfg.timeout,
                            language=self.cfg.language,
                            user_agent=self.cfg.user_agent,
                        ) as client:
                            account = client.signup_flow(
                                email,
                                persona.password,
                                name=persona.name,
                                turnstile=token,
                                verify=self.cfg.verify_email,
                                provider=provider,
                                box=box,
                                verify_timeout=self.cfg.mail.verify_timeout,
                                key_name=self.cfg.key_name(email),
                                keys=self.cfg.keys_per_account,
                                log=lambda message: self.log(message, level="debug"),
                            )
                        engine = "protocol"
                    except (CaptchaRequired, CaptchaError, Blocked) as exc:
                        if self.cfg.engine != "auto":
                            raise
                        self.log(
                            f"#{index + 1}.{attempt} 协议被拦（{type(exc).__name__}: {exc}），切指纹浏览器",
                            level="warn",
                        )
                        account = self._browser_signup(email, persona, proxy, provider, box)
                        engine = "browser"

                account.setdefault("email", email)
                account["persona"] = persona.as_dict()
                account["provider"] = "allmodels"
                account["domain"] = domain
                # Which leg produced it travels with the record, so the pool and
                # the HTTP push agree even when both paths land at once.
                account["engine"] = engine

                if self.store.add(account, engine=engine, proxy=proxy):
                    self.store.attempt(email, engine, proxy, "created")
                    self.emit(
                        {
                            "type": "account",
                            "engine": engine,
                            "domain": domain,
                            "provider": provider.name,
                            "account": {
                                "email": email,
                                "name": account.get("name", ""),
                                "api_key": account.get("api_key", ""),
                                "api_key_source": account.get("api_key_source", ""),
                                "key_id": account.get("key_id", ""),
                                "key_prefix": account.get("key_prefix", ""),
                                "key_last4": account.get("key_last4", ""),
                                "extra_keys": account.get("extra_keys") or [],
                                "project_id": account.get("project_id", ""),
                                "guardrail_id": account.get("guardrail_id", ""),
                                "organization_id": account.get("organization_id", ""),
                                "email_verified": account.get("email_verified"),
                                "password": account.get("password", ""),
                                "cookies": account.get("cookies") or [],
                                "engine": engine,
                                "created_at": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
                            },
                        }
                    )
                    return account

                # Duplicate address or key: a skip, not a failure.
                self.store.attempt(email, engine, proxy, "duplicate")
                self.log(f"#{index + 1} {email} already stored, skipped", level="warn")
                return account

            except EmailTaken as exc:
                last_error = exc
                self.store.attempt(email, engine, proxy, "email_taken", str(exc))
                self.log(f"#{index + 1}.{attempt} {email} already registered, rotating", level="warn")
            except EmailRejected as exc:
                last_error = exc
                self.store.attempt(email, engine, proxy, "email_rejected", str(exc))
                self.log(f"#{index + 1}.{attempt} address rejected ({exc}), rotating", level="warn")
            except (CaptchaRequired, CaptchaError) as exc:
                last_error = exc
                self.store.attempt(email, engine, proxy, "captcha", str(exc))
                if attempt < self.cfg.attempts:
                    wait = random.uniform(15, 30)
                    self.log(
                        f"#{index + 1}.{attempt} turnstile rejected ({exc}), sleeping {wait:.0f}s",
                        level="warn",
                    )
                    time.sleep(wait)
            except VerificationPending as exc:
                last_error = exc
                self.store.attempt(email, engine, proxy, "verification_pending", str(exc))
                self.log(f"#{index + 1}.{attempt} verification mail missing ({exc})", level="warn")
            except RateLimited as exc:
                last_error = exc
                self.store.attempt(email, engine, proxy, "rate_limited", str(exc))
                if attempt < self.cfg.attempts:
                    wait = random.uniform(20, 45)
                    self.log(f"#{index + 1}.{attempt} rate limited, sleeping {wait:.0f}s", level="warn")
                    time.sleep(wait)
            except Blocked as exc:
                last_error = exc
                self.store.attempt(email, engine, proxy, "blocked", str(exc))
                if attempt < self.cfg.attempts:
                    wait = random.uniform(20, 40)
                    self.log(
                        f"#{index + 1}.{attempt} edge blocked the request, sleeping {wait:.0f}s",
                        level="warn",
                    )
                    time.sleep(wait)
            except SignupError as exc:
                last_error = exc
                self.store.attempt(email, engine, proxy, "error", str(exc))
                self.log(f"#{index + 1}.{attempt} {type(exc).__name__}: {exc}", level="warn")
                self._sleep(2, 5)
            finally:
                provider.close()

        kind = getattr(last_error, "kind", "error") if last_error else "unknown"
        reason = str(last_error) if last_error else "signup failed without a recorded cause"
        self.store.record_failure(email=email, domain=domain, kind=str(kind), reason=reason, round_no=round_no)
        self.emit(
            {
                "type": "failed",
                "index": index + 1,
                "email": email,
                "domain": domain,
                "kind": str(kind),
                "reason": reason,
                "round": round_no,
            }
        )
        raise last_error or SignupError(reason)


    # ------------------------------------------------------------- internals
    def _browser_signup(
        self,
        email: str,
        persona: Persona,
        proxy: str,
        provider: Provider,
        box: Any,
    ) -> dict[str, Any]:
        """指纹浏览器路径：同一套表单/验证/领 Key，只是由真实浏览器执行。"""
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
                log=lambda message: self.log(message, level="debug"),
            ) as browser:
                account = browser.signup_flow(
                    email,
                    persona.password,
                    name=persona.name,
                    verify=self.cfg.verify_email,
                    provider=provider,
                    box=box,
                    verify_timeout=self.cfg.mail.verify_timeout,
                    key_name=self.cfg.key_name(email),
                    keys=self.cfg.keys_per_account,
                    log=lambda message: self.log(message, level="debug"),
                )
        except BrowserUnavailable as exc:
            raise SignupError(
                f"指纹浏览器不可用：{exc}（pip install camoufox 或 playwright install chromium）"
            ) from exc
        return account


def doctor(cfg: Config) -> dict[str, Any]:
    """Environment report; safe to run anywhere, touches each dependency once."""
    report: dict[str, Any] = {"config": cfg.describe(), "checks": {}}
    checks = report["checks"]
    proxy = cfg.proxies[0] if cfg.proxies else ""

    try:
        checks["dashboard"] = probe(cfg.base_url, proxy=proxy)
    except SignupError as exc:
        checks["dashboard"] = f"unreachable: {exc}"

    try:
        provider = build_provider(cfg.mail, proxy=proxy)
        box = provider.allocate()
        checks["mailbox"] = f"{box.provider}: {box.address}"
        provider.close()
    except Exception as exc:  # noqa: BLE001
        checks["mailbox"] = f"failed: {exc}"

    try:
        import playwright  # noqa: F401

        checks["playwright"] = "ok"
    except Exception as exc:  # noqa: BLE001
        checks["playwright"] = f"missing: {exc}"

    checks["browser_stacks"] = available_stacks()
    checks["engine"] = cfg.engine
    checks["cdp"] = cfg.browser.cdp or "(launch own)"

    checks["captcha_provider"] = cfg.captcha.provider
    checks["captcha_key"] = "set" if cfg.captcha.api_key else "missing"
    return report
