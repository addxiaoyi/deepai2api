"""域名邮箱 / 临时邮箱提供者 —— 注册地址（以及验证邮件）从哪来。

四个来源，按"耐用程度"排序：

  imap         : 自有 catch-all 域名（最耐用，能过大多数一次性域名黑名单）
  fixed        : 一个自己控制的地址，按 plus 分号给每个账号
  mailtm       : 公共 API，盒子约 7 天
  tempmail_lol : 公共 API，每次随机域名

WaveSpeed 本身没有邮箱验证环节（OAuth 由 Google/GitHub 提供），但：
  * OAuth 提供方可能要求邮箱是"可控"的（GitHub 注册要验证邮箱）；
  * novita 这类站点要验证链接，同一套 Provider 接口在两边通用；
所以这里保留完整实现，`wait_for_link` 的 must_contain 由调用方决定。
"""

from __future__ import annotations

import email
import imaplib
import random
import re
import string
import threading
import time
from dataclasses import dataclass, field
from typing import Any

from .errors import SignupError, Transport
from .net import build_client, request

TEMPMAIL_API = "https://api.tempmail.lol/v2"
MAILTM_API = "https://api.mail.tm"

# 多线程同时建盒子时公共 API 会直接 429；全局节流。
_create_lock = threading.Lock()
_last_create = 0.0
CREATE_INTERVAL = 1.5


def _pace() -> None:
    global _last_create
    with _create_lock:
        wait = CREATE_INTERVAL - (time.time() - _last_create)
        if wait > 0:
            time.sleep(wait)
        _last_create = time.time()


def _post_with_retry(client: Any, url: str, *, json_body: dict, tries: int = 4) -> Any:
    response = None
    for attempt in range(tries):
        _pace()
        response = request(client, "POST", url, json=json_body)
        if response.status_code != 429:
            return response
        time.sleep(2.5 * (attempt + 1))
    return response


@dataclass(slots=True)
class Mailbox:
    address: str
    provider: str
    token: str = ""
    meta: dict[str, Any] = field(default_factory=dict)


@dataclass(slots=True)
class Message:
    subject: str = ""
    sender: str = ""
    body: str = ""
    html: str = ""
    received: str = ""


class Provider:
    name = "base"

    def allocate(self, local_part: str = "", domain: str = "") -> Mailbox:  # pragma: no cover - interface
        raise NotImplementedError

    def messages(self, box: Mailbox) -> list[Message]:  # pragma: no cover - interface
        raise NotImplementedError

    def close(self) -> None:
        return None

    def domains(self) -> list[str]:
        return []

    # ------------------------------------------------------------- utilities
    def wait_for_link(
        self,
        box: Mailbox,
        *,
        timeout: float = 180.0,
        pattern: str = r"https?://[^\s\"'<>]+",
        must_contain: tuple[str, ...] = ("verify", "token"),
        poll: float = 5.0,
    ) -> str:
        """收件箱里第一个像"确认链接"的 URL。"""
        deadline = time.time() + timeout
        seen: set[str] = set()
        while time.time() < deadline:
            for message in self.messages(box):
                blob = f"{message.body}\n{message.html}"
                for link in re.findall(pattern, blob.replace("&amp;", "&")):
                    if link in seen:
                        continue
                    seen.add(link)
                    lowered = link.lower()
                    if not must_contain or any(k in lowered for k in must_contain):
                        return link
            time.sleep(poll)
        return ""


class TempmailLol(Provider):
    """https://tempmail.lol —— POST /inbox/create, GET /inbox?token="""

    name = "tempmail_lol"

    def __init__(self, *, domain: str = "", proxy: str = "", timeout: float = 30.0) -> None:
        self.domain = domain
        self.client = build_client(proxy=proxy, timeout=timeout)

    def allocate(self, local_part: str = "", domain: str = "") -> Mailbox:
        payload: dict[str, str] = {}
        chosen = (domain or self.domain).strip()
        if chosen:
            payload["domain"] = chosen
        response = _post_with_retry(self.client, f"{TEMPMAIL_API}/inbox/create", json_body=payload)
        if response.status_code >= 400:
            raise SignupError(
                f"tempmail.lol refused to create a box (HTTP {response.status_code})",
                detail=response.text[:200],
            )
        data = response.json()
        address = str(data.get("address", ""))
        if "@" not in address:
            raise SignupError("tempmail.lol returned no usable address", detail=str(data)[:200])
        return Mailbox(address=address, provider=self.name, token=str(data.get("token", "")))

    def messages(self, box: Mailbox) -> list[Message]:
        if not box.token:
            return []
        response = request(self.client, "GET", f"{TEMPMAIL_API}/inbox", params={"token": box.token})
        if response.status_code >= 400:
            return []
        out: list[Message] = []
        for item in (response.json() or {}).get("emails") or []:
            out.append(
                Message(
                    subject=str(item.get("subject", "")),
                    sender=str(item.get("from", "")),
                    body=str(item.get("body", "")),
                    html=str(item.get("html", "")),
                    received=str(item.get("date", "")),
                )
            )
        return out

    def close(self) -> None:
        self.client.close()

    def domains(self) -> list[str]:
        return [self.domain] if self.domain else []


