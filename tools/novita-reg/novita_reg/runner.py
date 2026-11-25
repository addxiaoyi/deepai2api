"""并发注册循环 + 每种失败的处置策略。

Novita 有完整的邮箱密码注册，所以协议腿可以从零建号（只需 Turnstile token）；
浏览器腿作兜底（协议被 Cloudflare 拦、或建 key 的接口拿不到明文时）。

失败处置表（见 antiban.should_switch_engine / should_rotate_identity）：

  email_taken          → 换地址，不重试同一个
  email_rejected       → 换域名（该域名健康分降权）
  password_rejected    → 换一个更强的密码（同一地址重试一次）
  blocked              → 换引擎（协议→浏览器）+ 退避
  captcha_required     → 换引擎 + 退避，可能补解题器
  verification_pending → 短退避后继续等信
  key_not_obtainable   → 换引擎（协议→浏览器点 UI）或换账号
  verify_required      → 换账号（实名门槛不可绕过）
  quota_exhausted      → 换账号（余额/voucher 问题）
  rate_limited         → 退避 + 换代理
"""

from __future__ import annotations

import random
import threading
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from typing import Any, Callable, Protocol

from .antiban import (
    DomainRotator,
    IpGate,
    SessionCache,
    backoff_for,
    fingerprint_for,
    jitter,
    key_policy_payload,
    profile_isolation_plan,
    should_rotate_identity,
    should_switch_engine,
)
from .browser import BrowserUnavailable, FingerprintBrowser, available_stacks
from .captcha import CaptchaError, solve_turnstile
from .config import Config
from .errors import (
    Blocked,
    CaptchaRequired,
    EmailRejected,
    EmailTaken,
    KeyNotObtainable,
    NoApiKey,
    PasswordRejected,
    QuotaExhausted,
    RateLimited,
    SignupError,
    VerificationPending,
    VerifyRequired,
)
from .identity import Persona, make_persona
from .mailbox import Provider, build_provider
from .protocol import Novita, probe
from .store import Store

_print_lock = threading.Lock()


