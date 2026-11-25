"""域名邮箱。

Blackbox 的"注册"不是填邮箱表单（app 侧只有 Google 登录），所以这里收信的场景是：

    * Google 账号的安全提醒 / 二次验证码（`--mail-provider domain` + 自有域 catch-all）
    * console.blackbox.ai 那条 Email OTP 腿的一次性登录码（6 位数字）
    * 万一站点以后恢复邮件验证，也复用同一套 `wait_for_link`

四种模式（与 `--mail-provider` 对应）：

    domain   自有域名 catch-all + IMAP 收信（**推荐**：域干净、可控、可轮换）
    imap     同 domain，但不轮换（单域名）
    mailtm   api.mail.tm 现开临时信箱（兜底；Google 基本不会给临时域发信）
    fixed    用 `--email` 指定地址（仍要 IMAP 才能收到信）

IMAP 环境变量（BLACKBOX_ 前缀，不抢别的 provider 的变量）：
    BLACKBOX_IMAP_HOST  BLACKBOX_IMAP_PORT(993)  BLACKBOX_IMAP_USER  BLACKBOX_IMAP_PASS
    BLACKBOX_IMAP_SSL(1)  BLACKBOX_IMAP_FOLDER(INBOX)
"""

from __future__ import annotations

import email
import imaplib
import os
import random
import re
import time
from dataclasses import dataclass, field
from email.header import decode_header
from typing import Iterable

import httpx

from .config import OTP_CODE_RE, VERIFY_LINK_RE
from .errors import EmailRejected, TransportError, VerificationPending
from .identity import Persona, local_part_for

VERIFY_RE = re.compile(VERIFY_LINK_RE)
OTP_RE = re.compile(OTP_CODE_RE)
SUBJECT_HINT = ("verify", "confirm", "code", "security", "sign-in", "welcome", "blackbox", "google")


def _decode(value: str | None) -> str:
    if not value:
        return ""
    parts = []
    for chunk, encoding in decode_header(value):
        if isinstance(chunk, bytes):
            parts.append(chunk.decode(encoding or "utf-8", errors="replace"))
        else:
            parts.append(chunk)
    return "".join(parts)


def _safe_json(res: httpx.Response) -> dict | None:
    if res is None or res.status_code >= 300:
        return None
    try:
        data = res.json()
    except Exception:  # noqa: BLE001 - 限流时可能回 HTML
        return None
    return data if isinstance(data, dict) else None


def extract_verify_link(text: str) -> str | None:
    if not text:
        return None
    match = VERIFY_RE.search(text.replace("&amp;", "&"))
    return match.group(0) if match else None


def extract_code(text: str) -> str | None:
    """从邮件正文里抠 6 位数字验证码。"""
    if not text:
        return None
    match = OTP_RE.search(text)
    return match.group(1) if match else None


class Mailbox:
    """邮箱抽象：所有实现都要能等验证码/链接。"""

    address: str = ""

    def wait_for_link(self, *, timeout: float = 180.0, poll: float = 3.0) -> str:
        raise NotImplementedError

    def wait_for_code(self, *, timeout: float = 180.0, poll: float = 3.0) -> str:
        raise NotImplementedError

    def close(self) -> None:  # pragma: no cover - 默认无资源
        return None