class MailTm(Provider):
    """https://mail.tm —— 先建账号，再用 JWT 读信。"""

    name = "mailtm"

    def __init__(self, *, domain: str = "", proxy: str = "", timeout: float = 30.0) -> None:
        self.domain = domain
        self.client = build_client(proxy=proxy, timeout=timeout)

    def _domain(self) -> str:
        if self.domain:
            return self.domain
        response = request(self.client, "GET", f"{MAILTM_API}/domains", params={"page": 1})
        if response.status_code >= 400:
            raise SignupError(f"mail.tm domain list failed (HTTP {response.status_code})")
        members = (response.json() or {}).get("hydra:member") or []
        active = [m for m in members if m.get("isActive")]
        if not active:
            raise SignupError("mail.tm has no active domain right now")
        return str(active[0]["domain"])

    def allocate(self, local_part: str = "", domain: str = "") -> Mailbox:
        domain = domain or self._domain()
        local = local_part or "u" + "".join(random.choices(string.ascii_lowercase + string.digits, k=9))
        local = re.sub(r"[^a-z0-9._-]", "", local.lower())[:32] or "u" + str(int(time.time()))
        address = f"{local}@{domain}"
        password = "M" + "".join(random.choices(string.ascii_letters + string.digits, k=14)) + "!7"
        created = _post_with_retry(
            self.client, f"{MAILTM_API}/accounts", json_body={"address": address, "password": password}
        )
        if created.status_code not in (200, 201):
            raise SignupError(f"mail.tm refused the box (HTTP {created.status_code})", detail=created.text[:200])
        token = request(self.client, "POST", f"{MAILTM_API}/token", json={"address": address, "password": password})
        if token.status_code >= 400:
            raise SignupError(f"mail.tm token failed (HTTP {token.status_code})")
        return Mailbox(
            address=address,
            provider=self.name,
            token=str(token.json().get("token", "")),
            meta={"password": password},
        )

    def messages(self, box: Mailbox) -> list[Message]:
        headers = {"Authorization": f"Bearer {box.token}"}
        listing = request(self.client, "GET", f"{MAILTM_API}/messages", headers=headers)
        if listing.status_code >= 400:
            return []
        out: list[Message] = []
        for item in listing.json() or []:
            detail = request(self.client, "GET", f"{MAILTM_API}/messages/{item['id']}", headers=headers)
            body = ""
            html = ""
            if detail.status_code < 400:
                payload = detail.json() or {}
                html = "\n".join(payload.get("html") or [])
                body = "\n".join(payload.get("text") or [])
            out.append(
                Message(
                    subject=str(item.get("subject", "")),
                    sender=str((item.get("from") or {}).get("address", "")),
                    body=body,
                    html=html,
                    received=str(item.get("createdAt", "")),
                )
            )
        return out

    def close(self) -> None:
        self.client.close()

    def domains(self) -> list[str]:
        try:
            response = request(self.client, "GET", f"{MAILTM_API}/domains", params={"page": 1})
            if response.status_code >= 400:
                return []
            members = (response.json() or {}).get("hydra:member") or []
            return [str(m["domain"]) for m in members if m.get("isActive")]
        except SignupError:
            return []


