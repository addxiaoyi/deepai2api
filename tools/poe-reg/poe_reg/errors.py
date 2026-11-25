"""Failure taxonomy — the runner's retry policy switches on `kind`."""

from __future__ import annotations


class PoeError(RuntimeError):
    kind = "error"

    def __init__(self, message: str, *, detail: object = None) -> None:
        super().__init__(message)
        self.detail = detail


class CloudflareBlocked(PoeError):
    """Edge challenge did not clear inside the budget."""

    kind = "cloudflare"


class EmailRejected(PoeError):
    kind = "email_rejected"


class EmailExists(PoeError):
    """Poe already has an account for that address (login flow, not signup)."""

    kind = "email_exists"


class AutomatedRequest(PoeError):
    """`automated_request_detected` — IP/device reputation, cool the proxy down."""

    kind = "automated_request"


class CodeNotDelivered(PoeError):
    kind = "code_missing"


class CodeRejected(PoeError):
    kind = "code_rejected"


class PhoneRequired(PoeError):
    """Account exists and is email-verified, but Poe wants an SMS step."""

    kind = "phone_required"


class SmsFailed(PoeError):
    kind = "sms_failed"


class NoSession(PoeError):
    kind = "no_session"


class Transport(PoeError):
    kind = "transport"
