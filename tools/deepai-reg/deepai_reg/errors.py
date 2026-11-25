"""Typed failures so the runner can decide "retry / rotate / give up"."""

from __future__ import annotations


class SignupError(RuntimeError):
    """Base class; `kind` is what the runner switches on."""

    kind = "error"

    def __init__(self, message: str, *, detail: object = None) -> None:
        super().__init__(message)
        self.detail = detail


class EmailRejected(SignupError):
    """DeepAI refused the address itself (disposable domain, bad syntax, ...)."""

    kind = "email_rejected"


class EmailTaken(SignupError):
    """The address already has an account."""

    kind = "email_taken"


class PasswordRejected(SignupError):
    kind = "password_rejected"


class RateLimited(SignupError):
    kind = "rate_limited"


class Blocked(SignupError):
    """Cloudflare / WAF / bot gate: the protocol engine cannot continue."""

    kind = "blocked"


class NoApiKey(SignupError):
    """Account exists but the profile carried no api_key."""

    kind = "no_api_key"


class Transport(SignupError):
    kind = "transport"