class MailboxFactory(Protocol):
    """测试缝：生产按 config 构造 provider，测试注入假实现。"""

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
        session_factory: Callable[..., Novita] | None = None,
        browser_factory: Callable[..., Any] | None = None,
    ) -> None:
        self.cfg = cfg
        self.store = store
        self.on_event = on_event
        self.quiet = quiet
        self.mail_factory = mail_factory or (lambda c, proxy: build_provider(c.mail, proxy=proxy))
        self.captcha_solver = captcha_solver or solve_turnstile
        self.session_factory = session_factory or (lambda **kw: Novita(**kw))
        self.browser_factory = browser_factory or (lambda **kw: FingerprintBrowser(**kw))
        self.cfg.output_dir.mkdir(parents=True, exist_ok=True)
        self.log_path = self.cfg.output_dir / "run.log"

        self._counter = 0
        self._counter_lock = threading.Lock()

        self.ip_gate = IpGate(cfg.anti_ban.ip_concurrency)
        self.domains = DomainRotator(cfg.anti_ban.mail_domains, cfg.anti_ban)
        self.sessions = SessionCache(cfg.anti_ban.session_ttl_hours)
        self._rng = random.Random()

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

    def _proxy(self) -> str:
        with self._counter_lock:
            index = self._counter
            self._counter += 1
        return self.cfg.proxy_for(index)

    @staticmethod
    def _sleep(low: float, high: float) -> None:
        time.sleep(random.uniform(low, high))

    def _domain_for(self, exclude: tuple[str, ...] = ()) -> str:
        picked = self.domains.pick(exclude)
        if picked:
            return picked
        if self.cfg.mail.domain:
            return self.cfg.mail.domain
        if self.cfg.mail.domains:
            return self.cfg.mail.domains[0]
        return ""

    def turnstile(self, proxy: str) -> str:
        return self.captcha_solver(
            provider=self.cfg.captcha.provider,
            api_key=self.cfg.captcha.api_key,
            site_url=f"{self.cfg.panel.rstrip('/')}/user/register",
            site_key=self.cfg.captcha.site_key,
            action=self.cfg.captcha.action,
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
                "provider": "novita",
                "base_url": self.cfg.panel,
                "panel_api": self.cfg.panel_api,
                "api_base": self.cfg.openai_base,
                "engine": self.cfg.engine,
                "browser_stack": self.cfg.browser.stack,
                "count": target,
                "concurrency": self.cfg.concurrency,
                "mail_provider": self.cfg.mail.provider,
                "mail_domains": list(self.cfg.anti_ban.mail_domains or self.cfg.mail.domains),
                "captcha": self.cfg.captcha.provider,
                "proxies": len(self.cfg.proxies),
                "retry_rounds": self.cfg.retry_rounds,
                "keys_per_account": self.cfg.key_policy.per_account,
                "key_policy": self.cfg.key_policy.as_dict(),
                "anti_ban": self.cfg.anti_ban.as_dict(),
            }
        )
        self.log(
            "start panel=%s engine=%s count=%d concurrency=%d proxies=%d mail=%s captcha=%s keys=%d"
            % (
                self.cfg.panel,
                self.cfg.engine,
                target,
                self.cfg.concurrency,
                len(self.cfg.proxies),
                self.cfg.mail.provider,
                self.cfg.captcha.provider,
                self.cfg.key_policy.per_account,
            )
        )
        if self.cfg.proxies and self.cfg.concurrency > len(self.cfg.proxies):
            self.log(
                f"concurrency {self.cfg.concurrency} > {len(self.cfg.proxies)} proxies："
                "多账号共用出口 IP 会显著提高 voucher 被撤销的概率",
                level="warn",
            )

        for round_no in range(self.cfg.retry_rounds + 1):
            if not pending:
                break
            if round_no:
                retries += len(pending)
                self.emit({"type": "retry", "round": round_no, "count": len(pending)})
                self.log(
                    f"retry round {round_no}/{self.cfg.retry_rounds}: {len(pending)} 个账号 "
                    "换全新 persona / 地址 / 代理 / 域名",
                    level="warn",
                )
                failures.clear()
                self._sleep(3, 7)

            still_failing: list[int] = []
            workers = max(1, self.cfg.concurrency)
            with ThreadPoolExecutor(max_workers=workers) as pool:
                futures = {pool.submit(self.signup, index, round_no): index for index in pending}
                for future in as_completed(futures):
                    index = futures[future]
                    try:
                        account = future.result()
                        created.append(account)
                        self.log(
                            f"#{index + 1} ok {account['email']} key={str(account['api_key'])[:10]}… "
                            f"({self.store.count()} stored / {self.store.key_count()} keys)"
                        )
                    except Exception as exc:  # noqa: BLE001 - 上报，不致命
                        still_failing.append(index)
                        failures.append(f"#{index + 1} {type(exc).__name__}: {exc}")
                        self.log(f"#{index + 1} failed {type(exc).__name__}: {exc}", level="error")
            pending = sorted(still_failing)
            self.emit({"type": "domain", "domains": self.domains.snapshot()})

        jsonl = self.cfg.output_dir / "accounts.jsonl"
        csv_path = self.cfg.output_dir / "accounts.csv"
        rows = self.store.export_jsonl(jsonl)
        self.store.export_csv(csv_path)

        summary = {
            "provider": "novita",
            "requested": target,
            "created": len(created),
            "failed": len(pending),
            "retries": retries,
            "stored": self.store.count(),
            "keys": self.store.key_count(),
            "seconds": round(time.time() - started, 1),
            "jsonl": str(jsonl),
            "csv": str(csv_path),
            "jsonl_rows": rows,
            "failures": failures,
            "failure_reasons": self.store.failure_summary(),
            "mail_domains": self.domains.snapshot(),
            "sessions": self.sessions.stats(),
        }
        self.emit({"type": "done", "summary": summary})
        self.log(
            f"done created={summary['created']} failed={summary['failed']} "
            f"keys={summary['keys']} in {summary['seconds']}s"
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
            proxy = self._proxy() if self.cfg.anti_ban.proxy_rotate else (self.cfg.proxies[0] if self.cfg.proxies else "")
            persona = make_persona(
                random.Random(),
                proxy=proxy,
                geo_align=self.cfg.anti_ban.geo_align,
                locale_override=self.cfg.language if self.cfg.language != "en-US" else "",
            )
            domain = self._domain_for()
            provider = self.mail_factory(self.cfg, proxy)
            engine = "protocol"
            try:
                box = provider.allocate(persona.local_part, domain=domain)
                email = box.address
                domain = email.rsplit("@", 1)[-1]
                if domain:
                    self.domains.note_send(domain)
                self.emit(
                    {
                        "type": "attempt",
                        "index": index + 1,
                        "attempt": attempt,
                        "email": email,
                        "domain": domain,
                        "provider": provider.name,
                        "engine": engine,
                        "ip": proxy or "direct",
                    }
                )
                self.log(f"#{index + 1}.{attempt} try {email} via {self.cfg.panel}")

                with self.ip_gate.slot(proxy, timeout=600) as acquired:
                    if not acquired:
                        raise SignupError("等待出口 IP 空闲超时（anti_ban.ip_concurrency）")
                    account, engine = self._do_signup(email, persona, proxy, provider, box, index, attempt)

                account.setdefault("email", email)
                account["persona"] = persona.as_dict()
                account["provider"] = "novita"
                account["domain"] = domain
                account["engine"] = engine
                account["proxy"] = proxy

                if self.store.add(account, engine=engine, proxy=proxy):
                    self.store.attempt(email, engine, proxy, "created")
                    self.domains.note_result(domain, True)
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
                                "expire_time": account.get("expire_time", ""),
                                "organization_id": account.get("organization_id", ""),
                                "email_verified": account.get("email_verified"),
                                "password": account.get("password", ""),
                                "cookies": account.get("cookies") or [],
                                "engine": engine,
                                "domain": domain,
                                "created_at": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
                            },
                        }
                    )
                    return account

                self.store.attempt(email, engine, proxy, "duplicate")
                self.log(f"#{index + 1} {email} already stored, skipped", level="warn")
                return account

            except EmailTaken as exc:
                last_error = exc
                self.store.attempt(email, engine, proxy, "email_taken", str(exc))
                self.domains.note_result(domain, False)
                self.log(f"#{index + 1}.{attempt} {email} already registered, rotating", level="warn")
            except EmailRejected as exc:
                last_error = exc
                self.store.attempt(email, engine, proxy, "email_rejected", str(exc))
                self.domains.note_result(domain, False)
                self.log(f"#{index + 1}.{attempt} address rejected ({exc}), rotating domain", level="warn")
            except PasswordRejected as exc:
                last_error = exc
                self.store.attempt(email, engine, proxy, "password_rejected", str(exc))
                self.log(f"#{index + 1}.{attempt} 密码被拒（{exc}），下一轮换更强的密码", level="warn")
            except KeyNotObtainable as exc:
                last_error = exc
                self.store.attempt(email, engine, proxy, "key_not_obtainable", str(exc))
                self.domains.note_result(domain, False)
                self.log(f"#{index + 1}.{attempt} 拿不到 key（{exc}）", level="warn")
                backoff_for(self.cfg.anti_ban, "key_not_obtainable")
            except VerifyRequired as exc:
                last_error = exc
                self.store.attempt(email, engine, proxy, "verify_required", str(exc))
                self.log(f"#{index + 1}.{attempt} 需要实名（{exc}），换账号", level="warn")
            except QuotaExhausted as exc:
                last_error = exc
                self.store.attempt(email, engine, proxy, "quota_exhausted", str(exc))
                self.log(f"#{index + 1}.{attempt} 余额/voucher 不可用（{exc}），换账号", level="warn")
            except (CaptchaRequired, CaptchaError) as exc:
                last_error = exc
                self.store.attempt(email, engine, proxy, "captcha", str(exc))
                seconds = backoff_for(self.cfg.anti_ban, "captcha_required")
                self.emit({"type": "backoff", "kind": "captcha_required", "seconds": round(seconds, 1), "email": email})
                self.log(f"#{index + 1}.{attempt} captcha 拒绝（{exc}），退避 {seconds:.0f}s", level="warn")
            except VerificationPending as exc:
                last_error = exc
                self.store.attempt(email, engine, proxy, "verification_pending", str(exc))
                self.log(f"#{index + 1}.{attempt} 没收到激活信（{exc}）", level="warn")
            except RateLimited as exc:
                last_error = exc
                self.store.attempt(email, engine, proxy, "rate_limited", str(exc))
                seconds = backoff_for(self.cfg.anti_ban, "rate_limited")
                self.emit({"type": "backoff", "kind": "rate_limited", "seconds": round(seconds, 1), "email": email})
                self.log(f"#{index + 1}.{attempt} 限流，退避 {seconds:.0f}s", level="warn")
            except Blocked as exc:
                last_error = exc
                self.store.attempt(email, engine, proxy, "blocked", str(exc))
                seconds = backoff_for(self.cfg.anti_ban, "blocked")
                self.emit({"type": "backoff", "kind": "blocked", "seconds": round(seconds, 1), "email": email})
                self.log(f"#{index + 1}.{attempt} 边缘拦截，退避 {seconds:.0f}s", level="warn")
            except NoApiKey as exc:
                last_error = exc
                self.store.attempt(email, engine, proxy, "no_api_key", str(exc))
                self.log(f"#{index + 1}.{attempt} 无可用 key（{exc}）", level="warn")
            except SignupError as exc:
                last_error = exc
                self.store.attempt(email, engine, proxy, "error", str(exc))
                self.log(f"#{index + 1}.{attempt} {type(exc).__name__}: {exc}", level="warn")
                self._sleep(2, 6)
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
    def _do_signup(
        self,
        email: str,
        persona: Persona,
        proxy: str,
        provider: Provider,
        box: Any,
        index: int,
        attempt: int,
    ) -> tuple[dict[str, Any], str]:
        """按引擎选腿。返回 (account, 实际生效的 engine)。"""
        if self.cfg.engine == "browser":
            return self._browser_signup(email, persona, proxy, provider, box), "browser"

        try:
            account = self._protocol_signup(email, persona, proxy, provider, box)
            return account, "protocol"
        except (CaptchaRequired, CaptchaError, Blocked, KeyNotObtainable) as exc:
            if self.cfg.engine not in ("auto", "hybrid"):
                raise
            self.log(
                f"#{index + 1}.{attempt} 协议腿被拦（{type(exc).__name__}: {exc}），切指纹浏览器",
                level="warn",
            )
            return self._browser_signup(email, persona, proxy, provider, box), "browser"

    def _protocol_signup(
        self,
        email: str,
        persona: Persona,
        proxy: str,
        provider: Provider,
        box: Any,
    ) -> dict[str, Any]:
        """纯 HTTP：注册 → 激活 → 建 key → 校验。"""
        token = self.turnstile(proxy)
        self.log(f"turnstile token: {'yes' if token else 'none'} ({len(token)} chars)", level="debug")
        with self.session_factory(
            panel=self.cfg.panel,
            panel_api=self.cfg.panel_api,
            api_base=self.cfg.api_base,
            openai_base=self.cfg.openai_base,
            proxy=proxy,
            timeout=self.cfg.timeout,
            language=persona.locale,
            user_agent=self.cfg.user_agent,
        ) as client:
            account = client.signup_flow(
                email,
                persona.password,
                turnstile=token,
                verify=self.cfg.verify_email,
                provider=provider,
                box=box,
                verify_timeout=self.cfg.mail.verify_timeout,
                key_name=self.cfg.key_name(email),
                keys=self.cfg.key_policy.per_account,
                expire_time=self.cfg.key_policy.expire_time,
                invite_code=self.cfg.invite_code,
                log=lambda message: self.log(message, level="debug"),
            )

            # 可选：放开 model-access（默认策略会挡掉一部分模型）
            policy = key_policy_payload(
                open_model_access=self.cfg.key_policy.open_model_access,
                model_ids=self.cfg.key_policy.model_ids,
            )
            if policy and account.get("key_id"):
                try:
                    client.set_key_policy(str(account["key_id"]), model_ids=self.cfg.key_policy.model_ids or None)
                    self.log(f"已放开 {email} 的 model-access", level="debug")
                except SignupError as exc:
                    self.log(f"model-access 设置失败（{exc}），继续", level="warn")

            # 校验 key 真的能用（不消耗生成额度）
            try:
                account["verify"] = client.verify_key(str(account.get("api_key") or ""))
            except SignupError as exc:
                self.log(f"key 校验未通过（{exc}），仍然入库", level="warn")

        if self.cfg.anti_ban.reuse_session and account.get("cookies"):
            self.sessions.put(proxy, account["cookies"], email=email, engine="protocol")
        return account

    def _browser_signup(
        self,
        email: str,
        persona: Persona,
        proxy: str,
        provider: Provider,
        box: Any,
    ) -> dict[str, Any]:
        """指纹浏览器路径：填表 → 激活 → Key 管理页建 key。"""
        screenshots = (self.cfg.output_dir / "screenshots") if self.cfg.browser.screenshots else None
        fingerprint = fingerprint_for(persona, proxy, geo_align=self.cfg.anti_ban.geo_align)
        plan = profile_isolation_plan(
            email,
            root=self.cfg.browser.profile_dir,
            isolate=self.cfg.anti_ban.isolate_profile,
            clear_storage=self.cfg.anti_ban.clear_storage_between,
            seed=persona.profile_seed,
        )
        expire_label = {"permanent": "Permanent", "90d": "90 days", "30d": "30 days", "24h": "24 hours"}.get(
            self.cfg.key_policy.expire_time, "Permanent"
        )
        try:
            with self.browser_factory(
                panel=self.cfg.panel,
                headless=self.cfg.browser.headless,
                proxy=proxy,
                cdp=self.cfg.browser.cdp,
                stack=self.cfg.browser.stack,
                locale=fingerprint.get("locale") or self.cfg.language,
                timezone_id=fingerprint.get("timezone") or "",
                geoip=self.cfg.browser.geoip,
                timeout=max(self.cfg.timeout, 60.0),
                humanize=self.cfg.browser.humanize,
                turnstile_timeout=self.cfg.browser.turnstile_timeout,
                screenshots=screenshots,
                fingerprint=fingerprint,
                profile_dir=plan.get("profile_dir") or "",
                isolate_profile=bool(plan.get("isolated")),
                clear_storage=bool(plan.get("clear_storage")),
                signup_wait=self.cfg.browser.signup_wait,
                verify_wait=self.cfg.browser.verify_wait,
                keygen_wait=self.cfg.browser.keygen_wait,
                log=lambda message: self.log(message, level="debug"),
            ) as browser:
                account = browser.signup_flow(
                    email,
                    persona.password,
                    invite_code=self.cfg.invite_code,
                    verify=self.cfg.verify_email,
                    provider=provider,
                    box=box,
                    verify_timeout=self.cfg.mail.verify_timeout,
                    key_name=self.cfg.key_name(email),
                    keys=self.cfg.key_policy.per_account,
                    expire_label=expire_label,
                    log=lambda message: self.log(message, level="debug"),
                )
        except BrowserUnavailable as exc:
            raise SignupError(
                f"指纹浏览器不可用：{exc}（pip install camoufox 或 playwright install chromium）"
            ) from exc

        if self.cfg.anti_ban.reuse_session and account.get("cookies"):
            self.sessions.put(proxy, account["cookies"], email=email, engine="browser")
        return account


def doctor(cfg: Config) -> dict[str, Any]:
    """环境自检：每个依赖都碰一次，只读不写。"""
    report: dict[str, Any] = {"config": cfg.describe(), "checks": {}}
    checks = report["checks"]
    proxy = cfg.proxies[0] if cfg.proxies else ""

    try:
        checks["panel"] = probe(cfg.panel, panel_api=cfg.panel_api, api_base=cfg.api_base, proxy=proxy)
    except SignupError as exc:
        checks["panel"] = f"unreachable: {exc}"

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
    checks["captcha_site_key"] = cfg.captcha.site_key
    checks["captcha_key"] = "set" if cfg.captcha.api_key else "missing"
    checks["key_policy"] = cfg.key_policy.as_dict()
    checks["anti_ban"] = cfg.anti_ban.as_dict()
    return report
