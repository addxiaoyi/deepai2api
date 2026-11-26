"""通用注册循环：串行出口 + 抖动 + 失败分类（所有 OpenAI 兼容线共用）。"""

from __future__ import annotations

import random
import threading
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from typing import Any, Callable, Protocol

from .config import Config
from .errors import (
    Blocked,
    CaptchaRequired,
    LineError,
    RateLimited,
    Transport,
    VerificationPending,
)
from .identity import make_persona
from .mailbox import Provider, build_provider
from .net import build_client, request
from .session import Session, anon_session, make_session
from .site import Site
from .store import Store

_print_lock = threading.Lock()


class MailboxFactory(Protocol):
    def __call__(self, cfg: Config, proxy: str) -> Provider: ...


class Runner:
    """一条线的注册执行器：`site` 决定"往哪打"，其余逻辑全通用。"""

    def __init__(
        self,
        site: Site,
        cfg: Config,
        store: Store,
        *,
        on_event: Callable[[dict[str, Any]], None] | None = None,
        quiet: bool = False,
        mail_factory: MailboxFactory | None = None,
        session_factory: Callable[..., Any] | None = None,
        captcha_hook: Callable[[str, str], dict[str, str]] | None = None,
    ) -> None:
        self.site = site
        self.cfg = cfg
        self.store = store
        self.on_event = on_event
        self.quiet = quiet
        self.mail_factory = mail_factory or (lambda c, proxy: build_provider(c.mail, proxy=proxy))
        # 按 site.flow 选会话实现：better-auth / code-signup
        self.session_factory = session_factory or (lambda **kw: make_session(site, **kw))
        # 有验证码门槛的线：注册前用它拿 ticket/randstr（manual / browser）
        self.captcha_hook = captcha_hook
        cfg.output_dir.mkdir(parents=True, exist_ok=True)
        self.log_path = cfg.output_dir / "run.log"
        self._counter = 0
        self._lock = threading.Lock()
        self._proxy_state: dict[str, dict[str, Any]] = {}
        self._state_lock = threading.Lock()

    # ------------------------------------------------------------------ utils
    def emit(self, event: dict[str, Any]) -> None:
        if not self.on_event:
            return
        event.setdefault("at", time.strftime("%Y-%m-%dT%H:%M:%S%z"))
        try:
            self.on_event(event)
        except Exception:  # noqa: BLE001
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
        with self._lock:
            index = self._counter
            self._counter += 1
        return index

    @staticmethod
    def _sleep(low: float, high: float) -> None:
        time.sleep(max(0.0, random.uniform(low, high)))

    # -------------------------------------------------------- proxy scheduling
    def _state(self, proxy: str) -> dict[str, Any]:
        with self._state_lock:
            return self._proxy_state.setdefault(proxy, {"active": 0, "failures": 0, "cooldown_until": 0.0})

    def _acquire(self, index: int) -> str:
        pool = self.cfg.proxies or [""]
        now = time.time()
        for offset in range(len(pool)):
            proxy = pool[(index + offset) % len(pool)]
            state = self._state(proxy)
            if state["cooldown_until"] <= now and state["active"] < self.cfg.antiban.per_proxy_concurrency:
                state["active"] += 1
                return proxy
        proxy = pool[index % len(pool)]
        self._state(proxy)["active"] += 1
        return proxy

    def _next_domain(self, index: int) -> str:
        """域名轮换：第一个域名被限流时（Agnes 会按域名限流）自动换下一个。"""
        domains = [d for d in self.cfg.domains() if d]
        if not domains:
            return ""
        return domains[index % len(domains)]

    def _release(self, proxy: str, *, ok: bool) -> None:
        state = self._state(proxy)
        state["active"] = max(0, state["active"] - 1)
        if ok:
            state["failures"] = 0
            return
        state["failures"] += 1
        if state["failures"] >= self.cfg.antiban.max_failures_per_proxy:
            state["cooldown_until"] = time.time() + self.cfg.antiban.proxy_cooldown
            self.log("warn", f"出口 {proxy or 'direct'} 连续失败 {state['failures']} 次，冷却中")

    # -------------------------------------------------------------------- run
    def run(self, count: int | None = None) -> dict[str, Any]:
        target = count or self.cfg.count
        started = time.time()
        self.emit(
            {
                "type": "start",
                "line": self.site.id,
                "count": target,
                "concurrency": self.cfg.concurrency,
                "mail_provider": self.cfg.mail.provider,
                "mail_domains": self.cfg.domains(),
                "verify_email": self.cfg.verify_email,
                "anonymous": not bool(self.site.key_candidates),
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
            results = self._batch(pending, round_no)
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
            "line": self.site.id,
            "requested": target,
            "created": len(created),
            "failed": len(failures),
            "seconds": round(time.time() - started, 1),
            "keys": len(self.store.keys()),
            "accounts": created,
            "failures": failures,
            "failureKinds": self.store.failure_summary(),
        }
        self.emit({"type": "done", "summary": summary})
        return summary

    def _batch(self, indexes: list[int], round_no: int) -> list[tuple[int, tuple[bool, dict[str, Any]]]]:
        out: list[tuple[int, tuple[bool, dict[str, Any]]]] = []
        workers = max(1, min(self.cfg.concurrency, len(indexes)))
        with ThreadPoolExecutor(max_workers=workers) as pool:
            futures = {pool.submit(self._one, index, round_no): index for index in indexes}
            for future in as_completed(futures):
                index = futures[future]
                try:
                    out.append((index, (True, future.result())))
                except LineError as exc:
                    retryable = isinstance(exc, (RateLimited, Transport, Blocked, CaptchaRequired, VerificationPending))
                    self.log("warn", f"失败 #{index}: {exc.kind} {exc}")
                    self.store.record_failure(email=f"#{index}", kind=exc.kind, reason=str(exc))
                    out.append(
                        (index, (False, {"index": index, "kind": exc.kind, "reason": str(exc)[:300], "retryable": retryable}))
                    )
                except Exception as exc:  # noqa: BLE001
                    self.log("error", f"未捕获异常 #{index}: {type(exc).__name__}: {exc}")
                    out.append(
                        (
                            index,
                            (
                                False,
                                {
                                    "index": index,
                                    "kind": "error",
                                    "reason": f"{type(exc).__name__}: {exc}"[:300],
                                    "retryable": False,
                                },
                            ),
                        )
                    )
        return out

    def _one(self, index: int, round_no: int) -> dict[str, Any]:
        index = self._next_index() if round_no == 0 else index
        proxy = self._acquire(index)
        provider: Provider | None = None
        email = ""
        try:
            # 匿名线（Pollinations）：不注册，直接产出一条"无 Key 账号"
            if not self.site.key_candidates:
                record = anon_session(self.site, proxy=proxy, timeout=self.cfg.timeout)
                self.store.add(record, engine="anonymous", proxy=proxy)
                self._release(proxy, ok=True)
                self.emit({"type": "account", "index": index, "account": record})
                self.log("info", f"+ {record['email']}（匿名，无需 Key）")
                return {"email": record["email"], "apiKey": ""}

            persona = make_persona()
            provider = self.mail_factory(self.cfg, proxy)
            domain = self._next_domain(index)
            box = provider.allocate(local_part=persona.local_part, domain=domain)
            email = box.address
            self.emit({"type": "attempt", "index": index, "email": email, "provider": provider.name, "proxy": bool(proxy)})
            self.log("info", f"#{index} 注册 {email}（出口 {proxy or 'direct'}）")

            client = self.session_factory(proxy=proxy, timeout=self.cfg.timeout)
            try:
                captcha = self.captcha_hook(email, proxy) if self.captcha_hook else {}
                account = client.run_flow(
                    email,
                    persona.password,
                    name=persona.name,
                    verify=self.cfg.verify_email,
                    provider=provider,
                    box=box,
                    verify_timeout=self.cfg.mail.verify_timeout,
                    key_name=self.cfg.key_name,
                    captcha=captcha,
                    log=lambda m: self.log("debug", m),
                )
            finally:
                client.close()

            account.setdefault("keyPrefix", self.site.key_prefix)
            account.setdefault("createdAt", time.strftime("%Y-%m-%dT%H:%M:%S%z"))
            self.store.add(account, engine="protocol", proxy=proxy)
            self._release(proxy, ok=True)
            record = {
                "email": account["email"],
                "password": account["password"],
                "name": account.get("name", ""),
                "apiKey": account["apiKey"],
                "keyPrefix": account.get("keyPrefix", self.site.key_prefix),
                "userId": account.get("userId", ""),
                "emailVerified": account.get("emailVerified"),
                "credits": account.get("credits") or {},
                "engine": "protocol",
                "createdAt": account["createdAt"],
            }
            self.emit({"type": "account", "index": index, "account": record})
            self.log("info", f"+ {email} 入池（{record['apiKey'][:12]}…）")
            self._push(record)
            return {"email": email, "apiKey": record["apiKey"]}
        except LineError as exc:
            self._release(proxy, ok=False)
            self.store.record_failure(email=email or f"#{index}", kind=exc.kind, reason=str(exc))
            self.emit({"type": "failed", "index": index, "email": email, "kind": exc.kind, "reason": str(exc)[:300]})
            raise
        finally:
            if provider is not None:
                try:
                    provider.close()
                except Exception:  # noqa: BLE001
                    pass

    def _push(self, record: dict[str, Any]) -> None:
        if not (self.cfg.push_url and self.cfg.push_token):
            return
        from .push import push_account

        try:
            result = push_account(self.cfg.push_url, self.cfg.push_token, record, line=self.site.id)
            self.emit(
                {
                    "type": "pushed",
                    "email": record["email"],
                    "gatewayKey": (result.get("gatewayKey") or {}).get("prefix", ""),
                }
            )
        except LineError as exc:
            self.emit({"type": "push_error", "email": record["email"], "message": str(exc)[:200]})
            self.log("warn", f"回推网关失败：{exc}")

    # ----------------------------------------------------------------- doctor
    def doctor(self) -> dict[str, Any]:
        out: dict[str, Any] = {"site": self.site.describe(), "config": self.cfg.describe()}
        client = build_client(proxy=self.cfg.proxy_for(0), timeout=20.0)
        probes: dict[str, Any] = {}
        for label, url in (
            ("health", self.site.url(self.site.health_path) if self.site.health_path else ""),
            ("session", self.site.url(self.site.session_path)),
            ("models", f"{self.site.api_base.rstrip('/')}/models"),
        ):
            if not url:
                continue
            try:
                response = request(client, "GET", url)
                probes[label] = f"HTTP {response.status_code} {(response.text or '')[:120]}"
            except LineError as exc:
                probes[label] = f"unreachable: {exc}"
        client.close()
        out["probes"] = probes
        out["store"] = self.store.counts()
        return out
