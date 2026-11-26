"""Typed failures shared by every OpenAI-compatible line worker."""

from __future__ import annotations


class LineError(RuntimeError):
    kind = "error"

    def __init__(self, message: str, *, detail: object = None) -> None:
        super().__init__(message)
        self.detail = detail


class RejectedDomain(LineError):
    kind = "email_rejected"


class EmailRejected(LineError):
    kind = "email_rejected"


class EmailTaken(LineError):
    kind = "email_taken"


class PasswordRejected(LineError):
    kind = "password_rejected"


class RateLimited(LineError):
    kind = "rate_limited"


class Blocked(LineError):
    kind = "blocked"


class CaptchaRequired(LineError):
    kind = "captcha_required"


class VerificationPending(LineError):
    kind = "verification_pending"


class NoApiKey(LineError):
    kind = "no_api_key"


class Transport(LineError):
    kind = "transport"
