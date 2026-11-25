"""并发调度与事件流。

事件契约（网关 `src/provider-register.mjs` 逐行解析 stdout）：

    {"type":"log","level":"info","message":"…"}
    {"type":"start","count":N,"concurrency":C,"mail_provider":"domain","engine":"auto","captcha":"none","anti_ban":{…}}
    {"type":"attempt","email":"…","domain":"…","provider":"blackbox","engine":"protocol","ip":"…"}
    {"type":"retry","round":2,"count":3}
    {"type":"backoff","kind":"rate_limited","seconds":27.4,"email":"…"}
    {"type":"domain","domains":[{"domain":"x.com","ok":3,"bad":0,"health":1.0}]}
    {"type":"account","account":{…},"domain":"x.com","engine":"protocol"}
    {"type":"pushed","email":"…"} / {"type":"push_error","email":"…","message":"…"}
    {"type":"failed","index":0,"email":"…","kind":"email_taken","reason":"…"}
    {"type":"done","summary":{"created":3,"failed":1,"seconds":41.2}}

双引擎策略（`--engine`）：
    protocol  纯 HTTP，吃"已登录会话 / Google cookies / 授权码"（最快）
    browser   指纹浏览器，真的走一遍 Google 登录并从面板 DOM 读 Key（最稳）
    auto      先 protocol；遇到 session_expired / oauth_denied / no_api_key / blocked
              当场切 browser 重跑（同一身份、同一出口）
    hybrid    每个号都用浏览器；protocol 只做预检
    console   console.blackbox.ai 的 Email OTP + Turnstile（需要公司已开通）
"""

from __future__ import annotations

import json
import os
import random
import sys
import threading
import time
import traceback
from concurrent.futures import ThreadPoolExecutor, as_completed
from typing import Any

from .antiban import AntiBan
from .browser import BrowserUnavailable, run_browser_registration
from .config import GoogleIdentity, RunConfig
from .errors import BlackboxError, SessionExpired
from .identity import load_cookies, load_identities, make_persona, identity_from_cookies
from .mailbox import DomainMailboxFactory
from .protocol import BlackboxProtocol, Registration
from .store import Store

PROVIDER_ID = "blackbox"
SWITCH_TO_BROWSER = {"session_expired", "oauth_denied", "no_api_key", "key_not_obtainable", "blocked", "captcha_required"}


