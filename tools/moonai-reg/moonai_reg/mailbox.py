"""域名邮箱。

三种模式（与 `--mail-provider` 对应）：

    mailtm   api.mail.tm 现开临时信箱（自己域名没配好时的兜底；域名会被站点拒）
    imap     自有域名 catch-all + IMAP 收信（**推荐**：域名干净、可控、可轮换）
    domain   同 imap，但用 `--mail-domains` 里的域轮换生成地址
    fixed    用 `--email` 指定的地址（仍要 IMAP 才能收到验证信）

关键点：moonai 的验证链接是
    https://auth.mooonai.com/auth/verify-email/?token=<uuid>
邮件正文里它有时被 mailtracker 包成 https://syjkx.mjt.lu/lnk/... ，
所以先直匹配，匹配不到再跟随 tracker 的 302。

IMAP 环境变量（imap / domain / fixed 用）：
    MOONAI_IMAP_HOST  MOONAI_IMAP_PORT(993)  MOONAI_IMAP_USER  MOONAI_IMAP_PASS
    MOONAI_IMAP_SSL(1)  MOONAI_IMAP_FOLDER(INBOX)
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

from .config import VERIFY_LINK_RE
from .errors import EmailRejected, TransportError, VerificationPending
from .identity import Persona, local_part_for

VERIFY_RE = re.compile(VERIFY_LINK_RE)
TRACKER_RE = re.compile(r"https://[a-z0-9.]*mjt\.lu/lnk/[^\"'\s<>]+", re.I)
SUBJECT_HINT = ("подтверд", "verify", "confirm", "welcome", "добро пожаловать")


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
    """mail.tm 限流时会返回 HTML / 空体；解析失败一律当"没数据"。"""
    if res is None or res.status_code >= 300:
        return None
    try:
        data = res.json()
    except Exception:  # noqa: BLE001 - JSONDecodeError / ValueError 都算
        return None
    return data if isinstance(data, dict) else None


def extract_verify_link(text: str) -> str | None:
    """从一封邮件正文里抠出验证链接（直链优先，tracker 其次）。"""
    if not text:
        return None
    direct = VERIFY_RE.search(text.replace("&amp;", "&"))
    if direct:
        return direct.group(0)
    return None


def extract_tracker_link(text: str) -> str | None:
    match = TRACKER_RE.search(text.replace("&amp;", "&"))
    return match.group(0) if match else None


def resolve_tracker(link: str, *, timeout: float = 20.0) -> str | None:
    """跟随 mjt.lu 的 302，拿到真实 verify-email 地址。"""
    try:
        with httpx.Client(follow_redirects=False, timeout=timeout, trust_env=False) as client:
            for _ in range(5):
                res = client.get(link, headers={"User-Agent": "Mozilla/5.0"})
                location = res.headers.get("location")
                if not location:
                    return None
                if "verify-email" in location:
                    return location
                link = location
    except httpx.HTTPError:
        return None
    return None


class Mailbox:
    """邮箱抽象：所有实现都要能 `wait_for_verify_link`。"""

    address: str = ""

    def wait_for_verify_link(self, *, timeout: float = 180.0, poll: float = 3.0) -> str:
        raise NotImplementedError

    def close(self) -> None:  # pragma: no cover - 默认无资源
        return None


# --------------------------------------------------------------------- mail.tm
@dataclass
class MailTM(Mailbox):
    """api.mail.tm 的开盒 + 轮询。开盒接口限流，所以调用方要串行。"""

    address: str = ""
    password: str = ""
    token: str = ""
    base: str = "https://api.mail.tm"
    _seen: set[str] = field(default_factory=set)

    @classmethod
    def open(cls, *, timeout: float = 20.0) -> "MailTM":
        with httpx.Client(timeout=timeout, trust_env=False) as client:
            domains = _safe_json(client.get(f"{cls.base}/domains"))
            rows = (domains or {}).get("hydra:member") or []
            domain = next((r.get("domain") for r in rows if r.get("isActive")), None)
            if not domain:
                raise EmailRejected("mail.tm 没有可用域名（接口限流或被墙）。")
            for attempt in range(5):
                address = f"moon{random.randrange(10**7, 10**8)}@{domain}"
                password = "Moon" + "".join(random.choices("Abcdefghijkmnpqrstuvwxyz23456789", k=12)) + "!9"
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
        if not data:
            return []
        return data.get("hydra:member") or []

    def _body(self, message_id: str) -> str:
        headers = {"Authorization": f"Bearer {self.token}"}
        with httpx.Client(timeout=20.0, trust_env=False) as client:
            data = _safe_json(client.get(f"{self.base}/messages/{message_id}", headers=headers))
        if not data:
            return ""
        html = "\n".join(data.get("html") or [])
        return "\n".join([html, data.get("text") or ""])

    def wait_for_verify_link(self, *, timeout: float = 180.0, poll: float = 3.0) -> str:
        deadline = time.time() + timeout
        while time.time() < deadline:
            for message in self._messages():
                mid = message.get("id")
                if not mid or mid in self._seen:
                    continue
                self._seen.add(mid)
                body = self._body(mid)
                link = extract_verify_link(body)
                if link:
                    return link
                tracker = extract_tracker_link(body)
                if tracker:
                    resolved = resolve_tracker(tracker)
                    if resolved:
                        return resolved
            time.sleep(poll + random.uniform(0, 1.5))
        raise VerificationPending(f"等验证邮件超时（{int(timeout)}s）：{self.address}")


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
    def from_env(cls, address: str, *, timeout: float = 20.0) -> "ImapMailbox":
        host = os.environ.get("MOONAI_IMAP_HOST", "").strip()
        user = os.environ.get("MOONAI_IMAP_USER", "").strip()
        password = os.environ.get("MOONAI_IMAP_PASS", "").strip()
        if not (host and user and password):
            raise EmailRejected(
                "IMAP 未配置：需要 MOONAI_IMAP_HOST / MOONAI_IMAP_USER / MOONAI_IMAP_PASS。"
            )
        return cls(
            address=address,
            host=host,
            port=int(os.environ.get("MOONAI_IMAP_PORT", "993") or 993),
            user=user,
            password=password,
            folder=os.environ.get("MOONAI_IMAP_FOLDER", "INBOX") or "INBOX",
            use_ssl=os.environ.get("MOONAI_IMAP_SSL", "1") not in ("0", "false", "False"),
        )

    def _connect(self) -> imaplib.IMAP4:
        if self.use_ssl:
            conn = imaplib.IMAP4_SSL(self.host, self.port, timeout=30)
        else:
            conn = imaplib.IMAP4(self.host, self.port, timeout=30)
        conn.login(self.user, self.password)
        conn.select(self.folder)
        return conn

    def wait_for_verify_link(self, *, timeout: float = 180.0, poll: float = 3.0) -> str:
        deadline = time.time() + timeout
        needle = self.address.lower()
        while time.time() < deadline:
            try:
                conn = self._connect()
            except (imaplib.IMAP4.error, OSError) as cause:
                raise TransportError(f"IMAP 连接失败：{cause}") from cause
            try:
                # 先在服务端按收件人过滤，再本地兜底（有些服务不支持 TO 搜索）
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
                    subject = _decode(message.get("Subject"))
                    if needle not in to_header and not any(h in subject.lower() for h in SUBJECT_HINT):
                        continue
                    body = _message_text(message)
                    link = extract_verify_link(body)
                    if link:
                        self._seen_uids.add(uid)
                        return link
                    tracker = extract_tracker_link(body)
                    if tracker:
                        resolved = resolve_tracker(tracker)
                        if resolved:
                            self._seen_uids.add(uid)
                            return resolved
                    self._seen_uids.add(uid)
            finally:
                try:
                    conn.logout()
                except Exception:  # noqa: BLE001 - logout 失败不影响结果
                    pass
            time.sleep(poll + random.uniform(0, 1.5))
        raise VerificationPending(f"等验证邮件超时（{int(timeout)}s）：{self.address}")


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

    def next_address(self, persona: Persona, *, domain: str | None = None) -> tuple[str, str]:
        """返回 (email, domain)。"""
        if self.fixed_email:
            domain = self.fixed_email.split("@", 1)[-1]
            return self.fixed_email, domain
        if not self.domains:
            raise EmailRejected("没有可用邮箱域名：用 --mail-domains 指定，或 MOONAI_IMAP_* + 域名。")
        domain = domain or random.choice(self.domains)
        return f"{local_part_for(persona, persona.note + domain)}@{domain}", domain

    def open(self, address: str, *, timeout: float = 20.0) -> Mailbox:
        if self.provider == "mailtm":
            return MailTM.open(timeout=timeout)
        return ImapMailbox.from_env(address, timeout=timeout)
