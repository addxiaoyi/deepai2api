"""Runner: pacing, proxy health, retries, pool writes, gateway push.

Anti-ban rules that actually matter for Poe (they gate on IP/device reputation,
not on email domains):

  * one account per proxy per `account_cooldown` window, enforced here;
  * a proxy that answers `automated_request_detected` is benched for 15 min;
  * every attempt gets a fresh fingerprint + fresh persona, never a reused jar;
  * `cloudflare` failures do not retry the same proxy immediately;
  * the session cookie (`p-b`) is stored, so a created account is never
    re-authenticated from a different IP afterwards.
"""

from __future__ import annotations

import random
import threading
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from typing import Any, Callable

from .config import Config
from .errors import (
    AutomatedRequest,
    CloudflareBlocked,
    CodeRejected,
    EmailExists,
    EmailRejected,
    PoeError,
    PhoneRequired,
)
from .identity import make_persona
from .mail import poll_code, provider_for
from .net import build_client, request
from .pool import Pool

_print_lock = threading.Lock()


class ProxyHealth:
    def __init__(self) -> None:
        self.state: dict[str, dict[str, float]] = {}
        self.lock = threading.Lock()

    def ready_at(self, proxy: str) -> float:
        with self.lock:
            return self.state.get(proxy, {}).get("ready_at", 0.0)

    def note_success(self, proxy: str, cooldown: float) -> None:
        with self.lock:
            row = self.state.setdefault(proxy, {})
            row["ready_at"] = time.time() + cooldown
            row["signups"] = row.get("signups", 0) + 1

    def bench(self, proxy: str, seconds: float) -> None:
        with self.lock:
            row = self.state.setdefault(proxy, {})
            row["ready_at"] = max(row.get("ready_at", 0.0), time.time() + seconds)
            row["benched"] = row.get("benched", 0) + 1

    def snapshot(self) -> dict[str, dict[str, float]]:
        with self.lock:
            return {k: dict(v) for k, v in self.state.items()}