class Reporter:
    """stdout 上一行一个 JSON；非 json 模式下给人看的文本走 stderr。"""

    def __init__(self, *, json_events: bool):
        self.json_events = json_events
        self._lock = threading.Lock()

    def event(self, **payload: Any) -> None:
        if not self.json_events:
            return
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
        self.identities = load_identities(config.google_pool)
        session_cookies = load_cookies(config.session_file)
        self.session_cookies = session_cookies
        if session_cookies and not self.identities:
            # 只给了一份 app 会话：用它认领一个身份（邮箱从 session 里读出来）
            self.identities = [GoogleIdentity(email=config.email or "session@local", cookies=session_cookies, note="session-file")]
        self.google_cookies = load_cookies(config.google_cookies)
        self.started = time.time()
        self.created: list[dict] = []
        self.failed: list[dict] = []
        self._counter = 0
        self._counter_lock = threading.Lock()
        self._identity_cursor = 0
        self._identity_lock = threading.Lock()

    # ------------------------------------------------------------------ 身份
    def _next_identity(self, persona, domain: str, seed: str) -> GoogleIdentity:
        with self._identity_lock:
            if self.identities:
                identity = self.identities[self._identity_cursor % len(self.identities)]
                self._identity_cursor += 1
                if not identity.email:
                    identity.email = f"{seed}@{domain or 'example.com'}"
                return identity
        # 没有身份池：用域名邮箱造一个地址（只适合"身份已由人工建好、注册机只领 Key"）
        email, _domain = self.factory.next_address(persona, domain=domain or None, seed=seed)
        return GoogleIdentity(email=email, cookies=self.google_cookies, note="generated")

    def _pick_domain(self) -> str:
        domains = self.factory.domains or list(self.anti_ban.domains.keys())
        if not domains:
            return ""
        domain = self.anti_ban.pick_domain(domains)
        wait = self.anti_ban.reserve_domain(domain)
        if wait > 0:
            self.report.event(type="backoff", kind="domain_window", seconds=round(wait, 1), email="")
            self.report.log(f"域名 {domain} 配额已满，等 {wait:.0f}s", "warn")
            time.sleep(wait)
        return domain

    # ------------------------------------------------------------------ 单元
    def _unit(self, index: int, round_index: int = 1) -> dict | None:
        config = self.config
        seed = f"{int(time.time())}-{index}-{round_index}-{random.randint(1000, 9999)}"
        persona = make_persona(seed=seed)
        domain = self._pick_domain()
        identity = self._next_identity(persona, domain, seed)
        proxy = self.anti_ban.proxy_for_identity(identity.email or seed, index)

        self.report.event(
            type="attempt",
            email=identity.email,
            domain=domain,
            provider=PROVIDER_ID,
            engine=config.engine,
            ip=proxy or "direct",
        )
        self.report.log(f"#{index + 1} 开始：{identity.email or '(no email)'}（engine={config.engine}, proxy={proxy or 'direct'}）")

        if identity.email and identity.email in self.store.seen_emails():
            self.report.log(f"#{index + 1} {identity.email} 已经成功过，跳过", "warn")
            return None

        engine_used = "protocol"
        registration = None
        last_error: BlackboxError | None = None

        try:
            wants_browser_first = config.engine in ("browser", "hybrid")
            protocol_first = config.engine in ("protocol", "auto", "console")

            if protocol_first and not wants_browser_first:
                try:
                    registration = self._run_protocol(identity, proxy)
                except BlackboxError as cause:
                    last_error = cause
                    if config.engine == "auto" and cause.kind in SWITCH_TO_BROWSER:
                        self.report.log(f"协议腿被拦（{cause.kind}: {cause}），切指纹浏览器", "warn")
                        registration = self._run_browser(identity, persona, proxy, domain)
                        engine_used = "browser"
                    else:
                        raise
            else:
                registration = self._run_browser(identity, persona, proxy, domain)
                engine_used = "browser"

            if registration is None:
                raise last_error or BlackboxError("没有产出注册结果。")

            engine_used = registration.engine or engine_used
            record = registration.to_record()
            record["engine"] = engine_used
            record["domain"] = domain
            record["proxy"] = proxy

            if not record.get("apiKey"):
                raise BlackboxError("没有拿到 API Key。", kind="no_api_key")

            self.store.upsert(record)
            self.store.record_domain(domain, ok=True)
            self.anti_ban.record_domain(domain, ok=True)
            self.anti_ban.record_proxy(proxy, ok=True)
            self.report.event(type="account", account=record, domain=domain, engine=engine_used)
            self.report.event(type="domain", domains=self.anti_ban.domain_report())
            self._push(record)
            return record

        except BlackboxError as cause:
            self.store.record_domain(domain, ok=False)
            self.anti_ban.record_domain(domain, ok=False)
            self.anti_ban.record_proxy(proxy, ok=False)
            self.store.record_failure(
                email=identity.email, kind=cause.kind, reason=str(cause), domain=domain, engine=config.engine
            )
            wait = self.anti_ban.backoff_seconds(cause.kind)
            if wait > 0:
                self.report.event(type="backoff", kind=cause.kind, seconds=round(wait, 1), email=identity.email)
                time.sleep(wait)
            self.report.event(
                type="failed",
                index=index,
                email=identity.email,
                kind=cause.kind,
                reason=str(cause)[:300],
            )
            raise
        except Exception as cause:  # noqa: BLE001 - 第三方库裸异常也要归类
            kind = "transport"
            self.store.record_domain(domain, ok=False)
            self.store.record_failure(
                email=identity.email, kind=kind, reason=f"{type(cause).__name__}: {cause}", domain=domain, engine=config.engine
            )
            self.report.log(f"异常（按 {kind} 处理）：{type(cause).__name__}: {cause}", "warn")
            self.report.event(
                type="failed",
                index=index,
                email=identity.email,
                kind=kind,
                reason=f"{type(cause).__name__}: {cause}"[:300],
            )
            raise BlackboxError(str(cause), kind=kind) from cause

    def _run_protocol(self, identity: GoogleIdentity, proxy: str) -> Registration:
        from .net import apply_cookie_rows, host_of

        engine = BlackboxProtocol(config=self.config, persona=make_persona(seed=identity.email or "p"), proxy=proxy, log=self.report.log)
        try:
            # --session-file / 身份自带的 app 会话先注入：这是协议腿最快的一条路，
            # 省掉整段 Google 授权（也是唯一不依赖浏览器的一条）。
            if self.session_cookies:
                applied = apply_cookie_rows(
                    engine.session.client, self.session_cookies, default_domain=host_of(self.config.base_url)
                )
                self.report.log(f"注入 app 会话 cookies {applied} 条", "debug")
            try:
                info = engine.session_info()
            except BlackboxError:
                info = {}
            code = self.config.authorization_code if not info else ""
            return engine.run(
                identity,
                keys=self.config.keys_per_account,
                key_name="",
                authorization_code=code,
            )
        finally:
            engine.close()

    def _run_browser(self, identity: GoogleIdentity, persona, proxy: str, domain: str) -> Registration:
        try:
            return run_browser_registration(
                config=self.config,
                persona=persona,
                identity=identity,
                proxy=proxy,
                log=self.report.log,
                keys=self.config.keys_per_account,
                key_name="",
                session_cookies=self.session_cookies or None,
            )
        except BrowserUnavailable as cause:
            raise BlackboxError(f"指纹浏览器不可用：{cause}", kind="browser_unavailable") from cause

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
            f"开跑：{config.count} 个 × 并发 {config.concurrency}，身份 {len(self.identities)} 个，"
            f"邮箱 {config.mail_provider}，引擎 {config.engine}，代理 {len(config.proxies)} 个"
        )
        if not self.identities and not self.session_cookies:
            self.report.log(
                "没有 Google 身份池（--google-pool）也没有已登录会话（--session-file）："
                "只能用 browser 引擎现场登录，或用 --google-cookies 复用授权",
                "warn",
            )
        if len(self.identities) and len(self.identities) < config.count:
            self.report.log(
                f"身份只有 {len(self.identities)} 个，但目标 {config.count} 个：会循环使用（同一身份只出同一个账号）",
                "warn",
            )

        pending = list(range(config.count))
        for round_index in (1, 2):
            if not pending:
                break
            if round_index == 2:
                self.report.event(type="retry", round=2, count=len(pending))
                self.report.log(f"第 2 轮重试：{len(pending)} 个（换 persona / 域名 / 出口）", "warn")
            pending = self._run_round(pending, round_index)

        summary = {
            "created": len(self.created),
            "failed": len(self.failed),
            "seconds": round(time.time() - self.started, 1),
            "accounts": [row.get("email") for row in self.created],
            "failure_reasons": self.store.counts().get("failures", {}),
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
                    if not isinstance(cause, BlackboxError):
                        self.report.log(f"未预期异常：{cause}\n{traceback.format_exc(limit=2)}", "error")
                    self.failed.append({"index": index, "reason": str(cause)[:200]})
                    remaining.append(index)
                    continue
                if record:
                    self.created.append(record)
                self.anti_ban.jitter()
        return remaining

    def _wrap(self, index: int, round_index: int):
        # 出口在 _unit 里按身份绑定；这里只保证"同一个出口不并发"
        proxy = self.anti_ban.proxy_for(index + (100 if round_index == 2 else 0))
        semaphore = self.anti_ban.proxy_lock(proxy)
        with semaphore:
            return self._unit(index, round_index)

    def close(self) -> None:
        self.store.close()


def doctor(config: RunConfig) -> dict[str, Any]:
    """环境自检：依赖 / 站点 / 身份 / 邮箱 / 浏览器内核。"""
    from .protocol import probe

    report: dict[str, Any] = {"config": config.describe(), "checks": {}}
    checks = report["checks"]

    for module in ("httpx",):
        try:
            __import__(module)
            checks[f"python:{module}"] = "ok"
        except Exception as cause:  # noqa: BLE001
            checks[f"python:{module}"] = f"missing: {cause}"

    proxy = config.proxies[0] if config.proxies else ""
    try:
        checks["site"] = probe(config, proxy=proxy, log=lambda *_: None)
    except Exception as cause:  # noqa: BLE001
        checks["site"] = f"failed: {cause}"

    identities = load_identities(config.google_pool)
    checks["google_pool"] = (
        f"{len(identities)} 身份" if identities else "未配置（--google-pool 或 BLACKBOX_GOOGLE_POOL）"
    )
    checks["session_file"] = "已配置" if load_cookies(config.session_file) else "未配置"
    checks["google_cookies"] = f"{len(load_cookies(config.google_cookies))} 条" if config.google_cookies else "未配置"

    try:
        factory = DomainMailboxFactory(
            provider=config.mail_provider,
            domains=config.anti_ban.mail_domains or config.mail_domains,
            fixed_email=config.email,
        )
        persona = make_persona(seed="doctor")
        email, domain = factory.next_address(persona, seed="doctor")
        checks["mailbox"] = f"{config.mail_provider}: {email}（域 {domain or '-'}）"
    except Exception as cause:  # noqa: BLE001
        checks["mailbox"] = f"failed: {cause}"

    if config.mail_provider in ("imap", "domain", "fixed"):
        host = os.environ.get("BLACKBOX_IMAP_HOST", "")
        user = os.environ.get("BLACKBOX_IMAP_USER", "")
        checks["imap_config"] = f"{host or '(未设置)'} / {user or '-'}"

    try:
        from .browser import available_stacks, camoufox_installed_version

        stacks = available_stacks()
        checks["browser_stacks"] = stacks or "未安装（pip install 'camoufox[geoip]' && camoufox fetch）"
        version = camoufox_installed_version()
        checks["camoufox"] = (
            f"内核已就绪 {version}" if version else ("已装 pip 包但没 fetch 内核：camoufox fetch" if "camoufox" in stacks else "未安装")
        )
    except Exception as cause:  # noqa: BLE001
        checks["browser_stacks"] = f"failed: {cause}"

    checks["captcha"] = f"{config.captcha} ({'key set' if config.captcha_key else 'no key'})"
    return report
