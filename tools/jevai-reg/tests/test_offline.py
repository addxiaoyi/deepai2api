"""离线测试：Better Auth 注册/验证、Key 端点探测与缓存、账号池落库。

用 `httpx.MockTransport` 模拟 jev-ai.org 的响应形状（不联网）。
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

import httpx
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from jevai_reg.errors import (  # noqa: E402
    EmailRejected,
    EmailTaken,
    NoApiKey,
    RateLimited,
    VerificationPending,
)
from jevai_reg.protocol import (  # noqa: E402
    KEY_PREFIX,
    Jev,
    KeyEndpoints,
    find_keys,
)
from jevai_reg.store import Store  # noqa: E402

KEY = "sk-glm5-abcdefghijklmnop123456"


class FakeJev:
    """Stateful stand-in: Better Auth + 一个建 Key 端点 + 账号信息。"""

    def __init__(self, *, key_path: str = "/api/user/api-keys", existing: list[str] | None = None):
        self.key_path = key_path
        self.users: dict[str, str] = {}
        self.keys: list[str] = list(existing or [])
        self.verified = False
        self.calls: list[tuple[str, str]] = []

    def handler(self, request: httpx.Request) -> httpx.Response:
        path = request.url.path
        self.calls.append((request.method, path))
        body = {}
        if request.content:
            try:
                body = json.loads(request.content)
            except Exception:  # noqa: BLE001
                body = {}

        if path == "/api/auth/sign-up/email":
            email = body.get("email", "")
            if email.endswith("@mailinator.com"):
                return httpx.Response(
                    400, json={"code": "EMAIL_DOMAIN_NOT_ALLOWED", "message": "Temporary or placeholder email addresses cannot be used."}
                )
            if email in self.users:
                return httpx.Response(400, json={"code": "USER_ALREADY_EXISTS", "message": "User already exists"})
            if len(body.get("password") or "") < 8:
                return httpx.Response(400, json={"code": "PASSWORD_TOO_SHORT", "message": "Password too short"})
            self.users[email] = body.get("password", "")
            return httpx.Response(200, json={"user": {"id": "u-1", "email": email, "emailVerified": False}})

        if path == "/api/auth/sign-in/email":
            if self.users.get(body.get("email")) != body.get("password"):
                return httpx.Response(401, json={"code": "INVALID_CREDENTIALS", "message": "Invalid"})
            return httpx.Response(200, json={"user": {"id": "u-1", "email": body.get("email")}})

        if path == "/api/auth/get-session":
            return httpx.Response(200, json={"session": {"id": "s-1"}, "user": {"id": "u-1", "emailVerified": self.verified}})

        if path == "/api/auth/send-verification-email":
            return httpx.Response(200, json={"status": True})

        if path == "/api/auth/verify-email":
            if request.url.params.get("token") == "good":
                self.verified = True
                return httpx.Response(302, headers={"location": "https://jev-ai.org/settings/apikeys"})
            return httpx.Response(302, headers={"location": "https://jev-ai.org/error?error=invalid_token"})

        if path == "/api/user/get-user-info":
            return httpx.Response(200, json={"data": {"credits": {"balance": 100}}})

        if path == "/api/user/get-user-credits":
            return httpx.Response(200, json={"data": {"credits": 100}})

        if path == "/api/user/checkin":
            return httpx.Response(200, json={"data": {"checked": True}})

        if path == self.key_path:
            if request.method == "GET":
                if not self.keys:
                    return httpx.Response(404, text="<!DOCTYPE html>not found")
                return httpx.Response(200, json={"data": [{"key": k} for k in self.keys]})
            self.keys.append(KEY)
            return httpx.Response(200, json={"data": {"key": KEY}})

        return httpx.Response(404, text="<!DOCTYPE html>not found")


def session_for(fake: FakeJev, endpoints: KeyEndpoints | None = None) -> Jev:
    return Jev(transport=httpx.MockTransport(fake.handler), timeout=5.0, key_endpoints=endpoints or KeyEndpoints())


# ------------------------------------------------------------------ helpers


def test_find_keys_extracts_and_dedupes():
    payload = {"a": KEY, "b": [f"prefix {KEY}"], "c": "sk-other-123"}
    assert find_keys(payload) == [KEY]
    assert find_keys(f'{{"key":"{KEY}"}}') == [KEY]


# --------------------------------------------------------------------- auth


def test_signup_success_and_session():
    fake = FakeJev()
    with session_for(fake) as client:
        payload = client.signup("ada@example.com", "Xk9#mQ2vLp7!aB", name="Ada")
        assert payload["user"]["email"] == "ada@example.com"
        assert client.session()["user"]["emailVerified"] is False


def test_login_and_session_verified_flag():
    fake = FakeJev()
    fake.users["ada@example.com"] = "Xk9#mQ2vLp7!aB"
    fake.verified = True
    with session_for(fake) as client:
        client.login("ada@example.com", "Xk9#mQ2vLp7!aB")
        assert client.verified() is True


def test_disposable_domain_is_typed_email_rejected():
    fake = FakeJev()
    with session_for(fake) as client:
        with pytest.raises(EmailRejected):
            client.signup("x@mailinator.com", "Xk9#mQ2vLp7!aB")


def test_existing_account_and_weak_password_are_typed():
    fake = FakeJev()
    with session_for(fake) as client:
        client.signup("dup@example.com", "Xk9#mQ2vLp7!aB")
        with pytest.raises(EmailTaken):
            client.signup("dup@example.com", "Xk9#mQ2vLp7!aB")
        with pytest.raises(Exception) as weak:
            client.signup("weak@example.com", "short")
        assert getattr(weak.value, "kind", "") == "password_rejected"


def test_verify_link_marks_verified_and_rejects_bad_token():
    fake = FakeJev()
    with session_for(fake) as client:
        client.signup("v@example.com", "Xk9#mQ2vLp7!aB")
        assert client.verify_link("https://jev-ai.org/api/auth/verify-email?token=good") is True
        assert fake.verified is True
        with pytest.raises(VerificationPending):
            client.verify_link("https://jev-ai.org/api/auth/verify-email?token=bad")


def test_rate_limit_maps_to_typed_error():
    class Limited(FakeJev):
        def handler(self, request):
            if request.url.path == "/api/auth/sign-up/email":
                return httpx.Response(429, json={"code": "RATE_LIMITED", "message": "too many"})
            return super().handler(request)

    with session_for(Limited()) as client:
        with pytest.raises(RateLimited):
            client.signup("x@example.com", "Xk9#mQ2vLp7!aB")


# --------------------------------------------------------------------- keys


def test_list_keys_from_candidate_endpoint():
    fake = FakeJev(existing=[KEY])
    with session_for(fake) as client:
        client.signup("k@example.com", "Xk9#mQ2vLp7!aB")
        rows = client.list_keys()
        assert rows and rows[0]["key"] == KEY


def test_create_key_discovers_endpoint_and_caches_it(tmp_path):
    fake = FakeJev(key_path="/api/apikeys")
    endpoints = KeyEndpoints(tmp_path / "key-endpoints.json")
    with session_for(fake, endpoints) as client:
        client.signup("c@example.com", "Xk9#mQ2vLp7!aB")
        assert client.list_keys() == [], "还没有 Key"
        created = client.create_key("dcg")
        assert created["key"] == KEY
        assert endpoints.get()["createPath"] == "/api/apikeys"
    assert (tmp_path / "key-endpoints.json").exists()


def test_ensure_key_prefers_existing_then_creates():
    fake = FakeJev(existing=[KEY])
    with session_for(fake) as client:
        client.signup("e@example.com", "Xk9#mQ2vLp7!aB")
        assert client.ensure_key() == KEY
        assert not any(method == "POST" and "keys" in path for method, path in fake.calls), "有 Key 就不该再建"

    empty = FakeJev()
    with session_for(empty) as client:
        client.signup("e2@example.com", "Xk9#mQ2vLp7!aB")
        assert client.ensure_key() == KEY


def test_ensure_key_raises_when_no_endpoint_answers():
    class NoKeys(FakeJev):
        def handler(self, request):
            if "key" in request.url.path:
                return httpx.Response(404, text="<!DOCTYPE html>not found")
            return super().handler(request)

    with session_for(NoKeys()) as client:
        client.signup("n@example.com", "Xk9#mQ2vLp7!aB")
        with pytest.raises(NoApiKey):
            client.ensure_key()


def test_run_flow_end_to_end(tmp_path):
    fake = FakeJev()
    with session_for(fake, KeyEndpoints(tmp_path / "ke.json")) as client:
        record = client.run_flow("flow@example.com", "Xk9#mQ2vLp7!aB", name="Flow", verify=False)
        assert record["email"] == "flow@example.com"
        assert record["apiKey"].startswith(KEY_PREFIX)
        assert record["credits"] == {"credits": 100}
        assert record["userId"]


# -------------------------------------------------------------------- store


def test_store_dedupes_and_exports_keys(tmp_path):
    store = Store(tmp_path / "jev.sqlite3")
    try:
        record = {
            "email": "a@example.com",
            "password": "pw",
            "name": "A",
            "apiKey": KEY,
            "userId": "u-1",
            "emailVerified": True,
            "credits": {"credits": 10},
        }
        assert store.add(record, engine="protocol", proxy="") is True
        assert store.add(record, engine="protocol", proxy="") is False
        assert store.keys() == [KEY]
        assert store.counts() == {"accounts": 1, "keys": 1, "verified": 1}

        target = tmp_path / "keys.txt"
        assert store.export_keys(target) == 1
        assert target.read_text(encoding="utf-8").strip() == KEY

        rows = store.pool_records()
        assert rows[0]["apiKey"] == KEY
        assert rows[0]["credits"] == {"credits": 10}
    finally:
        store.close()


def test_store_failure_summary(tmp_path):
    store = Store(tmp_path / "jev.sqlite3")
    try:
        store.record_failure(email="x@example.com", kind="no_api_key", reason="none")
        assert store.failure_summary()[0]["kind"] == "no_api_key"
    finally:
        store.close()

# ---------------------------------------------------------------- 签到（免费额度）

def test_credit_status_uses_post():
    """前端就是 POST /api/user/get-credit-status；用 GET 会 404。"""
    seen: list[str] = []

    class C(FakeJev):
        def handler(self, request):
            if request.url.path == "/api/user/get-credit-status":
                seen.append(request.method)
                if request.method != "POST":
                    return httpx.Response(404, text="<!DOCTYPE html>not found")
                return httpx.Response(200, json={"code": 0, "data": {"chatTier": "free", "credits": {"remainingCredits": 12}}})
            return super().handler(request)

    with session_for(C()) as client:
        client.signup("cs@example.com", "Xk9#mQ2vLp7!aB")
        status = client.credit_status()
        assert seen == ["POST"]
        assert status["chatTier"] == "free"
        assert status["credits"]["remainingCredits"] == 12


def test_credit_status_tolerates_nonzero_code():
    class C(FakeJev):
        def handler(self, request):
            if request.url.path == "/api/user/get-credit-status":
                return httpx.Response(200, json={"code": 401, "data": None})
            return super().handler(request)

    with session_for(C()) as client:
        client.signup("cs2@example.com", "Xk9#mQ2vLp7!aB")
        assert client.credit_status() == {}


def test_checkin_cli_updates_store_and_skips_same_day(tmp_path, capsys):
    from argparse import Namespace
    from jevai_reg import cli as jev_cli
    from jevai_reg.config import Config

    fake = FakeJev(existing=[KEY])
    fake.users["a@example.com"] = "Xk9#mQ2vLp7!aB"

    store = Store(tmp_path / "accounts.sqlite3")
    store.add(
        {"email": "a@example.com", "password": "Xk9#mQ2vLp7!aB", "apiKey": KEY, "emailVerified": True},
        engine="protocol",
        proxy="",
    )

    # 用一个打桩的 Jev 替换真网络
    import jevai_reg.cli as cli_mod

    def fake_jev(**kwargs):
        client = session_for(fake)
        original_login = client.login

        def login(email, password):
            return original_login(email, password)

        client.login = login
        return client

    cli_mod.Jev = fake_jev

    args = Namespace(
        config=None, count=0, concurrency=0, mail_provider=None, mail_domain=None, mail_domains=None,
        mail_address=None, mail_provider_=None, proxy=None, proxies=None, retry_rounds=None,
        output=str(tmp_path), push_url=None, push_token=None, key_name=None, no_verify=False,
        no_checkin=True, json=False, only_today=True, limit=0, pause=0,
    )
    code = jev_cli.cmd_checkin(args)
    assert code == 0
    rows = store.checkin_state()
    assert rows[0]["last_checkin"]
    assert rows[0]["checkin_count"] == 1
    store.close()

    # 第二次：当天已签，跳过（返回 1 = 没签到任何账号）
    store2 = Store(tmp_path / "accounts.sqlite3")
    cli_mod._store = lambda cfg: store2
    code2 = jev_cli.cmd_checkin(args)   # 内部会 close store2
    assert code2 == 1
    verify = Store(tmp_path / "accounts.sqlite3")
    assert verify.checkin_state()[0]["checkin_count"] == 1, "当天已签到的不会被重复计数"
    verify.close()


def test_credit_value_reads_every_shape():
    from jevai_reg.cli import _credit_value

    assert _credit_value({"credits": 5}) == 5
    assert _credit_value({"credits": {"remainingCredits": 7}}) == 7
    assert _credit_value({"remainingCredits": 9}) == 9
    assert _credit_value({"data": 1}) == 0
    assert _credit_value(None) == 0
