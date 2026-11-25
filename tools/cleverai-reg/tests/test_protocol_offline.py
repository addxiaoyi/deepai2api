"""Offline protocol tests: no network, no accounts, no captcha credits.

A synthetic pair of backends is served through `httpx.MockTransport`, mirroring
what was observed on the live site:

  * Firebase Identity Toolkit: `accounts:signUp`, `accounts:sendOobCode`,
    `accounts:update`, `accounts:lookup`, `securetoken`
  * appserver: `POST /users` (multipart), `GET /users/{uid}`, `GET /configs?type=engines`,
    `POST /omni/response` (SSE)

The mock enforces the two rules that actually bite in production: the
`x-functions-key` header and `X-Firebase-AppCheck`.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

import httpx
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from cleverai_reg.appcheck import AppCheckVault, _ttl_seconds  # noqa: E402
from cleverai_reg.config import Config, MailConfig  # noqa: E402
from cleverai_reg.errors import (  # noqa: E402
    AppCheckRequired,
    EmailTaken,
    NoCredit,
    RateLimited,
    SignupError,
)
from cleverai_reg.mailbox import FixedAddress, Message  # noqa: E402
from cleverai_reg.protocol import CleverAI, dialog_message, text_of  # noqa: E402
from cleverai_reg.store import Store  # noqa: E402

APP_CHECK = "appcheck-token-good"
# 离线用例的假密钥：worker 从环境变量读真值，测试通过 env 注入假值即可。
FUNCTIONS_KEY = "test-functions-key"
import os as _os  # noqa: E402

_os.environ.setdefault("CLEVERAI_FUNCTIONS_KEY", FUNCTIONS_KEY)


class FakeBackend:
    """Stateful stand-in for identitytoolkit + appserver."""

    def __init__(self, *, app_check=APP_CHECK, credits=75):
        self.app_check = app_check
        self.credits = credits
        self.users: dict[str, dict] = {}
        self.by_email: dict[str, str] = {}
        self.app_check_calls = 0
        self.calls: list[tuple[str, str]] = []
        self.omni_events: list[dict] = [
            {"sequenceNumber": 0, "delta": {"role": "assistant", "content": [{"type": "text", "text": "po"}]}},
            {"sequenceNumber": 1, "delta": {"content": [{"type": "text", "text": "ng"}]}, "finishReason": "stop"},
        ]

    # ------------------------------------------------------------------ rules
    def _appserver_guard(self, request: httpx.Request) -> httpx.Response | None:
        if request.headers.get("x-functions-key") != FUNCTIONS_KEY:
            return httpx.Response(401, json={"message": "missing function key"})
        token = request.headers.get("x-firebase-appcheck", "")
        if not token or token != self.app_check:
            self.app_check_calls += 1
            return httpx.Response(401, json={"code": "APP_CHECK_REQUIRED", "message": "App Check token required"})
        return None

    # ---------------------------------------------------------------- handler
    def handler(self, request: httpx.Request) -> httpx.Response:
        path = request.url.path
        self.calls.append((request.method, path))
        host = request.url.host

        if host == "identitytoolkit.googleapis.com":
            return self._identity(request, path)
        if host == "securetoken.googleapis.com":
            return httpx.Response(200, json={"id_token": "id-refreshed", "refresh_token": "refresh-refreshed"})
        return self._appserver(request, path)

    def _identity(self, request: httpx.Request, path: str) -> httpx.Response:
        payload = json.loads(request.content or b"{}")
        if path.endswith("accounts:signUp"):
            email = payload.get("email", "")
            if email in self.by_email:
                return httpx.Response(400, json={"error": {"message": "EMAIL_EXISTS"}})
            if "bad" in email:
                return httpx.Response(400, json={"error": {"message": "INVALID_EMAIL"}})
            if len(payload.get("password") or "") < 8:
                return httpx.Response(400, json={"error": {"message": "WEAK_PASSWORD"}})
            uid = f"uid-{len(self.users) + 1}"
            self.by_email[email] = uid
            self.users[uid] = {"email": email, "verified": False, "profile": None, "settings": None}
            return httpx.Response(
                200,
                json={
                    "kind": "identitytoolkit#SignupNewUserResponse",
                    "idToken": f"id-{uid}",
                    "refreshToken": f"refresh-{uid}",
                    "localId": uid,
                    "email": email,
                },
            )
        if path.endswith("accounts:sendOobCode"):
            return httpx.Response(200, json={"email": "sent"})
        if path.endswith("accounts:update"):
            code = payload.get("oobCode", "")
            uid = code.removeprefix("CODE-")
            if uid not in self.users:
                return httpx.Response(400, json={"error": {"message": "INVALID_OOB_CODE"}})
            self.users[uid]["verified"] = True
            return httpx.Response(200, json={"email": self.users[uid]["email"]})
        if path.endswith("accounts:lookup"):
            uid = str(payload.get("idToken", "")).removeprefix("id-")
            user = self.users.get(uid) or {}
            return httpx.Response(200, json={"users": [{"localId": uid, "emailVerified": user.get("verified", False)}]})
        if path.endswith("accounts:signInWithPassword"):
            email = payload.get("email", "")
            uid = self.by_email.get(email)
            if not uid:
                return httpx.Response(400, json={"error": {"message": "EMAIL_NOT_FOUND"}})
            return httpx.Response(
                200, json={"idToken": f"id-{uid}", "refreshToken": f"refresh-{uid}", "localId": uid}
            )
        return httpx.Response(404, json={"error": {"message": "not found"}})

    def _appserver(self, request: httpx.Request, path: str) -> httpx.Response:
        guard = self._appserver_guard(request)
        if guard is not None:
            return guard
        if path.endswith("/users") and request.method == "POST":
            body = request.content.decode("utf-8", "replace")
            raw = body.split('name="json"', 1)[-1]
            raw = raw.split("\r\n\r\n", 1)[-1].rsplit("\r\n--", 1)[0]
            payload = json.loads(raw)
            uid = payload["uid"]
            user = self.users.setdefault(uid, {"email": "", "verified": False})
            user["profile"] = {
                "uid": uid,
                "username": f"user{len(self.users)}",
                "wallet": {"totalCredit": self.credits, "renewableCredit": 30},
                "vouchers": [{"voucherCode": "WELCOME"}],
            }
            return httpx.Response(201, json=user["profile"])
        if "/users/" in path and request.method == "GET":
            uid = path.rsplit("/", 1)[-1]
            user = self.users.get(uid)
            if not user or not user.get("profile"):
                return httpx.Response(404, json={"message": "User does not exist"})
            return httpx.Response(200, json=user["profile"])
        if path.endswith("/configs"):
            return httpx.Response(
                200,
                json={
                    "data": [
                        {"id": "openai-gpt-6-luna", "type": "CHAT", "cost": 2},
                        {"id": "antropic-calude-sonnet-5-5", "type": "CHAT", "cost": 10},
                    ]
                },
            )
        if path.endswith("/user-settings") and request.method == "POST":
            return httpx.Response(200, json={"uid": "uid", "language": "en"})
        if path.endswith("/omni/response"):
            payload = json.loads(request.content or b"{}")
            if (payload.get("wallet") or {}).get("totalCredit", 1) <= 0:
                return httpx.Response(402, json={"message": "no credit left"})
            blocks = "".join(f"data: {json.dumps(event)}\n\n" for event in self.omni_events)
            return httpx.Response(200, text=blocks, headers={"content-type": "text/event-stream"})
        return httpx.Response(404, json={"message": "not found"})


def client_for(backend: FakeBackend, **kwargs) -> CleverAI:
    transport = httpx.MockTransport(backend.handler)
    return CleverAI(transport=transport, app_check=APP_CHECK, timeout=5.0, **kwargs)


# --------------------------------------------------------------------- tests


def test_signup_then_profile_builds_a_pool_record():
    backend = FakeBackend()
    client = client_for(backend)
    try:
        body = client.signup("ada@example.com", "Xk9#mQ2vLp7!aB")
        assert body["localId"] == "uid-1"
        profile = client.create_profile()
        assert profile["wallet"]["totalCredit"] == 75
        assert client.uid == "uid-1"
        assert client.refresh_token == "refresh-uid-1"
    finally:
        client.close()


def test_email_taken_and_weak_password_map_to_typed_errors():
    backend = FakeBackend()
    client = client_for(backend)
    try:
        client.signup("dup@example.com", "Xk9#mQ2vLp7!aB")
        with pytest.raises(EmailTaken):
            client.signup("dup@example.com", "Xk9#mQ2vLp7!aB")
        with pytest.raises(SignupError) as weak:
            client.signup("weak@example.com", "short")
        assert weak.value.kind == "password_rejected"
    finally:
        client.close()


def test_missing_app_check_is_refused_and_refreshed():
    backend = FakeBackend()
    refresh_hits = {"count": 0}

    def getter() -> str:
        refresh_hits["count"] += 1
        return APP_CHECK

    client = CleverAI(
        transport=httpx.MockTransport(backend.handler),
        app_check="stale-token",
        appcheck_getter=getter,
        timeout=5.0,
    )
    try:
        client.signup("apc@example.com", "Xk9#mQ2vLp7!aB")
        with pytest.raises(AppCheckRequired):
            client.create_profile()
        assert refresh_hits["count"] >= 1, "a rejected token must force one fresh mint"
        profile = client.create_profile()
        assert profile["wallet"]["totalCredit"] == 75
    finally:
        client.close()


def test_verification_link_marks_the_account_verified():
    backend = FakeBackend()
    client = client_for(backend)
    try:
        client.signup("ver@example.com", "Xk9#mQ2vLp7!aB")
        client.create_profile()
        client.send_verification()
        assert client.apply_verification_link("https://example.firebaseapp.com/__/auth/action?oobCode=CODE-uid-1&mode=verifyEmail")
        assert client.account_info()["emailVerified"] is True
    finally:
        client.close()


def test_engines_catalog_is_parsed():
    backend = FakeBackend()
    client = client_for(backend)
    try:
        client.signup("eng@example.com", "Xk9#mQ2vLp7!aB")
        rows = client.engines()
        assert [row["id"] for row in rows] == ["openai-gpt-6-luna", "antropic-calude-sonnet-5-5"]
        assert rows[0]["cost"] == 2
    finally:
        client.close()


def test_chat_streams_text_and_reports_finish():
    backend = FakeBackend()
    client = client_for(backend)
    try:
        client.signup("chat@example.com", "Xk9#mQ2vLp7!aB")
        profile = client.create_profile()
        events = list(
            client.chat(
                [dialog_message("hi")],
                engine="openai-gpt-6-luna",
                wallet=profile["wallet"],
                cost=2,
            )
        )
        text = "".join(text_of(event) for event in events)
        assert text == "pong"
        assert events[-1]["finishReason"] == "stop"
    finally:
        client.close()


def test_empty_wallet_maps_to_no_credit():
    backend = FakeBackend()
    client = client_for(backend)
    try:
        client.signup("broke@example.com", "Xk9#mQ2vLp7!aB")
        client.create_profile()
        with pytest.raises(NoCredit):
            list(client.chat([dialog_message("hi")], engine="openai-gpt-6-luna", wallet={"totalCredit": 0}, cost=2))
    finally:
        client.close()


def test_unexpected_401_without_appcheck_code_is_not_swallowed():
    backend = FakeBackend(app_check="another-token")
    client = client_for(backend)
    try:
        client.signup("x@example.com", "Xk9#mQ2vLp7!aB")
        with pytest.raises(AppCheckRequired):
            client.create_profile()
    finally:
        client.close()


# ------------------------------------------------------------------ mailbox


def test_fixed_mailbox_allocates_plus_tags_and_finds_the_link():
    provider = FixedAddress(address="bot@example.com")
    box = provider.allocate()
    assert box.address.startswith("bot+") and box.address.endswith("@example.com")
    provider.feed(
        [
            Message(
                subject="Verify your email",
                body="https://genius.firebaseapp.com/__/auth/action?mode=verifyEmail&oobCode=abc123",
            )
        ]
    )
    link = provider.wait_for_link(box, timeout=1.0, poll=0.05)
    assert "oobCode=abc123" in link


def test_imap_provider_requires_host_and_domain():
    from cleverai_reg.mailbox import ImapCatchAll

    with pytest.raises(SignupError):
        ImapCatchAll(host="", user="u", password="p", domain="example.com")
    provider = ImapCatchAll(host="imap.example.com", user="u", password="p", domain="example.com")
    box = provider.allocate("persona")
    assert box.address == "persona@example.com"


# ---------------------------------------------------------------- app check


def test_app_check_vault_caches_and_reports_ttl():
    vault = AppCheckVault(provider="manual")
    vault.seed("token-abc", 600)
    assert vault.get() == "token-abc"
    stats = vault.stats
    assert stats["has_token"] is True
    assert stats["expires_in"] > 0


def test_app_check_vault_manual_without_seed_is_typed():
    vault = AppCheckVault(provider="manual")
    with pytest.raises(Exception) as exc:
        vault.get()
    assert getattr(exc.value, "kind", "") == "captcha_required"


@pytest.mark.parametrize(
    ("raw", "expected"),
    [("3600s", 3600), ("", 3300), (None, 3300), ("nonsense", 3300)],
)
def test_ttl_parsing(raw, expected):
    assert _ttl_seconds(raw) == expected


# -------------------------------------------------------------------- store


def test_store_dedupes_by_email_and_exports_pool_records(tmp_path):
    store = Store(tmp_path / "accounts.sqlite3")
    try:
        record = {
            "email": "pool@example.com",
            "password": "pw",
            "uid": "uid-1",
            "refresh_token": "refresh-1",
            "app_check": "ac",
            "wallet": {"totalCredit": 75},
        }
        assert store.add(record, engine="protocol", proxy="") is True
        assert store.add(record, engine="protocol", proxy="") is False
        rows = store.pool_records()
        assert len(rows) == 1
        assert rows[0]["refreshToken"] == "refresh-1"
        assert rows[0]["wallet"]["totalCredit"] == 75

        store.domain_ok("example.com")
        store.domain_failed("example.com", cooldown=60)
        score = store.domain_score("example.com")
        assert score["ok"] == 1 and score["failed"] == 1

        out = tmp_path / "out.jsonl"
        assert store.export_jsonl(out) == 1
        assert json.loads(out.read_text(encoding="utf-8").strip())["email"] == "pool@example.com"
    finally:
        store.close()


# ------------------------------------------------------------------- config


def test_config_defaults_prefer_domain_mailboxes():
    cfg = Config.load(None, {})
    assert cfg.mail.provider == "imap"
    assert cfg.appcheck.provider == "browser"
    assert cfg.antiban.per_proxy_concurrency == 1
    described = cfg.describe()
    assert described["appcheck"] == "browser"
    assert described["antiban"]["per_domain_burst"] == 5


def test_config_overrides_from_flags():
    cfg = Config.load(None, {"mail_provider": "fixed", "mail_address": "bot@example.com", "appcheck_provider": "capsolver"})
    assert cfg.mail.provider == "fixed"
    assert cfg.appcheck.provider == "capsolver"
    assert MailConfig(address="bot@example.com").address == "bot@example.com"


def test_rate_limit_error_is_typed():
    from cleverai_reg.protocol import _auth_error

    assert isinstance(_auth_error(429, '{"error":{"message":"TOO_MANY_ATTEMPTS_TRY_LATER"}}'), RateLimited)
