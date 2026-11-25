"""Mailbox adapter: reuse deepai-reg's provider layer instead of forking it.

The two factories need exactly the same primitives (allocate a box, poll it,
extract a link or a 6-digit code), so this module imports the sibling package
when it is present and degrades to a clear error otherwise.
"""

from __future__ import annotations

import re
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any

_SIBLING = Path(__file__).resolve().parents[2] / "deepai-reg"


def _load():
    if _SIBLING.exists() and str(_SIBLING) not in sys.path:
        sys.path.append(str(_SIBLING))
    try:
        from deepai_reg import mailbox as mod  # type: ignore
    except Exception as exc:  # noqa: BLE001
        raise RuntimeError(
            "需要 tools/deepai-reg 存在（邮箱层复用它的 provider 实现）："
            f"{_SIBLING} -> {exc}"
        ) from exc
    return mod


@dataclass(slots=True)
class MailConfigView:
    """The subset of deepai-reg's MailConfig this package fills in."""

    provider: str = "tempmail_lol"
    domain: str = ""
    domains: list[str] | None = None
    auto_domains: bool = True
    address: str = ""
    prefix: str = ""
    imap_host: str = ""
    imap_port: int = 993
    imap_user: str = ""
    imap_password: str = ""
    verify_timeout: float = 180.0
    verify_pattern: str = r"\d{6}"


def provider_for(mail: dict[str, Any], *, proxy: str = ""):
    mod = _load()
    view = MailConfigView(
        provider=str(mail.get("provider") or "tempmail_lol"),
        domain=str(mail.get("domain") or ""),
        domains=[str(d) for d in (mail.get("domains") or [])],
        address=str(mail.get("address") or ""),
        prefix=str(mail.get("prefix") or ""),
        imap_host=str(mail.get("imap_host") or ""),
        imap_user=str(mail.get("imap_user") or ""),
        imap_password=str(mail.get("imap_password") or ""),
    )
    return mod.build_provider(view, proxy=proxy)


def poll_code(provider, box, *, timeout: float = 180.0, poll: float = 5.0) -> str:
    """Return the 6-digit Poe code, or '' when the mailbox stays empty."""
    import time

    deadline = time.time() + timeout
    seen: set[str] = set()
    while time.time() < deadline:
        for message in provider.messages(box):
            blob = f"{message.subject}\n{message.body}\n{message.html}"
            for code in re.findall(r"\b(\d{6})\b", blob):
                if code in ("333333", "111111", "000000"):
                    continue  # template padding, not the real code
                if code not in seen:
                    seen.add(code)
                    return code
        time.sleep(poll)
    return ""
