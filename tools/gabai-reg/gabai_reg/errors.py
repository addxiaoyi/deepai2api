"""Typed failures for the gab.ai worker.

Same vocabulary as `deepai_reg.errors` / `allmodels_reg.errors` so the gateway
controller, the NDJSON stream and the retry policy all speak one language:

    email_rejected · email_taken · password_rejected · rate_limited · blocked
    captcha_required · verification_pending · no_api_key · plus_required
    transport · error
"""

from __future__ import annotations


class SignupError(RuntimeError):
    """Base class; `kind` is what the runner switches on."""

    kind = "error"

    def __init__(self, message: str, *, detail: object = None) -> None:
        super().__init__(message)
        self.detail = detail


class EmailRejected(SignupError):
    """gab.ai refused the address itself (syntax, blocked / disposable domain)."""

    kind = "email_rejected"


class EmailTaken(SignupError):
    """The address already has a gab.ai account."""

    kind = "email_taken"


class PasswordRejected(SignupError):
    kind = "password_rejected"


class RateLimited(SignupError):
    kind = "rate_limited"


class Blocked(SignupError):
    """Cloudflare / WAF / bot gate: the protocol engine cannot continue."""

    kind = "blocked"


class CaptchaRequired(SignupError):
    """Turnstile wanted a token and none (or a stale one) was supplied."""

    kind = "captcha_required"


class VerificationPending(SignupError):
    """Signed up, but the mailbox never produced the confirmation link."""

    kind = "verification_pending"


class NoApiKey(SignupError):
    """Account exists but no `gab_…` key could be read or minted."""

    kind = "no_api_key"


class PlusRequired(SignupError):
    """Account created fine, but the developer API needs Plus.

    Not a signup failure — it is an account-tier fact. The worker keeps the
    account (it is still a valid pool member for chat/web use) and records
    `api_blocked = plus_required` so the gateway stops sending API traffic at it.
    """

    kind = "plus_required"


class Transport(SignupError):
    kind = "transport"
