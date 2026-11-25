"""Typed failures so the runner can decide "retry / rotate / give up".

与 `wavespeed_reg.errors` / `allmodels_reg.errors` 同一套词汇；额外多了
`VerifyRequired`（实名门槛）与 `KeyNotObtainable`（拿不到明文）。
"""

from __future__ import annotations


class SignupError(RuntimeError):
    """Base class; `kind` is what the runner switches on."""

    kind = "error"

    def __init__(self, message: str, *, detail: object = None) -> None:
        super().__init__(message)
        self.detail = detail


class EmailRejected(SignupError):
    """面板拒绝了这个地址（语法、一次性域名、被拉黑）。"""

    kind = "email_rejected"


class EmailTaken(SignupError):
    """地址已经注册过。"""

    kind = "email_taken"


class PasswordRejected(SignupError):
    kind = "password_rejected"


class RateLimited(SignupError):
    kind = "rate_limited"


class Blocked(SignupError):
    """Cloudflare / WAF / bot gate：协议腿过不去，必须换浏览器腿。"""

    kind = "blocked"


class CaptchaRequired(SignupError):
    """Turnstile 要 token，而手上没有（或已过期）。"""

    kind = "captcha_required"


class VerificationPending(SignupError):
    """注册了，但邮箱一直没收到激活信。"""

    kind = "verification_pending"


class NoApiKey(SignupError):
    """账号有了，key 拿不到。"""

    kind = "no_api_key"


class KeyNotObtainable(NoApiKey):
    """拿不到 key 明文：接口拒绝 / 达到数量上限 / 明文已被消费。

    "别在同一条腿上重试"的信号。
    """

    kind = "key_not_obtainable"


class VerifyRequired(SignupError):
    """需要实名 / 身份验证才能用（403 "Model requires identity verification"）。"""

    kind = "verify_required"


class QuotaExhausted(SignupError):
    """余额不足（402）或 voucher 被撤销。"""

    kind = "quota_exhausted"


class Transport(SignupError):
    kind = "transport"