# --------------------------------------------------------------------- mail.tm
@dataclass
class MailTM(Mailbox):
    address: str = ""
    password: str = ""
    token: str = ""
    base: str = "https://api.mail.tm"
    _seen: set[str] = field(default_factory=set)

    @classmethod
    def open(cls, *, timeout: float = 20.0) -> "MailTM":
        with httpx.Client(timeout=timeout, trust_env=False) as client:
            domains = _safe_json(client.get(f"{cls.base}/domains")) or {}
            rows = domains.get("hydra:member") or []
            domain = next((r.get("domain") for r in rows if r.get("isActive")), None)
            if not domain:
                raise EmailRejected("mail.tm 没有可用域名（接口限流或被墙）。")
            for attempt in range(5):
                address = f"bbx{random.randrange(10**7, 10**8)}@{domain}"
                password = "Bbx" + "".join(random.choices("Abcdefghijkmnpqrstuvwxyz23456789", k=12)) + "!9"
                res = client.post(f"{cls.base}/accounts", json={"address": address, "password": password})
                if res.status_code < 300:
                    tok = _safe_json(client.post(f"{cls.base}/token", json={"address": address, "password": password})) or {}
                    return cls(address=address, password=password, token=tok.get("token", ""))
                time.sleep(3 + attempt * 2)
        raise EmailRejected("mail.tm 开盒失败（限流）。")

    def _messages(self) -> list[dict]:
        headers = {"Authorization": f"Bearer {self.token}"}
        with httpx.Client(timeout=20.0, trust_env=False) as client:
            data = _safe_json(client.get(f"{self.base}/messages", headers=headers))
        return (data or {}).get("hydra:member") or []

    def _body(self, message_id: str) -> str:
        headers = {"Authorization": f"Bearer {self.token}"}
        with httpx.Client(timeout=20.0, trust_env=False) as client:
            data = _safe_json(client.get(f"{self.base}/messages/{message_id}", headers=headers))
        if not data:
            return ""
        return "\n".join(list(data.get("html") or []) + [data.get("text") or ""])

    def _scan(self, *, timeout: float, poll: float, want: str) -> str:
        deadline = time.time() + timeout
        while time.time() < deadline:
            for message in self._messages():
                mid = message.get("id")
                if not mid or mid in self._seen:
                    continue
                self._seen.add(mid)
                body = self._body(mid)
                found = extract_verify_link(body) if want == "link" else extract_code(body)
                if found:
                    return found
            time.sleep(poll + random.uniform(0, 1.5))
        raise VerificationPending(f"等邮件超时（{int(timeout)}s）：{self.address}")

    def wait_for_link(self, *, timeout: float = 180.0, poll: float = 3.0) -> str:
        return self._scan(timeout=timeout, poll=poll, want="link")

    def wait_for_code(self, *, timeout: float = 180.0, poll: float = 3.0) -> str:
        return self._scan(timeout=timeout, poll=poll, want="code")


