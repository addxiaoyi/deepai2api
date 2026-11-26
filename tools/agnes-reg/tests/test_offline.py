"""离线测试：Agnes 站点事实（端点来自真实注册脚本）+ 验证码流程。

用 `httpx.MockTransport` 复刻 `platform-backend.agnes-ai.com` 的四步响应，不联网。
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

import httpx

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parent))
sys.path.insert(0, str(HERE.parents[2] / "tools" / "linekit"))

from agnes_reg.site import SITE  # noqa: E402
from linekit.errors import LineError, VerificationPending  # noqa: E402
from linekit.session import CodeSignupSession, find_keys  # noqa: E402

KEY = "sk-agnes-abcdefghijklmnop123456"


class FakeBackend:
    """复刻真实响应形状：{code:200, message, data:{...}}。"""

    def __init__(self, *, code: str = "123456", domain_limited: bool = False):
        self.code = code
        self.domain_limited = domain_limited
        self.tokens: list[str] = []
        self.last_query: dict[str, str] = {}
        self.calls: list[tuple[str, str]] = []

    def handler(self, request: httpx.Request) -> httpx.Response:
        path = request.url.path
        self.calls.append((request.method, path))

        if path == "/api/verification":
            # 真实站点要求 ticket/randstr（腾讯云验证码）；这里把 query 记下来供断言
            self.last_query = dict(request.url.params)
            if self.domain_limited:
                return httpx.Response(
                    400,
                    json={"code": 400, "message": "too many registration from this domain, please retry later"},
                )
            assert request.url.params.get("purpose") == "register"
            return httpx.Response(200, json={"code": 200, "message": "ok"})

        if path == "/api/user/register":
            body = json.loads(request.content)
            if body.get("code") != self.code:
                return httpx.Response(200, json={"code": 400, "message": "invalid verification code"})
            token = f"acc-{len(self.tokens) + 1}"
            self.tokens.append(token)
            return httpx.Response(
                200,
                json={
                    "code": 200,
                    "data": {
                        "access_token": token,
                        "user": {"display_id": "u-42", "username": "ada", "email": body["email"]},
                    },
                },
            )

        if path == "/api/token":
            auth = request.headers.get("authorization", "")
            if not auth.startswith("Bearer acc-"):
                return httpx.Response(200, json={"code": 401, "message": "unauthorized"})
            body = json.loads(request.content or b"{}")
            assert body.get("api_key_profile") == "default", "真实脚本用 default profile"
            return httpx.Response(
                200,
                json={"code": 200, "data": {"key": KEY, "name": body.get("name", "x"), "key_profile": "default"}},
            )

        return httpx.Response(404, text="not found")


class FakeProvider:
    """假的邮箱 provider：只实现 run_flow 用到的两个方法。"""

    name = "fake"

    def __init__(self, code: str = "123456"):
        self.code = code
        self.requested = 0

    def wait_for_code(self, box, *, timeout: float = 180.0, digits: int = 6, poll: float = 5.0) -> str:
        self.requested += 1
        return self.code


def session(fake: FakeBackend) -> CodeSignupSession:
    return CodeSignupSession(SITE, transport=httpx.MockTransport(fake.handler), timeout=5.0)


# --------------------------------------------------------------- 站点事实


def test_site_describes_the_real_endpoints():
    assert SITE.flow == "code-signup"
    assert SITE.base_url == "https://platform-backend.agnes-ai.com"
    assert SITE.url("/api/verification") == "https://platform-backend.agnes-ai.com/api/verification"
    assert SITE.verify_code_path == "/api/verification"
    assert SITE.register_path == "/api/user/register"
    assert SITE.token_path == "/api/token"
    assert SITE.token_json_path == "data.access_token"
    assert SITE.key_json_path == "data.key"
    assert SITE.api_base == "https://api.agnes-ai.cn/v1"
    assert SITE.key_prefix == "sk-"


def test_models_and_prefix():
    assert SITE.models
    blob = json.dumps({"key": KEY})
    assert find_keys(blob, SITE.key_prefix) == [KEY]


# ----------------------------------------------------------------- 四步流程


def test_request_code_hits_verification_endpoint():
    fake = FakeBackend()
    with session(fake) as client:
        assert client.request_code("ada@example.com") is True
    assert ("GET", "/api/verification") in fake.calls


def test_signup_with_code_stores_access_token():
    fake = FakeBackend()
    with session(fake) as client:
        payload = client.signup_with_code("ada@example.com", "Xk9#mQ2vLp7!aB", "123456")
        assert client.access_token.startswith("acc-")
        assert payload["data"]["user"]["display_id"] == "u-42"
        assert client.user["username"] == "ada"


def test_wrong_code_is_typed_failure():
    fake = FakeBackend(code="999999")
    with session(fake) as client:
        client.request_code("ada@example.com")
        try:
            client.signup_with_code("ada@example.com", "Xk9#mQ2vLp7!aB", "123456")
        except LineError as exc:
            assert "invalid verification code" in str(exc) or exc.kind in ("error",)
        else:  # pragma: no cover
            raise AssertionError("错误验证码必须失败")


def test_create_key_uses_bearer_and_returns_sk():
    fake = FakeBackend()
    with session(fake) as client:
        client.request_code("ada@example.com")
        client.signup_with_code("ada@example.com", "Xk9#mQ2vLp7!aB", "123456")
        created = client.create_key("dcg-x")
        assert created["key"] == KEY
        assert created["path"] == "/api/token"

    with session(fake) as client:
        client.access_token = ""
        try:
            client.create_key("dcg")
        except LineError:
            pass
        else:  # pragma: no cover
            raise AssertionError("没有 access_token 不能建 Key")


def test_run_flow_end_to_end():
    fake = FakeBackend()
    provider = FakeProvider()
    box = object()
    with session(fake) as client:
        record = client.run_flow(
            "flow@example.com",
            "Xk9#mQ2vLp7!aB",
            name="Flow",
            provider=provider,
            box=box,
            captcha={"ticket": "t-123", "randstr": "r-456"},
        )
    assert record["email"] == "flow@example.com"
    assert record["apiKey"] == KEY
    assert record["keyPrefix"] == "sk-"
    assert record["userId"] == "u-42"
    assert record["emailVerified"] is True
    assert provider.requested == 1


def test_run_flow_without_code_raises_verification_pending():
    fake = FakeBackend()
    provider = FakeProvider(code="")
    with session(fake) as client:
        try:
            client.run_flow(
                "x@example.com",
                "Xk9#mQ2vLp7!aB",
                provider=provider,
                box=object(),
                captcha={"ticket": "t-1", "randstr": "r-1"},
            )
        except VerificationPending:
            pass
        else:  # pragma: no cover
            raise AssertionError("没收到验证码应当停在 verification_pending")


def test_domain_limit_is_surfaced():
    fake = FakeBackend(domain_limited=True)
    with session(fake) as client:
        try:
            client.request_code("ada@example.com")
        except LineError as exc:
            assert exc.kind in ("email_rejected", "error")
        else:  # pragma: no cover
            raise AssertionError("域名限流必须抛错")


def test_captcha_ticket_is_sent_with_verification():
    fake = FakeBackend()
    with session(fake) as client:
        client.request_code("ada@example.com", captcha={"ticket": "T-9", "randstr": "R-9"})
    assert fake.last_query.get("ticket") == "T-9"
    assert fake.last_query.get("randstr") == "R-9"
    assert fake.last_query.get("purpose") == "register"


def test_missing_captcha_is_typed():
    from linekit.errors import CaptchaRequired

    fake = FakeBackend()
    provider = FakeProvider()
    with session(fake) as client:
        try:
            client.run_flow("x@example.com", "Xk9#mQ2vLp7!aB", provider=provider, box=object())
        except CaptchaRequired:
            pass
        else:  # pragma: no cover
            raise AssertionError("没有 ticket/randstr 必须报 captcha_required")