"""Typed failures for the proxyscrape.com worker.

Same vocabulary as the other workers so the gateway controller, the NDJSON stream
and the retry policy all speak one language:

    email_rejected · email_taken · password_rejected · rate_limited · blocked
    turnstile_required · verification_pending · no_trial · no_credentials
    transport · error
"""

from __future__ import annotations


class ProxyScrapeError(RuntimeError):
    kind = "error"

    def __init__(self, message: str, *, detail: object = None) -> None:
        super().__init__(message)
        self.detail = detail


class EmailRejected(ProxyScrapeError):
    kind = "email_rejected"


class EmailTaken(ProxyScrapeError):
    kind = "email_taken"


class PasswordRejected(ProxyScrapeError):
    kind = "password_rejected"


class RateLimited(ProxyScrapeError):
    kind = "rate_limited"


class Blocked(ProxyScrapeError):
    """Cloudflare 挑战 / WAF / 出口被拉黑。"""

    kind = "blocked"


class TurnstileRequired(ProxyScrapeError):
    kind = "turnstile_required"


class VerificationPending(ProxyScrapeError):
    kind = "verification_pending"


class NoTrial(ProxyScrapeError):
    """账号建好了，但试用资格没有（同一 IP/设备已领过、或号太新）。"""

    kind = "no_trial"


class NoCredentials(ProxyScrapeError):
    """试用领到了，但没读到代理凭据。"""

    kind = "no_credentials"


class Transport(ProxyScrapeError):
    kind = "transport"
