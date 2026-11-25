"""并发调度与事件流。

事件契约（网关 src/provider-register.mjs 逐行解析 stdout）：
    {"type":"log","level":"info","message":"…"}
    {"type":"start","count":N,"concurrency":C,"mail_provider":"imap","engine":"auto","captcha":"none","anti_ban":{…}}
    {"type":"attempt","email":"…","domain":"…","provider":"moonai","engine":"protocol","ip":"…"}
    {"type":"retry","round":2,"count":3}
    {"type":"backoff","kind":"rate_limited","seconds":27.4,"email":"…"}
    {"type":"domain","domains":[{"domain":"x.com","ok":3,"bad":0,"health":1.0}]}
    {"type":"account","account":{…},"domain":"x.com","engine":"protocol"}
    {"type":"pushed","email":"…"} / {"type":"push_error","email":"…","message":"…"}
    {"type":"failed","index":0,"email":"…","kind":"email_taken","reason":"…"}
    {"type":"done","summary":{"created":3,"failed":1,"seconds":41.2}}

双引擎策略（`--engine`）：
    protocol  只跑 HTTP
    browser   只跑指纹浏览器
    auto      先 HTTP；遇到 captcha_required / blocked / no_token 当场切浏览器重跑
    hybrid    每个号都用浏览器（更慢但更稳），HTTP 只做预检
"""

from __future__ import annotations

import json
import random
import sys
import threading
import time
import traceback
from concurrent.futures import ThreadPoolExecutor, as_completed

from .antiban import AntiBan
from .browser import BrowserUnavailable, run_browser_registration
from .config import RunConfig
from .errors import MoonAIError, VerificationPending
from .identity import make_persona
from .mailbox import DomainMailboxFactory
from .protocol import MoonAIProtocol
from .store import Store

PROVIDER_ID = "moonai"


class Reporter:
    """stdout 上的一行一个 JSON；非 json 模式下给人看的文本走 stderr。"""

    def __init__(self, *, json_events: bool):
        self.json_events = json_events
        self._lock = threading.Lock()

    def event(self, **payload) -> None:
        if self.json_events:
            with self._lock:
                sys.stdout.write(json.dumps(payload, ensure_ascii=False) + "\n")
                sys.stdout.flush()

    def log(self, message: str, level: str = "info") -> None:
        if self.json_events:
            self.event(type="log", level=level, message=message)
        else:
            with self._lock:
                sys.stderr.write(f"[{level}] {message}\n")
                sys.stderr.flush()


