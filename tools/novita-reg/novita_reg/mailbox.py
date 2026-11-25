"""邮箱：域名 catch-all（首选）/ IMAP 固定信箱 / mail.tm / tempmail.lol。

为什么域名邮箱是首选：Novita 对一次性域名有名单（实测 `CLOUDFLARE_CHECK_ERROR`
之外最常见的拒绝就是域名被判不可用），而自有 catch-all 域名是"真域名"，
且能无限造 local part。IMAP 只负责**收信**，注册地址仍然是 `<local>@<domain>`。

接口统一成三件事：
    mailbox.address          当前收信地址
    mailbox.wait_for_code()  等激活邮件，返回可点链接 / 验证码
    mailbox.close()

所有 provider 都遵守 `verify_timeout`，超时抛 VerificationPending。
"""

from __future__ import annotations

import imaplib
import email
import email.policy
import random
import re
import time
from dataclasses import dataclass
from email.message import EmailMessage
from typing import Any, Iterable

import httpx

from .errors import SignupError, VerificationPending
from .identity import make_persona
from .net import build_client, request

CODE_RE = re.compile(r"\b(\d{6})\b")
LINK_RE = re.compile(r"https?://[^\s\"'<>)\]]+")


@dataclass
class InboxMessage:
    subject: str
    body: str
    received: float

    def links(self) -> list[str]:
        return [m.group(0) for m in LINK_RE.finditer(self.body)]

    def codes(self) -> list[str]:
        return [m.group(1) for m in CODE_RE.finditer(self.body)]


def _html_to_text(html: str) -> str:
    text = re.sub(r"(?is)<(script|style)[^>]*>.*?</\1>", " ", html)
    text = re.sub(r"(?i)<br\s*/?>", "\n", text)
    text = re.sub(r"(?i)</p>", "\n", text)
    text = re.sub(r"<[^>]+>", " ", text)
    return re.sub(r"[ \t]{2,}", " ", text)


def _extract(message: EmailMessage) -> str:
    """把一封邮件压成纯文本，链接与验证码都能正则命中。"""
    chunks: list[str] = []
    if message.is_multipart():
        for part in message.walk():
            ctype = part.get_content_type()
            if ctype not in ("text/plain", "text/html"):
                continue
            try:
                payload = part.get_content()
            except (LookupError, ValueError):
                payload = part.get_payload(decode=True) or b""
                if isinstance(payload, bytes):
                    payload = payload.decode(part.get_content_charset() or "utf-8", "replace")
            chunks.append(_html_to_text(payload) if ctype == "text/html" else str(payload))
    else:
        try:
            payload = message.get_content()
        except (LookupError, ValueError):
            raw = message.get_payload(decode=True) or b""
            payload = raw.decode(message.get_content_charset() or "utf-8", "replace")
        chunks.append(str(payload))
    return "\n".join(chunks)


class BaseMailbox:
    provider = "base"

    def __init__(self, *, verify_timeout: float = 180.0, poll: float = 4.0, proxy: str = "") -> None:
        self.verify_timeout = float(verify_timeout)
        self.poll = float(poll)
        self.proxy = proxy
        self.address = ""

    # -- 子类实现 ---------------------------------------------------------
    def _fetch(self, since: float) -> Iterable[InboxMessage]:
        return ()

    def _cleanup(self) -> None:
        return None

    # -- 公共 -------------------------------------------------------------
    def messages(self, since: float = 0.0) -> list[InboxMessage]:
        try:
            rows = list(self._fetch(since))
        except SignupError:
            raise
        except Exception as exc:  # noqa: BLE001 - 邮箱 API 的异常太杂
            raise SignupError(f"mailbox fetch failed: {exc.__class__.__name__}: {exc}") from exc
        return sorted(rows, key=lambda m: m.received)

    def wait_for(self, *, keywords: Iterable[str] = ("novita", "activate", "verify"), since: float | None = None) -> InboxMessage:
        """等一封含关键词的邮件；超时抛 VerificationPending。"""
        needles = tuple(k.lower() for k in keywords)
        started = time.time()
        since_ts = since if since is not None else started - 5
        while time.time() - started < self.verify_timeout:
            for message in reversed(self.messages(since_ts)):
                haystack = f"{message.subject}\n{message.body}".lower()
                if not needles or any(n in haystack for n in needles):
                    return message
            time.sleep(self.poll)
        raise VerificationPending(
            f"{self.address}: 等激活邮件超时（{self.verify_timeout:.0f}s）",
            detail={"provider": self.provider, "address": self.address},
        )

    def activation_link(self, message: InboxMessage, *, must_contain: str = "") -> str:
        """从邮件里挑出真正能激活的那条链接。"""
        links = message.links()
        for link in links:
            lowered = link.lower()
            if must_contain and must_contain.lower() not in lowered:
                continue
            if any(hint in lowered for hint in ("activate", "active", "verify", "token", "confirm")):
                return link
        for link in links:
            if "novita" in link.lower():
                return link
        return links[0] if links else ""

    def close(self) -> None:
        self._cleanup()


