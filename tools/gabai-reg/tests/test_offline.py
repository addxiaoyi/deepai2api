"""Offline protocol tests for the gab.ai worker: no network, no accounts.

A synthetic gab.ai is served through `httpx.MockTransport`, mirroring the shapes
documented at the top of `gabai_reg/protocol.py`:

    POST /json/auth/signup/email   -> {error:false, user, tokens:{access,refresh,expiresAt}}
    POST /json/auth/login/email
    GET  /json/api-keys            -> {apiKeys:[...]}
    POST /json/api-keys            -> {apiKey:{key:"gab_…"}}
    POST /v1/chat/completions      -> 403 {error:{code:"plus_required"}} for free accounts
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

import httpx
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from gabai_reg.errors import (  # noqa: E402
    Blocked,
    CaptchaRequired,
    EmailRejected,
    EmailTaken,
    NoApiKey,
    PlusRequired,
    SignupError,
)
from gabai_reg.protocol import GabSession, extract_verify_link  # noqa: E402
from gabai_reg.store import Store  # noqa: E402

TOKENS = {"access": "access-token-abc", "refresh": "refresh-token-def", "expiresAt": "2026-12-01T00:00:00Z"}


class FakeGab:
    """Stateful stand-in for gab.ai's JSON API."""

    def __init__(self, *, plus: bool = False, keys: list[dict] | None = None):
        self.plus = plus
        self.users: dict[str, str] = {}
        self.keys: list[dict] = list(keys or [])
        self.calls: list[tuple[str, str]] = []
        self.issued = 0

    def handler(self, request: httpx.Request) -> httpx.Response:
        path = request.url.path
        self.calls.append((request.method, path))
        auth = request.headers.get("authorization", "")

        if path == "/json/auth/signup/email":
            body = json.loads(request.content or b"{}")
            email = body.get("email", "")
            if "blocked" in email:
                return httpx.Response(200, text="<html>Just a moment...</html>", headers={"server": "cloudflare"})
            if "taken" in email:
                return httpx.Response(400, json={"error": True, "message": "An account already exists for this email"})
            if "captcha" in email:
                return httpx.Response(400, json={"error": True, "message": "captcha verification failed"})
            if "bad" in email:
                return httpx.Response(400, json={"error": True, "message": "invalid email address"})
            if not body.get("turnstileToken"):
                return httpx.Response(400, json={"error": True, "message": "turnstile required"})
            self.users[email] = body.get("password", "")
            return httpx.Response(
                200,
                json={"error": False, "user": {"email": email, "verified": False}, "tokens": TOKENS},
            )

        if path == "/json/auth/login/email":
            body = json.loads(request.content or b"{}")
            email = body.get("email", "")
            if self.users.get(email) != body.get("password"):
                return httpx.Response(401, json={"error": True, "message": "invalid credentials"})
            return httpx.Response(200, json={"error": False, "user": {"email": email}, "tokens": TOKENS})

        if path == "/json/api-keys":
            if "Bearer access-token-abc" not in auth:
                return httpx.Response(401, json={"error": True, "message": "unauthorized"})
            if request.method == "GET":
                return httpx.Response(200, json={"error": False, "apiKeys": self.keys})
            self.issued += 1
            key = {"id": f"key-{self.issued}", "name": f"k{self.issued}", "key": f"gab_live_offline{self.issued:04d}"}
            self.keys.append(key)
            return httpx.Response(200, json={"error": False, "apiKey": key})

        if path == "/v1/chat/completions":
            if not self.plus:
                return httpx.Response(403, json={"error": {"code": "plus_required", "message": "Plus required"}})
            return httpx.Response(200, json={"choices": [{"message": {"role": "assistant", "content": "pong"}}]})

        return httpx.Response(404, json={"error": True, "message": "not found"})


def session_for(fake: FakeGab, **kwargs) -> GabSession:
    return GabSession(transport=httpx.MockTransport(fake.handler), timeout=5.0, **kwargs)


# --------------------------------------------------------------------- signup


def test_signup_captures_tokens():
    fake = FakeGab()
    with session_for(fake) as session:
        payload = session.signup(email="ada@example.com", password="Xk9#mQ2vLp7!aB", turnstile="ts-token")
        assert payload["error"] is False
        assert session.access_token == "access-token-abc"
        assert session.refresh_token == "refresh-token-def"
        assert session.bearer_headers()["Authorization"] == "Bearer access-token-abc"