class Runner:
    def __init__(self, config: RunConfig, *, reporter: Reporter | None = None):
        self.config = config
        self.report = reporter or Reporter(json_events=config.json_events)
        self.store = Store(config.out_dir)
        self.anti_ban = AntiBan(config.anti_ban, proxies=config.proxies, log=self.report.log)
        self.factory = DomainMailboxFactory(
            provider=config.mail_provider,
            domains=config.anti_ban.mail_domains or config.mail_domains,
            fixed_email=config.email,
        )
        self.started = time.time()
        self.created: list[dict] = []
        self.failed: list[dict] = []
        self._counter = 0
        self._counter_lock = threading.Lock()

    # ------------------------------------------------------------------ 单元
    def _next_seed(self) -> str:
        with self._counter_lock:
            self._counter += 1
            return f"{int(time.time())}-{self._counter}-{random.randint(1000, 9999)}"

    def _unit(self, index: int) -> dict | None:
        """一个账号的完整尝试；成功返回 record，失败抛 MoonAIError。"""
        config = self.config
        seed = self._next_seed()
        domain = ""
        proxy = self.anti_ban.proxy_for(index)

        persona = make_persona(seed=seed, country=None, domain=domain)

        mailbox = None
        # mail.tm 只能现开信箱并沿用它的地址；自有域名则先用域名池定地址
        if config.mail_provider == "mailtm":
            from .mailbox import MailTM

            mailbox = MailTM.open(timeout=config.timeout)
            email = mailbox.address
            domain = email.split("@", 1)[-1]
        else:
            domains = self.factory.domains or list(self.anti_ban.domains.keys())
            if domains:
                domain = self.anti_ban.pick_domain(domains)
                wait = self.anti_ban.reserve_domain(domain)
                if wait > 0:
                    self.report.event(type="backoff", kind="domain_window", seconds=round(wait, 1), email="")
                    self.report.log(f"域名 {domain} 配额已满，等 {wait:.0f}s")
                    time.sleep(wait)
            email, domain = self.factory.next_address(persona, domain=domain or None)

        self.report.event(
            type="attempt",
            email=email,
            domain=domain,
            provider=PROVIDER_ID,
            engine=config.engine,
            ip=proxy or "direct",
        )
        self.report.log(f"#{index + 1} 开始：{email}（engine={config.engine}, proxy={proxy or 'direct'}）")

        if email in self.store.seen_emails():
            if mailbox is not None:
                mailbox.close()
            raise MoonAIError("这个邮箱已经注册成功过，跳过。", kind="email_taken")

        try:
            if mailbox is None and (config.mail_provider != "fixed" or config.verify_email):
                mailbox = self.factory.open(email)

            engine_used = "protocol"
            registration = None
            last_error: MoonAIError | None = None

            wants_protocol = config.engine in ("protocol", "auto")
            wants_browser = config.engine in ("browser", "hybrid")

            if wants_protocol and not wants_browser:
                try:
                    registration = self._run_protocol(email, persona, proxy, mailbox)
                except (BrowserUnavailable,) as cause:
                    last_error = cause
                except MoonAIError as cause:
                    last_error = cause
                    # auto：被拦就换浏览器，同一个人、同一封邮箱再来一次
                    if config.engine == "auto" and cause.kind in ("blocked", "captcha_required", "no_token", "upstream"):
                        self.report.log(f"协议腿被拦（{cause.kind}），切指纹浏览器重跑", "warn")
                        registration = self._run_browser(email, persona, proxy, mailbox)
                        engine_used = "browser"
            else:
                registration = self._run_browser(email, persona, proxy, mailbox)
                engine_used = "browser"

            if registration is None:
                raise last_error or MoonAIError("没有产出注册结果。")

            if registration.engine and registration.engine.startswith("browser"):
                engine_used = registration.engine

            record = registration.to_record()
            record["engine"] = engine_used if engine_used else record.get("engine", "protocol")
            record["domain"] = domain
            record["proxy"] = proxy

            if not record.get("apiKey") and not record.get("accessToken"):
                raise MoonAIError("既没有 api_ Key 也没有 access_token。", kind="no_api_key")

            self.store.upsert(record)
            self.store.record_domain(domain, ok=True)
            self.anti_ban.record_domain(domain, ok=True)
            self.anti_ban.record_proxy(proxy, ok=True)
            self.report.event(type="account", account=record, domain=domain, engine=record["engine"])
            self.report.event(type="domain", domains=self.anti_ban.domain_report())
            self._push(record)
            return record
        except (MoonAIError, BrowserUnavailable) as cause:
            self.store.record_domain(domain, ok=False)
            self.anti_ban.record_domain(domain, ok=False)
            self.anti_ban.record_proxy(proxy, ok=False)
            self.store.record_failure(email=email, kind=cause.kind, reason=str(cause), domain=domain, engine=config.engine)
            wait = self.anti_ban.backoff_seconds(cause.kind)
            if wait > 0:
                self.report.event(type="backoff", kind=cause.kind, seconds=round(wait, 1), email=email)
                time.sleep(wait)
            self.report.event(
                type="failed",
                index=index,
                email=email,
                kind=cause.kind,
                reason=str(cause)[:300],
            )
            raise
        except Exception as cause:  # noqa: BLE001 - 第三方库的裸异常也要归类，不能吞
            kind = "transport"
            self.store.record_domain(domain, ok=False)
            self.anti_ban.record_domain(domain, ok=False)
            self.anti_ban.record_proxy(proxy, ok=False)
            self.store.record_failure(email=email, kind=kind, reason=f"{type(cause).__name__}: {cause}", domain=domain, engine=config.engine)
            self.report.log(f"异常（按 {kind} 处理）：{type(cause).__name__}: {cause}", "warn")
            self.report.event(
                type="failed",
                index=index,
                email=email,
                kind=kind,
                reason=f"{type(cause).__name__}: {cause}"[:300],
            )
            raise MoonAIError(str(cause), kind=kind) from cause
        finally:
            if mailbox is not None:
                try:
                    mailbox.close()
                except Exception:  # noqa: BLE001
                    pass

    def _run_protocol(self, email: str, persona, proxy: str, mailbox):
        verify_link = ""
        engine = MoonAIProtocol(config=self.config, persona=persona, proxy=proxy, log=self.report.log)
        try:
            if self.config.verify_email and mailbox is not None:
                self.report.log("等验证邮件…")
                verify_link = mailbox.wait_for_verify_link(timeout=self.config.verify_timeout)
                self.report.log(f"验证链接：{verify_link[:60]}…")
            return engine.run(
                email=email,
                password=persona.password,
                username=persona.username,
                verify_link=verify_link,
                keys_per_account=self.config.keys_per_account,
                probe=self.config.probe,
            )
        finally:
            engine.close()

    def _run_browser(self, email: str, persona, proxy: str, mailbox):
        verify_link = ""
        if self.config.verify_email and mailbox is not None:
            self.report.log("等验证邮件（浏览器腿）…")
            verify_link = mailbox.wait_for_verify_link(timeout=self.config.verify_timeout)
        return run_browser_registration(
            config=self.config,
            persona=persona,
            email=email,
            password=persona.password,
            username=persona.username,
            verify_link=verify_link,
            proxy=proxy,
            log=self.report.log,
            keys_per_account=self.config.keys_per_account,
        )

    # ------------------------------------------------------------------ 推送
    def _push(self, record: dict) -> None:
        if not self.config.push_url:
            return
        from .push import PushError, push_account

        try:
            result = push_account(
                record,
                base_url=self.config.push_url,
                token=self.config.push_token,
                mint_key=self.config.key_policy.get("mintKey", True) is not False,
            )
            prefix = (result.get("gatewayKey") or {}).get("prefix", "")
            self.report.event(type="pushed", email=record["email"])
            self.report.log(f"已回推网关 {record['email']}（网关 Key {prefix or '-'}）")
        except PushError as cause:
            self.report.event(type="push_error", email=record["email"], message=str(cause))
            self.report.log(f"回推失败 {record['email']}：{cause}", "error")

    # ------------------------------------------------------------------ 主循环
    def run(self) -> dict:
        config = self.config
        self.report.event(
            type="start",
            count=config.count,
            concurrency=config.concurrency,
            mail_provider=config.mail_provider,
            engine=config.engine,
            captcha=config.captcha,
            anti_ban=config.anti_ban.to_json(),
        )
        self.report.log(
            f"开跑：{config.count} 个 × 并发 {config.concurrency}，"
            f"邮箱 {config.mail_provider}，引擎 {config.engine}，代理 {len(config.proxies)} 个"
        )

        pending = list(range(config.count))
        for round_index in (1, 2):
            if not pending:
                break
            if round_index == 2:
                self.report.event(type="retry", round=2, count=len(pending))
                self.report.log(f"第 2 轮重试：{len(pending)} 个（换 persona / 域名 / 代理）", "warn")
            pending = self._run_round(pending, round_index)

        summary = {
            "created": len(self.created),
            "failed": len(self.failed),
            "seconds": round(time.time() - self.started, 1),
            "accounts": [row.get("email") for row in self.created],
        }
        self.report.event(type="done", summary=summary)
        self.report.log(f"完成：成功 {summary['created']}，失败 {summary['failed']}，{summary['seconds']}s")
        self.anti_ban.cool_down_batch()
        return summary

    def _run_round(self, indexes: list[int], round_index: int) -> list[int]:
        remaining: list[int] = []
        with ThreadPoolExecutor(max_workers=max(1, self.config.concurrency)) as pool:
            futures = {pool.submit(self._wrap, index, round_index): index for index in indexes}
            for future in as_completed(futures):
                index = futures[future]
                try:
                    record = future.result()
                except Exception as cause:  # noqa: BLE001 - 失败已记录，这里只统计
                    if not isinstance(cause, MoonAIError):
                        self.report.log(f"未预期异常：{cause}\n{traceback.format_exc(limit=2)}", "error")
                    self.failed.append({"index": index, "reason": str(cause)[:200]})
                    remaining.append(index)
                    continue
                if record:
                    self.created.append(record)
                self.anti_ban.jitter()
        return remaining

    def _wrap(self, index: int, round_index: int):
        proxy = self.anti_ban.proxy_for(index + (100 if round_index == 2 else 0))
        semaphore = self.anti_ban.proxy_lock(proxy)
        with semaphore:
            return self._unit(index)

    def close(self) -> None:
        self.store.close()
