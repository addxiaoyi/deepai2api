"""Typed failures so the runner can decide "retry / rotate / give up".

Same vocabulary as `deepai_reg.errors` and `allmodels_reg.errors` — the gateway
controller reads the `kind` field off the NDJSON stream, so all three workers
must agree on these names.
"""

from __future__ import annotations


class SignupError(RuntimeError):
    """Base class; `kind` is what the runner switches on."""

    kind = "error"

    def __init__(self, message: str, *, detail: object = None) -> None:
        super().__init__(message)
        self.detail = detail


class EmailRejected(SignupError):
    """Firebase refused the address (bad syntax, disabled domain, blocked)."""

    kind = "email_rejected"


class EmailTaken(SignupError):
    """The address already has a Firebase user."""

    kind = "email_taken"


class PasswordRejected(SignupError):
    kind = "password_rejected"


class RateLimited(SignupError):
    kind = "rate_limited"


class Blocked(SignupError):
    """Edge / WAF / IP reputation: the protocol engine cannot continue."""

    kind = "blocked"


class AppCheckRequired(SignupError):
    """`X-Firebase-AppCheck` missing, stale or rejected by the backend."""

    kind = "app_check_required"


class CaptchaRequired(SignupError):
    """reCAPTCHA Enterprise refused to mint a token for this session."""

    kind = "captcha_required"


class VerificationPending(SignupError):
    """Signed up, but the mailbox never produced the confirmation link."""

    kind = "verification_pending"


class NoCredit(SignupError):
    """Account exists but the wallet cannot serve a chat turn."""

    kind = "no_credit"


class NoApiKey(SignupError):
    """Account exists but no usable credential could be read."""

    kind = "no_api_key"


class Transport(SignupError):
    kind = "transport"