class DomainMailbox(BaseMailbox):
    """自有域名 catch-all：造地址不花钱，收信走 IMAP。

    这是唯一能"无限注册"的形态 —— 其它免费 API 都按 IP 限流。
    """

    provider = "domain"

    def __init__(
        self,
        *,
        domain: str,
        imap_host: str,
        imap_port: int = 993,
        imap_user: str = "",
        imap_password: str = "",
        address: str = "",
        local_prefix: str = "",
        verify_timeout: float = 180.0,
        poll: float = 4.0,
        proxy: str = "",
    ) -> None:
        super().__init__(verify_timeout=verify_timeout, poll=poll, proxy=proxy)
        self.domain = domain.strip().lower()
        self.imap_host = imap_host
        self.imap_port = int(imap_port or 993)
        self.imap_user = imap_user or address or f"inbox@{self.domain}"
        self.imap_password = imap_password
        self._conn: imaplib.IMAP4_SSL | None = None
        if address:
            self.address = address
        else:
            persona = make_persona(domain=self.domain, local_prefix=local_prefix)
            self.address = persona.email

    def _connect(self) -> imaplib.IMAP4_SSL:
        if self._conn is not None:
            return self._conn
        if not self.imap_host:
            raise SignupError("domain 邮箱缺 imap_host：只能收信，不能注册", detail={"domain": self.domain})
        conn = imaplib.IMAP4_SSL(self.imap_host, self.imap_port, timeout=30)
        conn.login(self.imap_user, self.imap_password)
        conn.select("INBOX")
        self._conn = conn
        return conn

    def _fetch(self, since: float) -> Iterable[InboxMessage]:
        conn = self._connect()
        # IMAP 的 SINCE 只到天粒度；用当天 + 本地时间过滤兜底。
        day = time.strftime("%d-%b-%Y", time.localtime(since - 86400))
        typ, data = conn.search(None, f'(SINCE "{day}")')
        if typ != "OK":
            return []
        ids = (data[0] or b"").split()
        out: list[InboxMessage] = []
        for raw_id in ids[-40:]:
            typ, payload = conn.fetch(raw_id, "(RFC822)")
            if typ != "OK" or not payload:
                continue
            blob = payload[0]
            if not isinstance(blob, tuple):
                continue
            message = email.message_from_bytes(blob[1], policy=email.policy.default)
            to_header = str(message.get("To", "")).lower()
            if self.address.lower() not in to_header and self.domain not in to_header:
                continue
            received = time.time()
            out.append(InboxMessage(subject=str(message.get("Subject", "")), body=_extract(message), received=received))
        return out

    def _cleanup(self) -> None:
        if self._conn is not None:
            try:
                self._conn.logout()
            except Exception:  # noqa: BLE001
                pass
            self._conn = None


class MailTmMailbox(BaseMailbox):
    """mail.tm：免费、无需 key，但按 IP 限流，且域名会被站点拉黑。"""

    provider = "mailtm"
    BASE = "https://api.mail.tm"

    def __init__(self, *, domain: str = "", verify_timeout: float = 180.0, poll: float = 4.0, proxy: str = "") -> None:
        super().__init__(verify_timeout=verify_timeout, poll=poll, proxy=proxy)
        self.client = build_client(proxy=proxy, timeout=30.0, base_url=self.BASE)
        self.token = ""
        self._bootstrap(domain)

    def _domains(self) -> list[str]:
        response = request(self.client, "GET", "/domains?page=1")
        data = response.json() if response.status_code == 200 else {}
        rows = data.get("hydra:member") if isinstance(data, dict) else None
        return [row.get("domain", "") for row in (rows or []) if row.get("domain")]

    def _bootstrap(self, domain: str) -> None:
        candidates = [domain] if domain else self._domains()
        if not candidates:
            raise SignupError("mail.tm 没有可用域名")
        last_error = ""
        for candidate in candidates[:6]:
            persona = make_persona(domain=candidate)
            password = persona.password
            created = request(
                self.client,
                "POST",
                "/accounts",
                json={"address": persona.email, "password": password},
            )
            if created.status_code not in (200, 201):
                last_error = created.text[:200]
                continue
            token = request(self.client, "POST", "/token", json={"address": persona.email, "password": password})
            if token.status_code != 200:
                last_error = token.text[:200]
                continue
            self.token = token.json().get("token", "")
            self.address = persona.email
            self.client.headers["Authorization"] = f"Bearer {self.token}"
            return
        raise SignupError(f"mail.tm 建信箱失败：{last_error}")

    def _fetch(self, since: float) -> Iterable[InboxMessage]:
        listing = request(self.client, "GET", "/messages?page=1")
        if listing.status_code != 200:
            return []
        rows = listing.json().get("hydra:member") or []
        out: list[InboxMessage] = []
        for row in rows[:20]:
            msg_id = row.get("id")
            if not msg_id:
                continue
            detail = request(self.client, "GET", f"/messages/{msg_id}")
            if detail.status_code != 200:
                continue
            data = detail.json()
            body = "\n".join(
                part for part in (data.get("text") or "", _html_to_text(data.get("html") or "")) if part
            )
            out.append(
                InboxMessage(
                    subject=str(data.get("subject") or ""),
                    body=body,
                    received=time.time(),
                )
            )
        return out

    def _cleanup(self) -> None:
        try:
            self.client.close()
        except Exception:  # noqa: BLE001
            pass