def test_signup_requires_a_turnstile_token():
    fake = FakeGab()
    with session_for(fake) as session:
        with pytest.raises(CaptchaRequired):
            session.signup(email="ada@example.com", password="Xk9#mQ2vLp7!aB")


def test_signup_maps_typed_failures():
    fake = FakeGab()
    with session_for(fake) as session:
        with pytest.raises(EmailTaken):
            session.signup(email="taken@example.com", password="Xk9#mQ2vLp7!aB", turnstile="t")
        with pytest.raises(CaptchaRequired):
            session.signup(email="captcha@example.com", password="Xk9#mQ2vLp7!aB", turnstile="t")
        with pytest.raises(EmailRejected):
            session.signup(email="bad@example.com", password="Xk9#mQ2vLp7!aB", turnstile="t")
        with pytest.raises(Blocked):
            session.signup(email="blocked@example.com", password="Xk9#mQ2vLp7!aB", turnstile="t")


def test_login_reuses_the_same_token_shape():
    fake = FakeGab()
    with session_for(fake) as session:
        session.signup(email="ada@example.com", password="pw-12345678", turnstile="t")
        fresh = session_for(fake)
        with fresh as again:
            again.login(email="ada@example.com", password="pw-12345678")
            assert again.access_token == "access-token-abc"
        with pytest.raises(SignupError):
            with session_for(fake) as wrong:
                wrong.login(email="ada@example.com", password="nope")


# ------------------------------------------------------------------- api keys


def test_api_key_create_and_list():
    fake = FakeGab()
    with session_for(fake) as session:
        session.signup(email="ada@example.com", password="pw-12345678", turnstile="t")
        created = session.api_key_create("dcg-2026-11-26")
        assert created["apiKey"]["key"].startswith("gab_live_offline")
        listed = session.api_keys_list()
        assert [row["id"] for row in listed] == ["key-1"]


def test_api_key_create_without_a_gab_key_is_no_api_key():
    class BadKey(FakeGab):
        def handler(self, request: httpx.Request) -> httpx.Response:
            if request.url.path == "/json/api-keys" and request.method == "POST":
                return httpx.Response(200, json={"error": False, "apiKey": {"id": "x", "key": "not-a-key"}})
            return super().handler(request)

    fake = BadKey()
    with session_for(fake) as session:
        session.signup(email="ada@example.com", password="pw-12345678", turnstile="t")
        with pytest.raises(NoApiKey):
            session.api_key_create()


def test_plus_gate_is_typed():
    fake = FakeGab(plus=False)
    with session_for(fake) as session:
        session.signup(email="ada@example.com", password="pw-12345678", turnstile="t")
        key = session.api_key_create()["apiKey"]["key"]
        with pytest.raises(PlusRequired):
            session.developer_api_check(key)

    paid = FakeGab(plus=True)
    with session_for(paid) as session:
        session.signup(email="paid@example.com", password="pw-12345678", turnstile="t")
        key = session.api_key_create()["apiKey"]["key"]
        session.developer_api_check(key)  # must not raise


# --------------------------------------------------------------- mail + store


def test_verify_link_extraction():
    class Msg:
        def __init__(self, body="", html=""):
            self.body = body
            self.html = html

    messages = [
        Msg(body="welcome"),
        Msg(html='<a href="https://gab.ai/verify-email?token=abc123.DEF-ghi">verify</a>'),
    ]
    assert extract_verify_link(messages) == "https://gab.ai/verify-email?token=abc123.DEF-ghi"
    assert extract_verify_link([Msg(body="nothing here")]) == ""


def test_store_dedupes_by_email(tmp_path):
    store = Store(tmp_path / "gabai.sqlite3")
    try:
        record = {"email": "ada@example.com", "password": "pw", "api_key": "gab_live_0001", "api_key_id": "key-1"}
        assert store.add(record, engine="protocol", proxy="") is True
        assert store.add(record, engine="protocol", proxy="") is False
        rows = store.pool_records()
        assert len(rows) == 1
        assert rows[0]["apiKey"].startswith("gab_live_")
    finally:
        store.close()