# ------------------------------------------------------------------------ IMAP
@dataclass
class ImapMailbox(Mailbox):
    """自有域名 catch-all：IMAP 收信，按 To 头过滤。"""

    address: str = ""
    host: str = ""
    port: int = 993
    user: str = ""
    password: str = ""
    folder: str = "INBOX"
    use_ssl: bool = True
    _seen_uids: set[bytes] = field(default_factory=set)

    @classmethod
    def from_env(cls, address: str) -> "ImapMailbox":
        host = os.environ.get("BLACKBOX_IMAP_HOST", "").strip()
        user = os.environ.get("BLACKBOX_IMAP_USER", "").strip()
        password = (
            os.environ.get("BLACKBOX_IMAP_PASS", "").strip() or os.environ.get("BLACKBOX_IMAP_PASSWORD", "").strip()
        )
        if not (host and user and password):
            raise EmailRejected("IMAP 未配置：需要 BLACKBOX_IMAP_HOST / BLACKBOX_IMAP_USER / BLACKBOX_IMAP_PASS。")
        return cls(
            address=address,
            host=host,
            port=int(os.environ.get("BLACKBOX_IMAP_PORT", "993") or 993),
            user=user,
            password=password,
            folder=os.environ.get("BLACKBOX_IMAP_FOLDER", "INBOX") or "INBOX",
            use_ssl=os.environ.get("BLACKBOX_IMAP_SSL", "1") not in ("0", "false", "False"),
        )

    def _connect(self) -> imaplib.IMAP4:
        if self.use_ssl:
            conn = imaplib.IMAP4_SSL(self.host, self.port, timeout=30)
        else:
            conn = imaplib.IMAP4(self.host, self.port, timeout=30)
        conn.login(self.user, self.password)
        conn.select(self.folder)
        return conn

    def _scan(self, *, timeout: float, poll: float, want: str) -> str:
        deadline = time.time() + timeout
        needle = self.address.lower()
        while time.time() < deadline:
            try:
                conn = self._connect()
            except (imaplib.IMAP4.error, OSError) as cause:
                raise TransportError(f"IMAP 连接失败：{cause}") from cause
            try:
                criteria: Iterable[str] = ("UNSEEN", "TO", f'"{self.address}"')
                typ, data = conn.search(None, *criteria)
                uids = (data[0] or b"").split() if typ == "OK" else []
                if not uids:
                    typ, data = conn.search(None, "ALL")
                    uids = (data[0] or b"").split() if typ == "OK" else []
                for uid in uids[-40:]:
                    if uid in self._seen_uids:
                        continue
                    typ, payload = conn.fetch(uid, "(RFC822)")
                    if typ != "OK" or not payload or not isinstance(payload[0], tuple):
                        continue
                    message = email.message_from_bytes(payload[0][1])
                    to_header = " ".join(
                        _decode(message.get(h)) for h in ("To", "Delivered-To", "X-Original-To", "Cc")
                    ).lower()
                    subject = _decode(message.get("Subject")).lower()
                    if needle not in to_header and not any(hint in subject for hint in SUBJECT_HINT):
                        continue
                    body = _message_text(message)
                    found = extract_verify_link(body) if want == "link" else extract_code(body)
                    self._seen_uids.add(uid)
                    if found:
                        return found
            finally:
                try:
                    conn.logout()
                except Exception:  # noqa: BLE001 - logout 失败不影响结果
                    pass
            time.sleep(poll + random.uniform(0, 1.5))
        raise VerificationPending(f"等邮件超时（{int(timeout)}s）：{self.address}")

    def wait_for_link(self, *, timeout: float = 180.0, poll: float = 3.0) -> str:
        return self._scan(timeout=timeout, poll=poll, want="link")

    def wait_for_code(self, *, timeout: float = 180.0, poll: float = 3.0) -> str:
        return self._scan(timeout=timeout, poll=poll, want="code")


def _message_text(message: email.message.Message) -> str:
    chunks: list[str] = []
    if message.is_multipart():
        for part in message.walk():
            ctype = part.get_content_type()
            if ctype not in ("text/plain", "text/html"):
                continue
            try:
                payload = part.get_payload(decode=True) or b""
                chunks.append(payload.decode(part.get_content_charset() or "utf-8", errors="replace"))
            except Exception:  # noqa: BLE001
                continue
    else:
        payload = message.get_payload(decode=True) or b""
        chunks.append(payload.decode(message.get_content_charset() or "utf-8", errors="replace"))
    return "\n".join(chunks)


# ------------------------------------------------------------------- 工厂
class DomainMailboxFactory:
    """按配置造信箱；域名轮换与健康分由 antiban 决定用哪个域。"""

    def __init__(self, *, provider: str, domains: list[str], fixed_email: str = ""):
        self.provider = provider
        self.domains = [d for d in domains if d]
        self.fixed_email = fixed_email

    def next_address(self, persona: Persona, *, domain: str | None = None, seed: str = "") -> tuple[str, str]:
        if self.fixed_email:
            return self.fixed_email, self.fixed_email.split("@", 1)[-1]
        if not self.domains:
            raise EmailRejected("没有可用邮箱域名：用 --mail-domains 指定，或配 BLACKBOX_IMAP_* + 自有域。")
        domain = domain or random.choice(self.domains)
        return f"{local_part_for(persona, seed or persona.note + domain)}@{domain}", domain

    def open(self, address: str, *, timeout: float = 20.0) -> Mailbox:
        if self.provider == "mailtm":
            return MailTM.open(timeout=timeout)
        return ImapMailbox.from_env(address)
