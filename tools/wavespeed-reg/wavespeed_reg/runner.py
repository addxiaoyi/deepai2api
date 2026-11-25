"""并发注册循环 + 每种失败的处置策略（换地址 / 换引擎 / 换代理 / 换域名）。

WaveSpeed 的特殊之处：**没有邮箱密码注册**，所以"注册"= OAuth + 领 key。
由此推出两条策略：

  * `engine=protocol` 只在"已有会话"或"只校验 key"时才有意义；
    纯协议没法完成 OAuth —— 这时直接抛 `OAuthUnavailable`，不浪费时间重试。
  * `engine=auto` 优先复用会话（协议），会话不可用才开浏览器。
  * `engine=browser` 直接开指纹浏览器走 OAuth。

失败处置表（见 antiban.should_switch_engine / should_rotate_identity）：

  email_taken            → 换地址，不重试同一个
  email_rejected         → 换域名（该域名健康分降权）
  blocked                → 换引擎（协议→浏览器）+ 退避
  captcha_required       → 换引擎 + 退避，可能补解题器
  key_not_obtainable     → 换账号（同一会话再点 Generate 只会更可疑）
  oauth_unavailable      → 换账号（提供方那边过不去，重试无益）
  rate_limited           → 退避 + 换代理
  verification_pending   → 短退避后继续等信
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
    OAuthUnavailable,
    RateLimited,
    SignupError,
    VerificationPending,
)
from .identity import Persona, make_persona
from .mailbox import Provider, build_provider
from .protocol import WaveSpeed, probe
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
        session_factory: Callable[..., WaveSpeed] | None = None,
        browser_factory: Callable[..., Any] | None = None,
    ) -> None:
        self.cfg = cfg
        self.store = store
        self.on_event = on_event
        self.quiet = quiet
        self.mail_factory = mail_factory or (lambda c, proxy: build_provider(c.mail, proxy=proxy))
        self.captcha_solver = captcha_solver or solve_turnstile
        self.session_factory = session_factory or (lambda **kw: WaveSpeed(**kw))
        self.browser_factory = browser_factory or (lambda **kw: FingerprintBrowser(**kw))
        self.cfg.output_dir.mkdir(parents=True, exist_ok=True)
        self.log_path = self.cfg.output_dir / "run.log"

        self._counter = 0
        self._counter_lock = threading.Lock()

        # 防封三件套：出口闸门 / 域名轮换 / 会话缓存
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
        except Exception:  # noqa: BLE001 - 坏掉的 sink 不能杀死注册
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
        """优先用轮换器挑；没有候选就退回配置里的第一个域名。"""
        picked = self.domains.pick(exclude)
        if picked:
            return picked
        if self.cfg.mail.domain:
            return self.cfg.mail.domain
        if self.cfg.mail.domains:
            return self.cfg.mail.domains[0]
        return ""

    def turnstile(self, proxy: str, site_key: str = "") -> str:
        return self.captcha_solver(
            provider=self.cfg.captcha.provider,
            api_key=self.cfg.captcha.api_key,
            site_url=f"{self.cfg.base_url.rstrip('/')}/sign-in",
            site_key=site_key or self.cfg.captcha.site_key,
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
                "provider": "wavespeed",
                "base_url": self.cfg.base_url,
                "api_base": self.cfg.api_base,
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
                "anti_ban": self.cfg.anti_ban.as_dict(),
            }
        )
        self.log(
            "start base=%s engine=%s count=%d concurrency=%d proxies=%d mail=%s captcha=%s"
            % (
                self.cfg.base_url,
                self.cfg.engine,
                target,
                self.cfg.concurrency,
                len(self.cfg.proxies),
                self.cfg.mail.provider,
                self.cfg.captcha.provider,
            )
        )
        if self.cfg.proxies and self.cfg.concurrency > len(self.cfg.proxies):
            self.log(
                f"concurrency {self.cfg.concurrency} > {len(self.cfg.proxies)} proxies："
                "多个账号会共用出口 IP，OAuth 与 Turnstile 都会更难",
                level="warn",
            )
        if self.cfg.engine == "protocol":
            self.log(
                "engine=protocol：WaveSpeed 只有 OAuth，纯协议无法完成注册；"
                "只有『已有会话』与『只校验 key』两种情形可用",
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
                self._sleep(5, 12)

            still_failing: list[int] = []
            workers = max(1, min(self.cfg.concurrency, max(1, len(self.cfg.proxies) or self.cfg.concurrency)))
            with ThreadPoolExecutor(max_workers=workers) as pool:
                futures = {pool.submit(self.signup, index, round_no): index for index in pending}
                for future in as_completed(futures):
                    index = futures[future]
                    try:
                        account = future.result()
                        created.append(account)
                        self.log(
                            f"#{index + 1} ok {account['email']} key={str(account['api_key'])[:10]}… "
                            f"({self.store.count()} stored)"
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
            "provider": "wavespeed",
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
            "mail_domains": self.domains.snapshot(),
            "sessions": self.sessions.stats(),
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
            proxy = self._proxy() if self.cfg.anti_ban.proxy_rotate else (self.cfg.proxies[0] if self.cfg.proxies else "")
            persona = make_persona(
                random.Random(),
                proxy=proxy,
                geo_align=self.cfg.anti_ban.geo_align,
                locale_override=self.cfg.language if self.cfg.language != "en-US" else "",
            )
            domain = self._domain_for()
            provider = self.mail_factory(self.cfg, proxy)
            engine = "browser" if self.cfg.engine == "browser" else "protocol"
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
                self.log(f"#{index + 1}.{attempt} try {email} via {self.cfg.base_url}")

                # 同一出口 IP 串行化：这是最有效的一条防封措施
                with self.ip_gate.slot(proxy, timeout=600) as acquired:
                    if not acquired:
                        raise SignupError("等待出口 IP 空闲超时（anti_ban.ip_concurrency）")
                    account, engine = self._do_signup(email, persona, proxy, provider, box, index, attempt)

                account.setdefault("email", email)
                account["persona"] = persona.as_dict()
                account["provider"] = "wavespeed"
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
                                "oauth_provider": account.get("oauth_provider", ""),
                                "password": account.get("password", ""),
                                "cookies": account.get("cookies") or [],
                                "engine": engine,
                                "domain": domain,
                                "created_at": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
                            },
                        }
                    )
                    return account

                # 地址或 key 重复：算跳过，不算失败
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
            except KeyNotObtainable as exc:
                last_error = exc
                self.store.attempt(email, engine, proxy, "key_not_obtainable", str(exc))
                self.domains.note_result(domain, False)
                self.log(f"#{index + 1}.{attempt} 拿不到 key（{exc}），换账号", level="warn")
                backoff_for(self.cfg.anti_ban, "key_not_obtainable")
            except OAuthUnavailable as exc:
                last_error = exc
                self.store.attempt(email, engine, proxy, "oauth_unavailable", str(exc))
                self.log(f"#{index + 1}.{attempt} OAuth 走不通（{exc}），换账号", level="warn")
                backoff_for(self.cfg.anti_ban, "oauth_unavailable")
            except (CaptchaRequired, CaptchaError) as exc:
                last_error = exc
                self.store.attempt(email, engine, proxy, "captcha", str(exc))
                seconds = backoff_for(self.cfg.anti_ban, "captcha_required")
                self.emit({"type": "backoff", "kind": "captcha_required", "seconds": round(seconds, 1), "email": email})
                self.log(f"#{index + 1}.{attempt} captcha 拒绝（{exc}），退避 {seconds:.0f}s", level="warn")
            except VerificationPending as exc:
                last_error = exc
                self.store.attempt(email, engine, proxy, "verification_pending", str(exc))
                self.log(f"#{index + 1}.{attempt} 没收到验证信（{exc}）", level="warn")
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
                self._sleep(3, 8)
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
        if self.cfg.engine in ("protocol", "auto"):
            # 只有拿到可用会话时才走协议腿
            cached = self.sessions.get(proxy) if self.cfg.anti_ban.reuse_session else None
            if cached:
                try:
                    account = self._protocol_with_session(email, persona, proxy, cached.cookies)
                    return account, "protocol"
                except (Blocked, CaptchaRequired, CaptchaError, KeyNotObtainable) as exc:
                    if self.cfg.engine == "protocol":
                        raise
                    self.log(
                        f"#{index + 1}.{attempt} 会话腿被拦（{type(exc).__name__}），切指纹浏览器",
                        level="warn",
                    )
            elif self.cfg.engine == "protocol":
                raise OAuthUnavailable(
                    "engine=protocol 但本地没有可用会话；WaveSpeed 只有 OAuth，"
                    "协议腿需要先由浏览器腿建立一次登录态（anti_ban.reuse_session=true）"
                )

        account = self._browser_signup(email, persona, proxy)
        # 浏览器腿成功后把会话存起来，下一轮可以走协议
        if self.cfg.anti_ban.reuse_session and account.get("cookies"):
            self.sessions.put(proxy, account["cookies"], engine="browser")
        return account, "browser"

    def _protocol_with_session(
        self,
        email: str,
        persona: Persona,
        proxy: str,
        cookies: list[dict[str, str]],
    ) -> dict[str, Any]:
        """带登录态走协议：读 /accesskey 的 key 元信息，或校验已有 key。"""
        with self.session_factory(
            base_url=self.cfg.base_url,
            api_base=self.cfg.api_base,
            task_base=self.cfg.task_base,
            proxy=proxy,
            timeout=self.cfg.timeout,
            language=persona.locale,
            user_agent=persona and self.cfg.user_agent,
            cookies=cookies,
        ) as client:
            client.warm()
            page = client.accesskey_page()  # 拿不到就抛 KeyNotObtainable
        raise KeyNotObtainable(
            f"/accesskey 只暴露元信息（{len(page.get('candidates') or [])} 个候选），"
            "WaveSpeed 没有创建 key 的 HTTP 接口；协议腿无法拿到明文"
        )

    def _browser_signup(self, email: str, persona: Persona, proxy: str) -> dict[str, Any]:
        """指纹浏览器路径：OAuth → /accesskey → 读一次性明文。"""
        screenshots = (self.cfg.output_dir / "screenshots") if self.cfg.browser.screenshots else None
        fingerprint = fingerprint_for(persona, proxy, geo_align=self.cfg.anti_ban.geo_align)
        profile_dir = ""
        if self.cfg.browser.profile_dir and not self.cfg.anti_ban.fresh_profile:
            profile_dir = self.cfg.browser.profile_dir
        try:
            with self.browser_factory(
                base_url=self.cfg.base_url,
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
                profile_dir=profile_dir,
                oauth_wait=self.cfg.browser.oauth_wait,
                accesskey_wait=self.cfg.browser.accesskey_wait,
                oauth_credentials=self.cfg.key_policy.get("oauth_credentials") or None,
                pin_cpu_cores=self.cfg.browser.pin_cpu_cores,
                use_preset=self.cfg.browser.use_preset,
                canvas_noise=self.cfg.browser.canvas_noise,
                log=lambda message: self.log(message, level="debug"),
            ) as browser:
                result = browser.signup_flow(
                    oauth_provider=str(self.cfg.key_policy.get("oauth_provider") or "google"),
                    key_name=self.cfg.key_name(email),
                    log=lambda message: self.log(message, level="debug"),
                )
        except BrowserUnavailable as exc:
            raise SignupError(
                f"指纹浏览器不可用：{exc}（pip install camoufox 或 playwright install chromium）"
            ) from exc

        api_key = str(result.get("api_key") or "")
        if not api_key:
            raise NoApiKey("浏览器腿走完了，但没读到 key 明文")

        # 用协议腿校验这把 key 真的能用（这一步不消耗生成额度）
        verified: dict[str, Any] = {}
        try:
            with self.session_factory(
                base_url=self.cfg.base_url,
                api_base=self.cfg.api_base,
                task_base=self.cfg.task_base,
                proxy=proxy,
                timeout=self.cfg.timeout,
                cookies=result.get("cookies") or [],
            ) as client:
                verified = client.verify_key(api_key)
        except (SignupError, Exception) as exc:  # noqa: BLE001 - 校验失败不致命
            self.log(f"key 校验未通过（{exc}），仍然入库", level="warn")

        return {
            "email": email,
            "password": persona.password,
            "name": persona.name,
            "api_key": api_key,
            "api_key_source": result.get("api_key_source") or "accesskey-ui",
            "key_id": "",
            "key_prefix": api_key[:8],
            "key_last4": api_key[-4:],
            "extra_keys": [],
            "expire_time": "",
            "organization_id": "",
            "email_verified": True,
            "oauth_provider": result.get("oauth_provider") or "",
            "cookies": result.get("cookies") or [],
            "verify": verified,
            "browser": result.get("browser") or "",
        }


def doctor(cfg: Config) -> dict[str, Any]:
    """环境自检：每个依赖都碰一次，只读不写。"""
    report: dict[str, Any] = {"config": cfg.describe(), "checks": {}}
    checks = report["checks"]
    proxy = cfg.proxies[0] if cfg.proxies else ""

    try:
        checks["panel"] = probe(cfg.base_url, api_base=cfg.api_base, proxy=proxy)
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
    checks["captcha_key"] = "set" if cfg.captcha.api_key else "missing"
    checks["anti_ban"] = cfg.anti_ban.as_dict()

    # WaveSpeed 特有：OAuth-only 意味着必须有一条能建立会话的腿
    if cfg.engine == "protocol" and not cfg.session_file:
        checks["engine_warning"] = (
            "engine=protocol 且没有 session_file：WaveSpeed 只有 OAuth，"
            "协议腿无法从零建立账号，请用 auto 或 browser"
        )
    return report
