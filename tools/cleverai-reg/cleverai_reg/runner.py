"""Concurrent signup loop with per-failure policy (rotate address / engine / proxy).

The interesting part is not the HTTP; it is the pacing. Clever AI Hub's signup is
Firebase, so the things that get you blocked are, in order:

1. one IP creating many users in a row      -> per-proxy concurrency = 1 + jittered delay
2. one mailbox domain creating many users   -> per-domain burst budget + cooldown
3. a reCAPTCHA score that reads as a bot    -> shared App Check token + real fingerprints
4. an account that never does anything      -> warmup chat right after creation

`Runner` owns all four. `protocol` and `browser` are just the two ways to reach
the same result; the scheduler does not care which one answered.
"""

from __future__ import annotations

import random
import threading
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass, field
from typing import Any, Callable, Protocol

from .appcheck import AppCheckVault
from .browser import BrowserUnavailable, FingerprintBrowser, available_stacks
from .config import Config
from .errors import (
    AppCheckRequired,
    Blocked,
    CaptchaRequired,
    EmailRejected,
    EmailTaken,
    NoCredit,
    RateLimited,
    SignupError,
    Transport,
    VerificationPending,
)
from .identity import Persona, make_persona, plus_tag
from .mailbox import Provider, build_provider
from .protocol import CleverAI, dialog_message, probe as protocol_probe, text_of
from .store import Store

_print_lock = threading.Lock()


class MailboxFactory(Protocol):
    """Test seam: production builds providers from config, tests inject fakes."""

    def __call__(self, cfg: Config, proxy: str) -> Provider: ...


@dataclass
class DomainState:
    """One mailbox domain's live budget."""

    domain: str
    burst: int = 0
    cooldown_until: float = 0.0


@dataclass
class ProxyState:
    """One exit's live budget."""

    proxy: str
    failures: int = 0
    cooldown_until: float = 0.0
    active: int = 0
    lock: threading.Lock = field(default_factory=threading.Lock)


