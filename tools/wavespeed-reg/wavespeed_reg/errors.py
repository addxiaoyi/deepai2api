"""Typed failures so the runner can decide "retry / rotate / give up".

刻意与 `allmodels_reg.errors` 保持同一套词汇：网关控制器、NDJSON 事件流、
号池的 `failureReasons` 统计都按 `kind` 归类，两个 worker 互换也不会错位。
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
    """Turnstile / hCaptcha 要 token，而手上没有（或已过期）。"""

    kind = "captcha_required"


class VerificationPending(SignupError):
    """注册了，但邮箱一直没收到确认信。"""

    kind = "verification_pending"


class NoApiKey(SignupError):
    """账号有了，key 拿不到（UI 没渲染出来 / 权限不足）。"""

    kind = "no_api_key"


class KeyNotObtainable(NoApiKey):
    """WaveSpeed 没有创建 key 的 HTTP 接口：协议腿永远拿不到明文。

    这个 kind 是"别在同一条腿上重试"的信号 —— 立刻切浏览器腿或换账号。
    """

    kind = "key_not_obtainable"


class OAuthUnavailable(SignupError):
    """OAuth 提供方（Google / GitHub）在这套环境里走不通。"""

    kind = "oauth_unavailable"


class Transport(SignupError):
    kind = "transport"
