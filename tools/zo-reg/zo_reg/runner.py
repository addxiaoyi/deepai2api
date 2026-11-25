"""并发调度与事件流。

双引擎策略（`--engine`）：
    protocol  只跑 HTTP：发魔法链 → 收信取 token → confirm → 建工作区 → 建 token
    browser   只跑指纹浏览器：注册页填表 → verify 页过 Turnstile → 页内 fetch 走 API
    auto      先 HTTP；遇 captcha_required / blocked / no_token 当场切浏览器重跑
    hybrid    每个号都用浏览器（更慢但更稳），HTTP 只做预检

**邮箱与 token 的关键差别**（和 moonai 那套不同）：
    Zo 的魔法链一次只对应一个邮箱，而 confirm 必须在收到信之后立刻做，
    所以单元流程是：定邮箱 → 发魔法链 → 等信 → 拿 token → 跑引擎。
    浏览器腿自己点"发送"，所以它发送后再等信；协议腿我们先发一次再等。
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
from .errors import ZoError, VerificationPending
from .identity import make_persona
from .mailbox import DomainMailboxFactory
from .protocol import ZoProtocol
from .store import Store

PROVIDER_ID = "zo"


class Reporter:
    """stdout 上一行一个 JSON；非 json 模式下给人看的文本走 stderr。"""

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

    def _unit(self, index: int) -> dict:
        """一个账号的完整尝试；成功返回 record，失败抛 ZoError。"""
        config = self.config
        seed = self._next_seed()
        domain = ""
        proxy = self.anti_ban.proxy_for(index)
        persona = make_persona(seed=seed, country=None, desktop=config.desktop_ua, handle_prefix=config.handle_prefix)

        mailbox = None
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
        self.report.log(f"#{index + 1} 开始：{email}（engine={config.engine}, handle={persona.handle}, proxy={proxy or 'direct'}）")

        record: dict | None = None
        engine_used = "protocol"
        try:
            if email in self.store.seen_emails() and config.mail_provider != "fixed":
                raise ZoError("这个邮箱已经注册成功过，跳过。", kind="email_taken")

            mailbox = self.factory.open(email) if config.mail_provider != "fixed" or config.verify_email else None

            wants_protocol = config.engine in ("protocol", "auto")
            wants_browser_only = config.engine in ("browser", "hybrid")
            registration = None
            last_error: ZoError | None = None

            if wants_protocol and not wants_browser_only:
                registration, engine_used = self._run_protocol(seed, email, persona, proxy, mailbox)
            else:
                registration, engine_used = self._run_browser(email, persona, proxy, mailbox)

            if registration is None:
                raise last_error or ZoError("没有产出注册结果。")

            record = registration.to_record()
            record["engine"] = engine_used
            record["domain"] = domain
            record["proxy"] = proxy
            if not record.get("apiKey") and not record.get("accessToken"):
                raise ZoError("既没有 access token 也没有 api-keys 凭据。", kind="no_api_key")

            self.store.upsert(record)
            self.store.record_domain(domain, ok=True)
            self.anti_ban.record_domain(domain, ok=True)
            self.anti_ban.record_proxy(proxy, ok=True)
            self.report.event(type="account", account=record, domain=domain, engine=engine_used)
            self.report.event(type="domain", domains=self.anti_ban.domain_report())
            self._push(record)
            return record
        except (ZoError, BrowserUnavailable) as cause:
            self.store.record_domain(domain, ok=False)
            self.anti_ban.record_domain(domain, ok=False)
            self.anti_ban.record_proxy(proxy, ok=False)
            self.store.record_failure(email=email, kind=cause.kind, reason=str(cause), domain=domain, engine=config.engine)
            wait = self.anti_ban.backoff_seconds(cause.kind)
            if wait > 0:
                self.report.event(type="backoff", kind=cause.kind, seconds=round(wait, 1), email=email)
                time.sleep(wait)
            self.report.event(type="failed", index=index, email=email, kind=cause.kind, reason=str(cause)[:300])
            raise
        except Exception as cause:  # noqa: BLE001 - 第三方库的裸异常也要归类
            kind = "transport"
            self.store.record_domain(domain, ok=False)
            self.anti_ban.record_domain(domain, ok=False)
            self.anti_ban.record_proxy(proxy, ok=False)
            self.store.record_failure(
                email=email, kind=kind, reason=f"{type(cause).__name__}: {cause}", domain=domain, engine=config.engine
            )
            self.report.log(f"异常（按 {kind} 处理）：{type(cause).__name__}: {cause}", "warn")
            self.report.event(type="failed", index=index, email=email, kind=kind, reason=f"{type(cause).__name__}: {cause}"[:300])
            raise ZoError(str(cause), kind=kind) from cause
        finally:
            if mailbox is not None:
                try:
                    mailbox.close()
                except Exception:  # noqa: BLE001
                    pass
            _ = record

    def _wait_token(self, mailbox, email: str) -> str:
        """等魔法链邮件，返回 token。"""
        if mailbox is None:
            return ""
        self.report.log(f"等魔法链邮件：{email}")
        try:
            token = mailbox.wait_for_token(timeout=self.config.verify_timeout)
            self.report.log(f"拿到魔法链 token：{token[:12]}…")
            return token
        except VerificationPending as cause:
            raise cause

    def _run_protocol(self, seed: str, email: str, persona, proxy: str, mailbox):
        engine = ZoProtocol(config=self.config, persona=persona, proxy=proxy, log=self.report.log)
        try:
            engine.warmup()
            engine.request_magic_link(email)
            token = self._wait_token(mailbox, email) if self.config.verify_email else ""
            if not token:
                raise VerificationPending("没等到魔法链 token（检查 IMAP 配置或 --verify-timeout）。")
            registration = engine.run(
                email=email,
                token=token,
                handle=persona.handle,
                keys_per_account=self.config.keys_per_account,
                token_name=self.config.token_name,
                probe=self.config.probe,
                mcp_probe=self.config.mcp_probe,
            )
            registration.engine = "protocol"
            return registration, "protocol"
        except ZoError as cause:
            if self.config.engine == "auto" and cause.kind in (
                "captcha_required",
                "blocked",
                "no_token",
                "upstream",
                "verification_pending",
            ):
                self.report.log(f"协议腿失败（{cause.kind}），切指纹浏览器重跑", "warn")
                return self._run_browser(email, persona, proxy, mailbox, reissue=True)
            raise
        finally:
            engine.close()

    def _run_browser(self, email: str, persona, proxy: str, mailbox, *, reissue: bool = False):
        # 浏览器腿会自己点"发送"，所以要先拿一个新的魔法链（旧 token 可能被协议腿用掉）
        if reissue and mailbox is not None:
            self.report.log("浏览器腿：重新发一封魔法链")
        verify_link = ""
        if mailbox is not None and self.config.verify_email:
            verify_link = mailbox.wait_for_verify_link(timeout=self.config.verify_timeout)
            self.report.log(f"浏览器腿拿到魔法链：{verify_link[:70]}…")
        registration = run_browser_registration(
            config=self.config,
            persona=persona,
            email=email,
            verify_link=verify_link,
            proxy=proxy,
            log=self.report.log,
            keys_per_account=self.config.keys_per_account,
            token_name=self.config.token_name,
        )
        return registration, registration.engine or "browser"

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
            "failure_reasons": _tally(self.failed),
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
                except Exception as cause:  # noqa: BLE001 - 失败已记录
                    if not isinstance(cause, ZoError):
                        self.report.log(f"未预期异常：{cause}\n{traceback.format_exc(limit=2)}", "error")
                    self.failed.append({"index": index, "reason": str(cause)[:200], "kind": getattr(cause, "kind", "error")})
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


def _tally(rows: list[dict]) -> list[dict]:
    counts: dict[str, int] = {}
    for row in rows:
        kind = str(row.get("kind") or "error")
        counts[kind] = counts.get(kind, 0) + 1
    return [{"kind": k, "count": v} for k, v in sorted(counts.items(), key=lambda kv: -kv[1])]