class Runner:
    def __init__(
        self,
        cfg: Config,
        pool: Pool,
        *,
        on_event: Callable[[dict[str, Any]], None] | None = None,
        quiet: bool = False,
        push_url: str = "",
        push_token: str = "",
    ) -> None:
        self.cfg = cfg
        self.pool = pool
        self.on_event = on_event
        self.quiet = quiet
        self.push_url = push_url
        self.push_token = push_token
        self.health = ProxyHealth()
        self.cfg.output_dir.mkdir(parents=True, exist_ok=True)
        self.log_path = self.cfg.output_dir / "poe-run.log"
        self.screens = self.cfg.output_dir / "screenshots"
        self._counter = 0
        self._lock = threading.Lock()

    # --------------------------------------------------------------- logging
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

    # ---------------------------------------------------------------- helpers
    def _next_proxy(self) -> str:
        with self._lock:
            index = self._counter
            self._counter += 1
        proxy = self.cfg.proxy_for(index) if self.cfg.proxy_rotate else (
            self.cfg.proxies[0] if self.cfg.proxies else ""
        )
        wait = self.health.ready_at(proxy) - time.time()
        if wait > 0:
            self.log(f"proxy cooling down {wait:.0f}s before next signup", level="warn")
            time.sleep(min(wait, 900))
        return proxy

    def _push(self, account: dict[str, Any]) -> None:
        if not (self.push_url and self.push_token):
            return
        payload = {
            "email": account.get("email"),
            "status": account.get("status"),
            "cookies": account.get("cookies") or {},
            "engine": account.get("engine"),
            "phone": account.get("phone") or "",
        }
        client = build_client(timeout=30)
        try:
            response = request(
                client,
                "POST",
                f"{self.push_url.rstrip('/')}/api/poe/accounts",
                json=payload,
                headers={"Authorization": f"Bearer {self.push_token}"},
            )
            if response.status_code >= 400:
                self.log(f"push rejected (HTTP {response.status_code})", level="warn")
            else:
                self.log(f"pushed {account.get('email')} to the Poe pool")
        except PoeError as exc:
            self.log(f"push failed: {exc}", level="warn")
        finally:
            client.close()

    # ------------------------------------------------------------------- run
    def run(self, count: int | None = None) -> dict[str, Any]:
        target = count or self.cfg.count
        started = time.time()
        created: list[dict[str, Any]] = []
        pending = list(range(target))
        failures: list[str] = []

        self.emit(
            {
                "type": "start",
                "engine": self.cfg.engine,
                "count": target,
                "concurrency": self.cfg.concurrency,
                "sms": self.cfg.sms.provider,
                "proxies": len(self.cfg.proxies),
                "retry_rounds": self.cfg.retry_rounds,
            }
        )
        self.log(
            f"start engine={self.cfg.engine} count={target} concurrency={self.cfg.concurrency} "
            f"proxies={len(self.cfg.proxies)} sms={self.cfg.sms.provider}"
        )

        for round_no in range(self.cfg.retry_rounds + 1):
            if not pending:
                break
            if round_no:
                self.emit({"type": "retry", "round": round_no, "count": len(pending)})
                self.log(f"retry round {round_no}: {len(pending)} account(s)", level="warn")
                failures.clear()
                time.sleep(random.uniform(20, 40))

            still_failing: list[int] = []
            with ThreadPoolExecutor(max_workers=max(1, self.cfg.concurrency)) as pool:
                futures = {pool.submit(self.signup, index, round_no): index for index in pending}
                for future in as_completed(futures):
                    index = futures[future]
                    try:
                        account = future.result()
                        created.append(account)
                        self.log(f"#{index + 1} ok {account['email']} ({account['status']})")
                    except Exception as exc:  # noqa: BLE001
                        still_failing.append(index)
                        failures.append(f"#{index + 1} {type(exc).__name__}: {exc}")
                        self.log(f"#{index + 1} failed {type(exc).__name__}: {exc}", level="error")
            pending = sorted(still_failing)

        jsonl = self.cfg.output_dir / "poe-accounts.jsonl"
        rows = self.pool.export_jsonl(jsonl)
        summary = {
            "requested": target,
            "created": len(created),
            "failed": len(pending),
            "stored": self.pool.count(),
            "statuses": self.pool.counts_by_status(),
            "seconds": round(time.time() - started, 1),
            "jsonl": str(jsonl),
            "jsonl_rows": rows,
            "failures": failures,
            "proxies": self.health.snapshot(),
        }
        self.emit({"type": "done", "summary": summary})
        self.log(
            f"done created={summary['created']} failed={summary['failed']} "
            f"stored={summary['stored']} in {summary['seconds']}s"
        )
        return summary

    # -------------------------------------------------------------- one unit
    def signup(self, index: int, round_no: int = 0) -> dict[str, Any]:
        from .browser import PoeBrowser, PoeSession
        from .protocol import PoeProtocol

        persona = make_persona(random.Random())
        proxy = self._next_proxy()
        provider = provider_for(self.cfg.mail, proxy=proxy)
        email = ""
        engine = self.cfg.engine
        box = None

        try:
            box = provider.allocate(persona.local_part)
            email = box.address
            self.emit(
                {
                    "type": "attempt",
                    "index": index + 1,
                    "attempt": round_no + 1,
                    "email": email,
                    "engine": engine,
                    "proxy": proxy,
                }
            )
            self.log(f"#{index + 1} {email} via {engine} (proxy {'yes' if proxy else 'direct'})")

            session = PoeSession(
                proxy=proxy,
                headless=self.cfg.headless,
                geoip=self.cfg.geoip,
                locale=self.cfg.language,
                os_list=self.cfg.device_os,
                screenshots=self.screens,
                log=lambda m: self.log(m, level="debug"),
                rng=random.Random(),
            ).open()

            try:
                if engine in ("protocol", "auto"):
                    try:
                        account = self._protocol_step(session, email, provider, box, proxy)
                        engine = "protocol"
                    except (PoeError,) as exc:
                        if engine != "auto" or isinstance(exc, (AutomatedRequest, CloudflareBlocked, EmailExists, EmailRejected)):
                            raise
                        self.log(f"protocol step failed ({exc}); falling back to the DOM engine", level="warn")
                        account = self._browser_step(session, email, provider, box)
                        engine = "browser"
                else:
                    account = self._browser_step(session, email, provider, box)
                    engine = "browser"

                account.update(
                    {
                        "email": email,
                        "engine": engine,
                        "proxy": proxy,
                        "cookies": session.cookies(),
                        "formkey": session.formkey,
                    }
                )
            finally:
                session.close()

            self.pool.upsert(account)
            self.health.note_success(proxy, self.cfg.account_cooldown)
            self._push(account)
            self.emit(
                {
                    "type": "account",
                    "engine": engine,
                    "account": {
                        "email": email,
                        "status": account.get("status"),
                        "phone": account.get("phone") or "",
                    },
                }
            )
            return account

        except AutomatedRequest as exc:
            self.health.bench(proxy, 900)
            self.pool.event(email, "automated", str(exc))
            raise
        except CloudflareBlocked as exc:
            self.health.bench(proxy, 600)
            self.pool.event(email, "cloudflare", str(exc))
            raise
        except EmailExists as exc:
            self.pool.event(email, "exists", str(exc))
            raise
        except EmailRejected as exc:
            self.pool.event(email, "email_rejected", str(exc))
            raise
        except (CodeRejected, PhoneRequired) as exc:
            self.pool.event(email, "code_or_phone", str(exc))
            raise
        except PoeError as exc:
            self.pool.event(email, "error", f"{type(exc).__name__}: {exc}")
            raise
        finally:
            try:
                provider.close()
            except Exception:  # noqa: BLE001
                pass

    # ------------------------------------------------------------ engines
    def _protocol_step(self, session, email: str, provider, box, proxy: str) -> dict[str, Any]:
        from .protocol import PoeProtocol

        protocol = PoeProtocol(session, log=lambda m: self.log(m, level="debug"))
        protocol.send_code(email)
        code = poll_code(provider, box, timeout=max(120.0, self.cfg.timeout * 2))
        if not code:
            raise PoeError("verification code never arrived")
        self.log(f"#{email} code {code}")
        try:
            result = protocol.signup_with_code(email, code)
        except PhoneRequired as exc:
            # Email is proven and the session is warm; Poe just refuses to create
            # the account without a recently verified phone. Keep the row: an SMS
            # rental added later finishes exactly this record.
            self.log(f"{email} verified email, blocked on phone: {exc}", level="warn")
            state = session.state()
            phone_gate = any(item[0] == "tel" for item in state.get("inputs", []))
            return {
                "status": "phone_required" if phone_gate else "email_verified",
                "phone": "",
                "sms_provider": "",
                "note": "email verified; Poe requires a recently verified phone",
            }
        return self._finish(session, email, result, proxy)

    def _browser_step(self, session, email: str, provider, box) -> dict[str, Any]:
        from .browser import PoeBrowser

        dom = PoeBrowser(session, log=lambda m: self.log(m, level="debug"))
        dom.submit_email(email)
        state = session.state()
        if "not valid" in (state.get("body") or "").lower():
            raise EmailRejected("Poe rejected the address as invalid")
        code = poll_code(provider, box, timeout=max(120.0, self.cfg.timeout * 2))
        if not code:
            raise PoeError("verification code never arrived")
        dom.enter_code(code)
        state = session.state()
        return self._finish(session, email, {"state": state}, "", dom=dom)

    # ------------------------------------------------------------- phone leg
    def _finish(self, session, email: str, result: dict[str, Any], proxy: str, dom=None) -> dict[str, Any]:
        from .sms import build_provider

        state = session.state()
        phone_gate = any(item[0] == "tel" for item in state.get("inputs", []))
        if not phone_gate:
            return {"status": "active", "phone": "", "sms_provider": ""}

        sms = build_provider(self.cfg.sms, proxy=proxy)
        if not self.cfg.sms.enabled:
            self.log(f"{email} reached the phone gate (no SMS provider configured)", level="warn")
            return {"status": "phone_required", "phone": "", "sms_provider": ""}

        rental = sms.rent()
        self.log(f"{email} renting {rental.phone} via {rental.provider}")
        browser = dom
        if browser is None:
            from .browser import PoeBrowser

            browser = PoeBrowser(session)
        if not browser.add_phone(rental.phone):
            sms.release(rental)
            raise PhoneRequired("could not submit the phone number")
        code = sms.wait_code(rental)
        session.set_field("input[placeholder=Code]", code, delay=90)
        session.click_text("Verify")
        session.page.wait_for_timeout(6000)
        sms.finish(rental)
        state = session.state()
        still_gate = any(item[0] == "tel" for item in state.get("inputs", []))
        return {
            "status": "phone_required" if still_gate else "active",
            "phone": rental.phone,
            "sms_provider": rental.provider,
        }
