"""错误分类 —— 决定网关侧的退避与是否换 persona / 域名 / 引擎。"""

from __future__ import annotations


class ZoError(Exception):
    """所有可分类失败的基类。`kind` 会被写进 NDJSON 的 failed 事件。"""

    kind = "error"

    def __init__(self, message: str, *, kind: str | None = None, status: int = 0, detail: str = ""):
        super().__init__(message)
        if kind:
            self.kind = kind
        self.status = status
        self.detail = detail


class Blocked(ZoError):
    """403 / Cloudflare 拦截 / bot 判定。"""

    kind = "blocked"


class RateLimited(ZoError):
    kind = "rate_limited"


class EmailRejected(ZoError):
    """站点不接受这个邮箱（一次性域名 / 格式 / 被拉黑）。"""

    kind = "email_rejected"


class EmailTaken(ZoError):
    kind = "email_taken"


class CaptchaRequired(ZoError):
    """confirm 要 turnstile token，而我们没有。"""

    kind = "captcha_required"


class VerificationPending(ZoError):
    """邮件还没到（魔法链）。"""

    kind = "verification_pending"


class HandleTaken(ZoError):
    """工作区 handle 已被占用。"""

    kind = "handle_taken"


class NoToken(ZoError):
    """拿到了会话但没有 access token。"""

    kind = "no_token"


class NoApiKey(ZoError):
    kind = "no_api_key"


class TransportError(ZoError):
    kind = "transport"


class UpstreamError(ZoError):
    kind = "upstream"


class BrowserUnavailable(ZoError):
    """没装 Playwright / Camoufox / Patchright，浏览器腿跑不了。"""

    kind = "browser_unavailable"