"""失败分类。

网关侧按 `kind` 决定退避与是否换号（见 `src/provider-register.mjs` 的
`DEFAULT_ANTI_BAN.backoff`），所以 kind 是协议的一部分，不要随手改名。
"""

from __future__ import annotations


class BlackboxError(Exception):
    """所有可归类失败的基类。"""

    kind = "error"

    def __init__(self, message: str, *, kind: str | None = None, status: int | None = None, detail=None):
        super().__init__(message)
        if kind:
            self.kind = kind
        self.status = status
        self.detail = detail or {}


class TransportError(BlackboxError):
    kind = "transport"


class UpstreamError(BlackboxError):
    kind = "upstream"


class RateLimited(BlackboxError):
    kind = "rate_limited"


class Blocked(BlackboxError):
    """边缘/网关拒绝（403、Cloudflare 挑战、地区封锁）。"""

    kind = "blocked"


class CaptchaRequired(BlackboxError):
    kind = "captcha_required"


class EmailRejected(BlackboxError):
    kind = "email_rejected"


class EmailTaken(BlackboxError):
    kind = "email_taken"


class VerificationPending(BlackboxError):
    kind = "verification_pending"


class NoApiKey(BlackboxError):
    """账号登进去了，但面板里拿不到 Key（没资格 / 没开通 / 建 Key 接口变了）。"""

    kind = "no_api_key"


class KeyNotObtainable(NoApiKey):
    kind = "key_not_obtainable"


class SessionExpired(BlackboxError):
    """Google / app 侧会话失效，需要重新授权。"""

    kind = "session_expired"


class OAuthDenied(BlackboxError):
    """Google 拒绝授权（密码错、二次验证、风控）。"""

    kind = "oauth_denied"


class PlanRequired(BlackboxError):
    """Agent API / 某些模型需要 Pro 档。"""

    kind = "plan_required"


# kind -> 中文说明，写进号池的 lastError 与日志，排障时不用翻代码
KIND_LABELS = {
    "transport": "网络/代理",
    "upstream": "上游异常",
    "rate_limited": "被限流",
    "blocked": "被边缘拦截",
    "captcha_required": "需要过人机校验",
    "email_rejected": "邮箱被拒",
    "email_taken": "邮箱已注册",
    "verification_pending": "等验证邮件超时",
    "no_api_key": "拿不到 Key",
    "key_not_obtainable": "建 Key 接口不可用",
    "session_expired": "会话过期",
    "oauth_denied": "Google 拒绝授权",
    "plan_required": "需要 Pro 档",
    "error": "未知错误",
}
