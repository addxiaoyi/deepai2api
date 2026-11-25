"""失败分类。

网关侧按 `kind` 做统计（job.failureReasons）与退避查表，所以取值必须稳定。
"""


class MoonAIError(Exception):
    """所有注册期失败的基类。`kind` 决定退避与是否重试。"""

    kind = "error"
    retryable = True

    def __init__(self, message: str, *, kind: str | None = None, status: int | None = None, detail: str = ""):
        super().__init__(message)
        if kind:
            self.kind = kind
        self.status = status
        self.detail = detail or message

    def __str__(self) -> str:  # pragma: no cover - trivial
        base = super().__str__()
        if self.status:
            return f"[{self.kind}/{self.status}] {base}"
        return f"[{self.kind}] {base}"


class EmailTaken(MoonAIError):
    kind = "email_taken"
    retryable = False


class EmailRejected(MoonAIError):
    kind = "email_rejected"
    retryable = False


class VerificationPending(MoonAIError):
    """注册成功了，但验证邮件迟迟没到（或被扔进垃圾箱）。"""

    kind = "verification_pending"


class RateLimited(MoonAIError):
    kind = "rate_limited"


class Blocked(MoonAIError):
    """Cloudflare / WAF / 区域封锁 / 直接 403。"""

    kind = "blocked"


class CaptchaRequired(MoonAIError):
    kind = "captcha_required"


class NoToken(MoonAIError):
    """OAuth 走完了但没换到 access_token。"""

    kind = "no_token"
    retryable = False


class NoApiKey(MoonAIError):
    """账号建好了，但 api_ Key 没签出来。"""

    kind = "no_api_key"


class TransportError(MoonAIError):
    kind = "transport"


class UpstreamError(MoonAIError):
    kind = "upstream"


class LoginRejected(MoonAIError):
    """注册成功但登录被拒（密码策略 / 账号被秒封）。"""

    kind = "login_rejected"
    retryable = False