class TempMailLolMailbox(BaseMailbox):
    """tempmail.lol：一次调用拿地址，轮询 /inbox。域名多为一次性，容易被拒。"""

    provider = "tempmail_lol"
    BASE = "https://api.tempmail.lol/v2"

    def __init__(self, *, domain: str = "", verify_timeout: float = 180.0, poll: float = 4.0, proxy: str = "") -> None:
        super().__init__(verify_timeout=verify_timeout, poll=poll, proxy=proxy)
        self.client = build_client(proxy=proxy, timeout=30.0)
        body: dict[str, Any] = {}
        if domain:
            body["domain"] = domain
        response = request(self.client, "POST", f"{self.BASE}/inbox/create", json=body or None)
        if response.status_code not in (200, 201):
            raise SignupError(f"tempmail.lol 建信箱失败：HTTP {response.status_code} {response.text[:160]}")
        data = response.json()
        self.address = str(data.get("address") or "")
        self.token = str(data.get("token") or "")
        if not (self.address and self.token):
            raise SignupError("tempmail.lol 返回缺 address/token")

    def _fetch(self, since: float) -> Iterable[InboxMessage]:
        response = request(self.client, "GET", f"{self.BASE}/inbox?token={self.token}")
        if response.status_code != 200:
            return []
        data = response.json()
        out: list[InboxMessage] = []
        for row in (data.get("emails") or [])[:20]:
            out.append(
                InboxMessage(
                    subject=str(row.get("subject") or ""),
                    body="\n".join(
                        part for part in (row.get("body") or "", _html_to_text(row.get("html") or "")) if part
                    ),
                    received=float(row.get("date") or time.time()),
                )
            )
        return out

    def _cleanup(self) -> None:
        try:
            self.client.close()
        except Exception:  # noqa: BLE001
            pass


class FixedMailbox(DomainMailbox):
    """固定地址 + plus 分号：`you+tag@domain`。适合自测与"一个信箱多账号"。"""

    provider = "fixed"

    def __init__(self, *, address: str, **kwargs: Any) -> None:
        domain = address.split("@")[-1]
        super().__init__(address=address, domain=domain, **kwargs)


def build_mailbox(cfg: Any, *, proxy: str = "", rng: random.Random | None = None) -> BaseMailbox:
    """按配置造信箱。`cfg` 是 Config（含 mail / anti_ban）。"""
    mail = cfg.mail
    common = {"verify_timeout": mail.verify_timeout, "proxy": proxy}
    provider = (mail.provider or "domain").lower()
    if provider in ("domain", "imap"):
        domain = mail.domain
        if not domain and mail.domains:
            rng = rng or random.Random()
            domain = rng.choice(mail.domains)
        return DomainMailbox(
            domain=domain,
            imap_host=mail.imap_host,
            imap_port=mail.imap_port,
            imap_user=mail.imap_user,
            imap_password=mail.imap_password,
            local_prefix=mail.prefix,
            **common,
        )
    if provider == "fixed":
        return FixedMailbox(
            address=mail.address or f"inbox@{mail.domain or 'example.com'}",
            imap_host=mail.imap_host,
            imap_port=mail.imap_port,
            imap_user=mail.imap_user,
            imap_password=mail.imap_password,
            **common,
        )
    if provider == "mailtm":
        return MailTmMailbox(domain=mail.domain, **common)
    if provider == "tempmail_lol":
        return TempMailLolMailbox(domain=mail.domain, **common)
    raise SignupError(f"未知邮箱 provider：{provider}")
