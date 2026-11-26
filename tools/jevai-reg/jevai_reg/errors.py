"""Typed failures for the jev-ai.org worker (same vocabulary as the other lines)."""

from __future__ import annotations


class JevError(RuntimeError):
    kind = "error"

    def __init__(self, message: str, *, detail: object = None) -> None:
        super().__init__(message)
        self.detail = detail


class EmailRejected(JevError):
    kind = "email_rejected"


class EmailTaken(JevError):
    kind = "email_taken"


class PasswordRejected(JevError):
    kind = "password_rejected"


class RateLimited(JevError):
    kind = "rate_limited"


class Blocked(JevError):
    kind = "blocked"


class CaptchaRequired(JevError):
    kind = "captcha_required"


class VerificationPending(JevError):
    kind = "verification_pending"


class NoApiKey(JevError):
    kind = "no_api_key"


class Transport(JevError):
    kind = "transport"
