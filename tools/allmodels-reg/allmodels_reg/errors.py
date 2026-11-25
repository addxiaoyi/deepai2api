"""Typed failures so the runner can decide "retry / rotate / give up".

Mirrors `deepai_reg.errors` on purpose: the two workers share a vocabulary, so
the gateway controller and the NDJSON event stream read the same either way.
"""

from __future__ import annotations


class SignupError(RuntimeError):
    """Base class; `kind` is what the runner switches on."""

    kind = "error"

    def __init__(self, message: str, *, detail: object = None) -> None:
        super().__init__(message)
        self.detail = detail


class EmailRejected(SignupError):
    """The dashboard refused the address itself (bad syntax, blocked domain...)."""

    kind = "email_rejected"


class EmailTaken(SignupError):
    """The address already has an Allmodels account."""

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
    """Account exists but no key could be read or minted."""

    kind = "no_api_key"


class Transport(SignupError):
    kind = "transport"