class ImapCatchAll(Provider):
    """自有域名 + catch-all 规则；用 IMAPS 轮询收件箱。"""

    name = "imap"

    def __init__(
        self,
        *,
        host: str,
        user: str,
        password: str,
        domain: str,
        prefix: str = "",
        port: int = 993,
        timeout: float = 30.0,
    ) -> None:
        if not (host and user and password and domain):
            raise SignupError("imap provider needs host, user, password and domain")
        self.host = host
        self.user = user
        self.password = password
        self.domain = domain
        self.prefix = prefix
        self.port = port
        self.timeout = timeout

    def allocate(self, local_part: str = "", domain: str = "") -> Mailbox:
        tag = local_part or "".join(random.choices(string.ascii_lowercase + string.digits, k=10))
        tag = re.sub(r"[^a-z0-9._-]", "", tag.lower())
        address = f"{self.prefix}{tag}@{domain or self.domain}"
        return Mailbox(address=address, provider=self.name, token=tag)

    def messages(self, box: Mailbox) -> list[Message]:
        out: list[Message] = []
        handle: imaplib.IMAP4_SSL | None = None
        try:
            handle = imaplib.IMAP4_SSL(self.host, self.port)
            handle.login(self.user, self.password)
            handle.select("INBOX")
            query = f'(TO "{box.address}")'
            status, data = handle.search(None, "UNSEEN", query)
            if status != "OK":
                status, data = handle.search(None, query)
            ids = (data[0] or b"").split()
            for msg_id in ids[-10:]:
                status, payload = handle.fetch(msg_id, "(RFC822)")
                if status != "OK" or not payload or not isinstance(payload[0], tuple):
                    continue
                parsed = email.message_from_bytes(payload[0][1])
                body = ""
                html = ""
                if parsed.is_multipart():
                    for part in parsed.walk():
                        ctype = part.get_content_type()
                        try:
                            chunk = part.get_payload(decode=True) or b""
                        except Exception:  # noqa: BLE001 - MIME 畸形很常见
                            continue
                        text = chunk.decode(part.get_content_charset() or "utf-8", "replace")
                        if ctype == "text/html":
                            html += text
                        elif ctype == "text/plain":
                            body += text
                else:
                    chunk = parsed.get_payload(decode=True) or b""
                    body = chunk.decode(parsed.get_content_charset() or "utf-8", "replace")
                out.append(
                    Message(
                        subject=str(parsed.get("subject", "")),
                        sender=str(parsed.get("from", "")),
                        body=body,
                        html=html,
                        received=str(parsed.get("date", "")),
                    )
                )
        except (imaplib.IMAP4.error, OSError, Transport) as exc:
            raise Transport(f"imap poll failed: {exc}") from exc
        finally:
            if handle is not None:
                try:
                    handle.close()
                except Exception:  # noqa: BLE001 - 死会话上 close 会抛
                    pass
                try:
                    handle.logout()
                except Exception:  # noqa: BLE001
                    pass
        return out

    def domains(self) -> list[str]:
        return [self.domain] if self.domain else []


class FixedAddress(Provider):
    """一个自己控制的地址；每个账号一个 plus 标签。"""

    name = "fixed"

    def __init__(self, *, address: str, messages: list[Message] | None = None, proxy: str = "", timeout: float = 30.0) -> None:
        if "@" not in address:
            raise SignupError("fixed provider needs a full address (user@domain)")
        self.address = address
        self.client = build_client(proxy=proxy, timeout=timeout) if proxy else None
        self._messages = list(messages or [])

    def allocate(self, local_part: str = "", domain: str = "") -> Mailbox:
        user, host = self.address.split("@", 1)
        tag = local_part or "".join(random.choices(string.ascii_lowercase + string.digits, k=8))
        tag = re.sub(r"[^a-z0-9._-]", "", tag.lower())
        base = user.split("+", 1)[0]
        return Mailbox(address=f"{base}+{tag}@{host}", provider=self.name, token=tag)

    def feed(self, messages: list[Message]) -> None:
        """测试钩子：假装收件箱里已经有这些信。"""
        self._messages = list(messages)

    def messages(self, box: Mailbox) -> list[Message]:
        return list(self._messages)

    def close(self) -> None:
        if self.client is not None:
            self.client.close()

    def domains(self) -> list[str]:
        return [self.address.split("@", 1)[1]]


def build_provider(mail_cfg: Any, *, proxy: str = "") -> Provider:
    name = (getattr(mail_cfg, "provider", "") or "imap").strip().lower()
    if name in ("imap", "domain"):
        return ImapCatchAll(
            host=mail_cfg.imap_host,
            user=mail_cfg.imap_user,
            password=mail_cfg.imap_password,
            domain=mail_cfg.domain or (mail_cfg.domains[0] if mail_cfg.domains else ""),
            prefix=mail_cfg.prefix,
            port=mail_cfg.imap_port,
        )
    if name == "tempmail_lol":
        return TempmailLol(domain=mail_cfg.domain, proxy=proxy)
    if name == "mailtm":
        return MailTm(domain=mail_cfg.domain, proxy=proxy)
    if name == "fixed":
        return FixedAddress(address=mail_cfg.address, proxy=proxy)
    raise SignupError(f"unknown mail provider: {getattr(mail_cfg, 'provider', '')}")