class Runner:
    def __init__(
        self,
        cfg: Config,
        store: Store,
        *,
        on_event: Callable[[dict[str, Any]], None] | None = None,
        quiet: bool = False,
        mail_factory: MailboxFactory | None = None,
        session_factory: Callable[..., Any] | None = None,
        browser_factory: Callable[..., Any] | None = None,
        vault: AppCheckVault | None = None,
    ) -> None:
        self.cfg = cfg
        self.store = store
        self.on_event = on_event
        self.quiet = quiet
        self.mail_factory = mail_factory or (lambda c, proxy: build_provider(c.mail, proxy=proxy))
        self.session_factory = session_factory or (lambda **kw: CleverAI(**kw))
        self.browser_factory = browser_factory or (lambda **kw: FingerprintBrowser(**kw))
        self.vault = vault or AppCheckVault(
            provider=cfg.appcheck.provider,
            api_key=cfg.appcheck.api_key,
            proxy=cfg.proxy_for(0),
            headless=cfg.browser.headless,
            log=self.log,
        )
        if cfg.appcheck.token:
            self.vault.seed(cfg.appcheck.token, cfg.appcheck.ttl)
        self.cfg.output_dir.mkdir(parents=True, exist_ok=True)
        self.log_path = self.cfg.output_dir / "run.log"
        self._counter = 0
        self._counter_lock = threading.Lock()
        self._domains: dict[str, DomainState] = {}
        self._proxies: dict[str, ProxyState] = {}
        self._state_lock = threading.Lock()
        self._appcheck_lock = threading.Lock()

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

    def _next_index(self) -> int:
        with self._counter_lock:
            index = self._counter
            self._counter += 1
        return index

    @staticmethod
    def _sleep(low: float, high: float) -> None:
        time.sleep(max(0.0, random.uniform(low, high)))

    # ------------------------------------------------------- proxy scheduling
    def _proxy_state(self, proxy: str) -> ProxyState:
        with self._state_lock:
            state = self._proxies.get(proxy)
            if state is None:
                state = ProxyState(proxy=proxy)
                self._proxies[proxy] = state
            return state

    def _acquire_proxy(self, index: int) -> ProxyState:
        """Pick the least-busy healthy exit and reserve a slot on it."""
        now = time.time()
        pool = self.cfg.proxies or [""]
        candidates = []
        for offset in range(len(pool)):
            proxy = pool[(index + offset) % len(pool)]
            state = self._proxy_state(proxy)
            if state.cooldown_until <= now and state.active < self.cfg.antiban.per_proxy_concurrency:
                candidates.append(state)
        if not candidates:
            # Everything is busy or cooling: wait for the soonest free slot.
            soonest = min(self._proxy_state(p).cooldown_until for p in pool)
            wait = max(1.0, soonest - now) if soonest > now else 2.0
            self.log("warn", f"所有出口都在冷却/占用，等待 {wait:.0f}s")
            time.sleep(min(wait, 60.0))
            proxy = pool[index % len(pool)]
            state = self._proxy_state(proxy)
        else:
            state = candidates[0]
        state.active += 1
        return state

    def _release_proxy(self, state: ProxyState, *, ok: bool) -> None:
        state.active = max(0, state.active - 1)
        if ok:
            state.failures = 0
            return
        state.failures += 1
        if state.failures >= self.cfg.antiban.max_failures_per_proxy:
            state.cooldown_until = time.time() + self.cfg.antiban.proxy_cooldown
            self.log(
                "warn",
                f"出口 {state.proxy or 'direct'} 连续失败 {state.failures} 次，冷却 "
                f"{self.cfg.antiban.proxy_cooldown:.0f}s",
            )

    # ------------------------------------------------------ domain scheduling
    def _pick_domain(self, provider: Provider) -> str:
        configured = list(self.cfg.domains()) or [""]
        for domain in provider.domains():
            if domain and domain not in configured:
                configured.append(domain)
        now = time.time()
        best = ""
        best_score = -1.0
        for domain in configured:
            state = self._domains.setdefault(domain, DomainState(domain=domain))
            if state.cooldown_until > now:
                continue
            score = max(0.0, self.cfg.antiban.per_domain_burst - state.burst)
            if score > best_score:
                best, best_score = domain, score
        if best_score <= 0:
            # Every domain is cooling or spent: use the one that frees up first.
            soonest = min(self._domains[d].cooldown_until for d in configured if d in self._domains)
            wait = max(1.0, soonest - now)
            self.log("warn", f"邮箱域名都在冷却，等待 {wait:.0f}s 再发下一个")
            time.sleep(min(wait, 90.0))
            best = configured[0]
        return best

    def _domain_ok(self, domain: str) -> None:
        if not domain:
            return
        state = self._domains.setdefault(domain, DomainState(domain=domain))
        state.burst += 1
        self.store.domain_ok(domain)
        if state.burst >= self.cfg.antiban.per_domain_burst:
            state.burst = 0
            state.cooldown_until = time.time() + self.cfg.antiban.domain_cooldown
            self.log("info", f"域名 {domain} 用完本轮配额，冷却 {self.cfg.antiban.domain_cooldown:.0f}s")

    def _domain_failed(self, domain: str) -> None:
        if not domain:
            return
        state = self._domains.setdefault(domain, DomainState(domain=domain))
        state.cooldown_until = time.time() + self.cfg.antiban.domain_cooldown
        self.store.domain_failed(domain, self.cfg.antiban.domain_cooldown)

    # ------------------------------------------------------------- app check
    def appcheck(self, proxy: str = "") -> str:
        """Serialize minting: one browser boot serves the whole batch."""
        with self._appcheck_lock:
            return self.vault.get(proxy=proxy)

    # --------------------------------------------------------------- engines
    def _engine_order(self) -> list[str]:
        configured = (self.cfg.engine or "auto").strip().lower()
        if configured in ("protocol", "browser"):
            return [configured]
        order = ["protocol"]
        if available_stacks():
            order.append("browser")
        return order

    # ------------------------------------------------------------------- work
    def run(self, count: int | None = None) -> dict[str, Any]:
        target = count or self.cfg.count
        started = time.time()
        self.emit(
            {
                "type": "start",
                "count": target,
                "concurrency": self.cfg.concurrency,
                "engine": self.cfg.engine,
                "mail_provider": self.cfg.mail.provider,
                "mail_domains": self.cfg.domains(),
                "appcheck": self.cfg.appcheck.provider,
                "base_url": self.cfg.base_url,
            }
        )
        created: list[dict[str, Any]] = []
        failures: list[dict[str, Any]] = []
        pending = list(range(target))

        for round_no in range(max(1, self.cfg.retry_rounds + 1)):
            if not pending:
                break
            if round_no:
                self.log("warn", f"第 {round_no} 轮重试：{len(pending)} 个待补")
                self.emit({"type": "retry", "round": round_no, "count": len(pending)})
            results = self._run_batch(pending, round_no)
            pending = []
            for index, (ok, payload) in results:
                if ok:
                    created.append(payload)
                else:
                    failures.append(payload)
                    if payload.get("retryable"):
                        pending.append(index)
            self._sleep(self.cfg.antiban.min_delay / 2, self.cfg.antiban.max_delay / 2)

        summary = {
            "requested": target,
            "created": len(created),
            "failed": len(failures),
            "seconds": round(time.time() - started, 1),
            "accounts": created,
            "failures": failures,
            "domains": self.store.domain_table(),
            "failureKinds": self.store.failure_summary(),
        }
        self.emit({"type": "done", "summary": summary})
        return summary

    def _run_batch(self, indexes: list[int], round_no: int) -> list[tuple[int, tuple[bool, dict[str, Any]]]]:
        out: list[tuple[int, tuple[bool, dict[str, Any]]]] = []
        workers = max(1, min(self.cfg.concurrency, len(indexes)))
        with ThreadPoolExecutor(max_workers=workers) as pool:
            futures = {pool.submit(self._one, index, round_no): index for index in indexes}
            for future in as_completed(futures):
                index = futures[future]
                try:
                    out.append((index, (True, future.result())))
                except SignupError as exc:
                    retryable = isinstance(
                        exc, (RateLimited, Transport, AppCheckRequired, CaptchaRequired, Blocked, VerificationPending)
                    )
                    self.log("warn", f"失败 #{index}: {exc.kind} {exc}")
                    out.append(
                        (
                            index,
                            (
                                False,
                                {
                                    "index": index,
                                    "kind": exc.kind,
                                    "reason": str(exc)[:300],
                                    "retryable": retryable,
                                },
                            ),
                        )
                    )
                except Exception as exc:  # noqa: BLE001 - never kill the batch
                    self.log("error", f"未捕获异常 #{index}: {type(exc).__name__}: {exc}")
                    out.append(
                        (
                            index,
                            (
                                False,
                                {"index": index, "kind": "error", "reason": f"{type(exc).__name__}: {exc}"[:300], "retryable": False},
                            ),
                        )
                    )
        return out

    def _one(self, index: int, round_no: int) -> dict[str, Any]:
        index = self._next_index() if round_no == 0 else index
        persona = make_persona()
        proxy_state = self._acquire_proxy(index)
        proxy = proxy_state.proxy
        engine = ""
        email = ""
        domain = ""
        provider: Provider | None = None
        try:
            provider = self.mail_factory(self.cfg, proxy)
            domain = self._pick_domain(provider)
            local = persona.local_part if provider.name in ("imap", "fixed") else ""
            box = provider.allocate(local_part=local or plus_tag(), domain=domain)
            email = box.address
            domain = email.rsplit("@", 1)[-1].lower()

            self.emit({"type": "attempt", "index": index, "email": email, "domain": domain, "provider": provider.name, "proxy": bool(proxy)})
            self.log("info", f"#{index} 注册 {email}（出口 {proxy or 'direct'}）")

            account: dict[str, Any] = {}
            last_error: SignupError | None = None
            for candidate in self._engine_order():
                try:
                    if candidate == "protocol":
                        account = self._signup_protocol(persona, email, proxy, provider, box)
                    else:
                        account = self._signup_browser(persona, email, proxy)
                    engine = candidate
                    last_error = None
                    break
                except BrowserUnavailable as exc:
                    self.log("warn", f"浏览器引擎不可用：{exc}")
                    last_error = exc
                    continue
                except (AppCheckRequired, CaptchaRequired) as exc:
                    self.log("warn", f"{candidate} 引擎被人机校验挡下：{exc}")
                    last_error = exc
                    continue
                except (Blocked, Transport) as exc:
                    self.log("warn", f"{candidate} 引擎被出口拖累：{exc}")
                    last_error = exc
                    continue
            if last_error is not None:
                raise last_error

            account.setdefault("email", email)
            account.setdefault("password", persona.password)
            account.setdefault("name", persona.name)
            account.setdefault("engine", engine)
            account.setdefault("createdAt", time.strftime("%Y-%m-%dT%H:%M:%S%z"))
            account.setdefault("email_verified", False)

            if self.cfg.verify_email and provider is not None:
                try:
                    account["email_verified"] = self._verify(account, provider, box, proxy)
                except VerificationPending as exc:
                    self.log("warn", f"邮箱验证未完成（账号可用）：{exc}")

            if self.cfg.antiban.warmup and account.get("refresh_token"):
                self._warmup(account, proxy)

            self.store.add(account, engine=engine, proxy=proxy)
            self.store.attempt(email, engine, proxy, "ok")
            self._domain_ok(domain)
            self._release_proxy(proxy_state, ok=True)

            record = self._pool_record(account)
            self.emit({"type": "account", "index": index, "account": record, "domain": domain, "engine": engine})
            self.log("info", f"+ {email} 入池（engine={engine}，uid={account.get('uid', '')[:8]}…）")
            self._push(record)
            return {"email": email, "engine": engine, "uid": account.get("uid", "")}
        except SignupError as exc:
            self._release_proxy(proxy_state, ok=False)
            if isinstance(exc, (EmailRejected, EmailTaken, RateLimited, Blocked, CaptchaRequired)):
                self._domain_failed(domain or "")
            self.store.attempt(email or f"#{index}", engine or "none", proxy, "failed", str(exc))
            self.store.record_failure(
                email=email or f"#{index}",
                domain=domain,
                kind=exc.kind,
                reason=str(exc),
                round_no=round_no,
            )
            self.emit({"type": "failed", "index": index, "email": email, "kind": exc.kind, "reason": str(exc)[:300]})
            raise
        finally:
            if provider is not None:
                try:
                    provider.close()
                except Exception:  # noqa: BLE001
                    pass

    # ------------------------------------------------------------- 协议注册
    def _signup_protocol(
        self,
        persona: Persona,
        email: str,
        proxy: str,
        provider: Provider,
        box: Any,
    ) -> dict[str, Any]:
        token = self.appcheck(proxy)
        client = self.session_factory(
            proxy=proxy,
            timeout=self.cfg.timeout,
            language=self.cfg.language,
            user_agent=self.cfg.user_agent or None,
            app_check=token,
            appcheck_getter=lambda: self.appcheck(proxy),
        )
        try:
            client.signup(email, persona.password)
            profile = client.create_profile()
            wallet = profile.get("wallet") or {}
            return {
                "email": email,
                "password": persona.password,
                "name": persona.name,
                "uid": client.uid,
                "id_token": client.id_token,
                "refresh_token": client.refresh_token,
                "app_check": client.app_check,
                "app_check_exp": time.time() + self.cfg.appcheck.ttl,
                "wallet": wallet,
                "device": client.device,
                "profile": profile,
            }
        finally:
            client.close()

    # ------------------------------------------------------------- 浏览器注册
    def _signup_browser(self, persona: Persona, email: str, proxy: str) -> dict[str, Any]:
        with self.browser_factory(
            stack=self.cfg.browser.stack,
            headless=self.cfg.browser.headless,
            proxy=proxy,
            humanize=self.cfg.browser.humanize,
            geoip=self.cfg.browser.geoip,
            cdp=self.cfg.browser.cdp,
            profile_dir=self.cfg.browser.profile_dir,
            output_dir=self.cfg.output_dir,
            timeout=self.cfg.browser.timeout,
            log=self.log,
        ) as browser:
            result = browser.signup(email, persona.password)
            return {
                "email": email,
                "password": persona.password,
                "name": persona.name,
                "uid": result.uid,
                "id_token": result.id_token,
                "refresh_token": result.refresh_token,
                "app_check": result.app_check,
                "app_check_exp": time.time() + result.app_check_ttl,
                "wallet": (result.profile or {}).get("wallet") or {},
                "device": result.device,
                "profile": result.profile,
            }

    # ------------------------------------------------------------- 后续动作
    def _verify(self, account: dict[str, Any], provider: Provider, box: Any, proxy: str) -> bool:
        """Firebase 的验证邮件：发信 → 轮询 → 消费 oobCode。"""
        client = self.session_factory(
            proxy=proxy,
            timeout=self.cfg.timeout,
            app_check=account.get("app_check") or "",
            appcheck_getter=lambda: self.appcheck(proxy),
        )
        try:
            client.id_token = account.get("id_token") or ""
            client.refresh_token = account.get("refresh_token") or ""
            client.uid = account.get("uid") or ""
            client.email = account.get("email") or ""
            client.send_verification()
            link = provider.wait_for_link(box, timeout=self.cfg.mail.verify_timeout)
            if not link:
                raise VerificationPending(f"{account.get('email')} 在 {self.cfg.mail.verify_timeout:.0f}s 内没收到验证信")
            client.apply_verification_link(link)
            info = client.account_info()
            return bool(info.get("emailVerified"))
        finally:
            client.close()

    def _warmup(self, account: dict[str, Any], proxy: str) -> None:
        """新号先聊一句：把「注册完就没动静」的时间窗抹掉。"""
        client = self.session_factory(
            proxy=proxy,
            timeout=self.cfg.timeout,
            app_check=account.get("app_check") or "",
            appcheck_getter=lambda: self.appcheck(proxy),
        )
        try:
            client.id_token = account.get("id_token") or ""
            client.refresh_token = account.get("refresh_token") or ""
            client.uid = account.get("uid") or ""
            client.email = account.get("email") or ""
            wallet = account.get("wallet") or {}
            events = client.chat(
                [dialog_message("Hi! What can you help me with today?")],
                engine=self.cfg.chat_engine,
                cost=2,
                wallet=wallet,
                timeout=90.0,
            )
            reply = ""
            for event in events:
                reply += text_of(event)
                if event.get("finishReason") in ("stop", "error"):
                    break
            if reply:
                self.log("debug", f"预热回复 {len(reply)} 字符")
        except (SignupError, Transport) as exc:
            self.log("warn", f"预热失败（不影响入池）：{exc}")
        finally:
            client.close()

    # ---------------------------------------------------------------- 入池
    def _pool_record(self, account: dict[str, Any]) -> dict[str, Any]:
        return {
            "email": account.get("email", ""),
            "password": account.get("password", ""),
            "name": account.get("name", ""),
            "uid": account.get("uid", ""),
            "idToken": account.get("id_token", ""),
            "refreshToken": account.get("refresh_token", ""),
            "appCheck": account.get("app_check", ""),
            "appCheckExp": account.get("app_check_exp", 0),
            "wallet": account.get("wallet") or {},
            "device": account.get("device") or {},
            "emailVerified": account.get("email_verified"),
            "engine": account.get("engine", "protocol"),
            "createdAt": account.get("createdAt", ""),
            "status": "active",
        }

    def _push(self, record: dict[str, Any]) -> None:
        if not (self.cfg.push_url and self.cfg.push_token):
            return
        from .push import push_account

        try:
            result = push_account(self.cfg.push_url, self.cfg.push_token, record)
            self.emit({"type": "pushed", "email": record["email"], "gatewayKey": (result.get("gatewayKey") or {}).get("prefix", "")})
        except SignupError as exc:
            self.emit({"type": "push_error", "email": record["email"], "message": str(exc)[:200]})
            self.log("warn", f"回推网关失败：{exc}")

    # ---------------------------------------------------------------- doctor
    def doctor(self) -> dict[str, Any]:
        out: dict[str, Any] = {"config": self.cfg.describe()}
        out["protocol"] = protocol_probe(proxy=self.cfg.proxy_for(0))
        out["browser_stacks"] = available_stacks()
        out["appcheck"] = self.vault.stats
        if self.cfg.appcheck.provider != "manual":
            try:
                token = self.vault.get(proxy=self.cfg.proxy_for(0))
                out["appcheck"]["ok"] = True
                out["appcheck"]["len"] = len(token)
            except SignupError as exc:
                out["appcheck"]["ok"] = False
                out["appcheck"]["error"] = f"{exc.kind}: {str(exc)[:200]}"
        out["domains"] = self.store.domain_table()
        out["accounts"] = self.store.count()
        return out
