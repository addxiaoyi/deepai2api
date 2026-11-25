"""Failure vocabulary shared by protocol, browser and runner.

Every failure carries a `kind` so the runner can decide whether to retry the same
address, burn it, switch engine, or back off the whole lane. The names are the
ones the gateway's job log and the pool's `lastError` field use.
"""

from __future__ import annotations

from typing import Any


class SignupError(Exception):
    """Base class: something about this signup unit failed."""

    kind = "signup_error"
    retryable = False

    def __init__(self, message: str, *, detail: Any = None) -> None:
        super().__init__(message)
        self.detail = detail

    def as_event(self) -> dict[str, Any]:
        return {"kind": self.kind, "reason": str(self), "detail": self.detail}


class Transport(SignupError):
    """Network / proxy / DNS / TLS. Always worth another try."""

    kind = "transport"
    retryable = True


class RateLimited(SignupError):
    """429 from the API or a provider. Back off, do not burn the address."""

    kind = "rate_limited"
    retryable = True

    def __init__(self, message: str, *, retry_after: float = 0.0, detail: Any = None) -> None:
        super().__init__(message, detail=detail)
        self.retry_after = retry_after


class Blocked(SignupError):
    """Cloudflare / WAF challenge or a hard 403 on the API host."""

    kind = "blocked"
    retryable = True


class EmailRejected(SignupError):
    """`This email provider is not supported` / `does not exist`.

    The address is unusable for this target: the domain is on the disposable
    list, or it has no working MX. Burn it, never retry.
    """

    kind = "email_rejected"


class EmailTaken(SignupError):
    """Address already has an account — log in instead of signing up."""

    kind = "email_taken"


class CodeRejected(SignupError):
    """Wrong / expired / too-many-attempts on the OTP. Burn it."""

    kind = "code_rejected"


class CodeMissing(SignupError):
    """Mail never arrived inside the window. Burn it (resend rarely helps)."""

    kind = "code_missing"


class MailboxUnavailable(SignupError):
    """The mailbox provider itself is down (IMAP refused, API 5xx). Retryable."""

    kind = "mailbox_unavailable"
    retryable = True


class NoCredential(SignupError):
    """Verified, but no access_token came back. Account exists, treat as partial."""

    kind = "no_credential"


class BrowserUnavailable(SignupError):
    """No usable browser stack (camoufox/playwright missing, CDP dead)."""

    kind = "browser_unavailable"
    retryable = True


class ProfileLocked(SignupError):
    """The fingerprint profile is in use by another worker."""

    kind = "profile_locked"
    retryable = True
