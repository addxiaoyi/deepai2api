"""Offline protocol tests: no network, no accounts, no captcha credits.

A synthetic dashboard is served through `httpx.MockTransport`, mirroring the
shapes observed on dash.allmodels.io: Better Auth sign-up, session, verify-email,
`/api/me/overview`, `/api/me/signup-key` and `/api/keys`.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

import httpx
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from allmodels_reg.config import CaptchaConfig, Config, MailConfig  # noqa: E402
from allmodels_reg.errors import CaptchaRequired, EmailRejected, EmailTaken, NoApiKey  # noqa: E402
from allmodels_reg.mailbox import FixedAddress, Message  # noqa: E402
from allmodels_reg.protocol import AllModels  # noqa: E402
from allmodels_reg.store import Store  # noqa: E402

BASE = "https://dash.test"
SIGNUP_KEY = "sk_live_signup_0123456789abcdef"


class FakeDashboard:
    """Stateful stand-in for the real dashboard API."""

    def __init__(self, *, signup_key=SIGNUP_KEY, verifies=True):
        self.signup_key = signup_key
        self.verifies = verifies
        self.accounts: dict[str, dict] = {}
        self.sessions: dict[str, str] = {}
        self.keys: dict[str, list[dict]] = {}
        self.calls: list[tuple[str, str]] = []

    def handler(self, request: httpx.Request) -> httpx.Response:
        path = request.url.path
        self.calls.append((request.method, path))
        cookie = request.headers.get("cookie", "")
        token = ""
        for part in cookie.split(";"):
            name, _, value = part.strip().partition("=")
            if name == "better-auth.session_token":
                token = value
        email = self.sessions.get(token, "")

        if path == "/signup":
            return httpx.Response(200, text="<html><title>Allmodels Dashboard</title></html>")

        if path == "/api/auth/ok":
            return httpx.Response(200, json={"ok": True})

        if path == "/api/auth/sign-up/email":
            payload = json.loads(request.content or b"{}")
            if not request.headers.get("x-turnstile-token"):
                return httpx.Response(403, json={"code": "captcha_failed", "message": "Verification failed."})
            if payload["email"].split("@")[-1] in ("gmail.com", "googlemail.com"):
                return httpx.Response(422, json={"code": "invalid_email", "message": "Use a work address"})
            if payload["email"] in self.accounts:
                return httpx.Response(422, json={"code": "user_already_exists", "message": "User already exists"})
            self.accounts[payload["email"]] = {
                "password": payload["password"],
                "verified": not self.verifies,
                "keys": [self.signup_key] if self.signup_key else [],
            }
            session = f"sess-{len(self.sessions) + 1}"
            self.sessions[session] = payload["email"]
            return httpx.Response(
                200,
                json={"user": {"id": "u1", "email": payload["email"], "name": payload["name"]}},
                headers={"set-cookie": f"better-auth.session_token={session}; Path=/; HttpOnly"},
            )

        if path == "/api/auth/get-session":
            if not email:
                return httpx.Response(200, json=None)
            state = self.accounts[email]
            return httpx.Response(200, json={"user": {"id": "u1", "email": email, "emailVerified": state["verified"]}})

        if path == "/api/auth/verify-email":
            if not email:
                return httpx.Response(401, json={"error": "unauthenticated"})
            self.accounts[email]["verified"] = True
            return httpx.Response(302, headers={"location": f"{BASE}/verify-email?verified=1"})

        if path == "/api/me/overview":
            if not email:
                return httpx.Response(401, json={"error": "unauthenticated"})
            return httpx.Response(
                200,
                json={
                    "user": {"id": "u1", "email": email, "name": "Test"},
                    "organization": {"id": "org1", "name": "org", "slug": "org", "status": "active"},
                    "projects": [{"id": "proj1", "name": "Default", "slug": "default", "status": "active", "isDefault": True}],
                    "guardrails": [
                        {"id": "gr1", "displayName": "Default", "budgetMode": "unlimited", "enabled": True, "isDefault": True}
                    ],
                    "apiKeys": [],
                },
            )

        if path == "/api/me/signup-key":
            if not email:
                return httpx.Response(401, json={"error": "unauthenticated"})
            key = self.signup_key
            return httpx.Response(200, json={"key": key})

        if path == "/api/keys" and request.method == "GET":
            if not email:
                return httpx.Response(401, json={"error": "unauthenticated"})
            rows = [
                {
                    "id": f"key{i}",
                    "keyPrefix": value[:8],
                    "last4": value[-4:],
                    "displayName": "k",
                    "status": "active",
                    "guardrailId": "gr1",
                    "projectId": "proj1",
                    "createdAt": 1,
                    "lastUsedAt": None,
                }
                for i, value in enumerate(self.accounts[email]["keys"])
            ]
            return httpx.Response(200, json={"keys": rows})

        if path == "/api/keys" and request.method == "POST":
            if not email:
                return httpx.Response(401, json={"error": "unauthenticated"})
            payload = json.loads(request.content or b"{}")
            assert payload["projectId"] == "proj1"
            assert payload["guardrailId"] == "gr1"
            value = f"sk_live_created_{len(self.accounts[email]['keys']):02d}"
            self.accounts[email]["keys"].append(value)
            return httpx.Response(200, json={"key": value})

        return httpx.Response(404, json={"error": "not_found"})


def build_client(dashboard: FakeDashboard, *, verify=True, provider=None, box=None, keys=1, base=BASE):
    transport = httpx.MockTransport(dashboard.handler)
    client = AllModels(base_url=base, transport=transport)
    return client


def test_signup_returns_auto_provisioned_key():
    dashboard = FakeDashboard()
    provider = FixedAddress(
        address="ops@example.com",
        messages=[Message(body=f'Confirm: {BASE}/api/auth/verify-email?token=abc123')],
    )
    box = provider.allocate()
    with build_client(dashboard) as client:
        account = client.signup_flow(
            box.address,
            "Sup3r-secret!",
            name="Test User",
            turnstile="turnstile-token",
            verify=True,
            provider=provider,
            box=box,
            keys=1,
        )
    assert account["api_key"] == SIGNUP_KEY
    assert account["api_key_source"] == "signup-key"
    assert account["email_verified"] is True
    assert account["project_id"] == "proj1"
    assert account["guardrail_id"] == "gr1"
    assert account["organization_id"] == "org1"
    assert account["key_prefix"] == SIGNUP_KEY[:8]
    assert any(cookie["name"] == "better-auth.session_token" for cookie in account["cookies"])
    assert ("POST", "/api/auth/sign-up/email") in dashboard.calls
    assert ("GET", "/api/auth/verify-email") in dashboard.calls


def test_missing_turnstile_token_is_classified():
    dashboard = FakeDashboard()
    with build_client(dashboard) as client:
        client.warm()
        with pytest.raises(CaptchaRequired):
            client.signup("user@example.com", "Sup3r-secret!")


def test_gmail_is_refused_before_the_request():
    dashboard = FakeDashboard()
    with build_client(dashboard) as client:
        with pytest.raises(EmailRejected):
            client.signup("user@gmail.com", "Sup3r-secret!", turnstile="t")
    assert ("POST", "/api/auth/sign-up/email") not in dashboard.calls


def test_duplicate_address_is_email_taken():
    dashboard = FakeDashboard()
    dashboard.accounts["dup@example.com"] = {"password": "x", "verified": True, "keys": []}
    with build_client(dashboard) as client:
        with pytest.raises(EmailTaken):
            client.signup("dup@example.com", "Sup3r-secret!", turnstile="t")


def test_extra_keys_are_minted_when_asked():
    dashboard = FakeDashboard(signup_key=None)  # nothing auto-provisioned
    provider = FixedAddress(
        address="ops@example.com",
        messages=[Message(body=f'Confirm: {BASE}/api/auth/verify-email?token=abc123')],
    )
    box = provider.allocate()
    with build_client(dashboard) as client:
        account = client.signup_flow(
            box.address,
            "Sup3r-secret!",
            turnstile="t",
            verify=True,
            provider=provider,
            box=box,
            keys=3,
        )
    assert account["api_key_source"] == "created"
    assert account["api_key"] == "sk_live_created_00"
    assert [row["key"] for row in account["extra_keys"]] == ["sk_live_created_01", "sk_live_created_02"]


def test_no_key_anywhere_raises():
    dashboard = FakeDashboard(signup_key=None)

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/api/keys" and request.method == "POST":
            return httpx.Response(500, json={"error": "key_provision_failed"})
        if request.url.path == "/api/me/signup-key":
            return httpx.Response(200, json={"key": None})
        return dashboard.handler(request)

    with AllModels(base_url=BASE, transport=httpx.MockTransport(handler)) as client:
        with pytest.raises(NoApiKey):
            client.signup_flow("nokey@example.com", "Sup3r-secret!", turnstile="t", verify=False)


def test_store_exports_pool_shape(tmp_path):
    store = Store(tmp_path / "accounts.sqlite3")
    account = {
        "email": "pool@example.com",
        "password": "pw",
        "name": "Pool",
        "api_key": SIGNUP_KEY,
        "key_id": "key0",
        "key_prefix": SIGNUP_KEY[:8],
        "key_last4": SIGNUP_KEY[-4:],
        "api_key_source": "signup-key",
        "extra_keys": [{"key": "sk_live_extra", "source": "created"}],
        "project_id": "proj1",
        "guardrail_id": "gr1",
        "organization_id": "org1",
        "email_verified": True,
        "cookies": [{"name": "better-auth.session_token", "value": "sess-1", "domain": "dash.test"}],
    }
    assert store.add(account, engine="protocol", proxy="") is True
    assert store.add(account, engine="protocol", proxy="") is False  # duplicate email

    rows = store.pool_records()
    assert rows[0]["apiKey"] == SIGNUP_KEY
    assert rows[0]["projectId"] == "proj1"
    assert rows[0]["emailVerified"] is True
    assert rows[0]["extraKeys"] == [{"key": "sk_live_extra", "source": "created"}]

    jsonl = tmp_path / "accounts.jsonl"
    assert store.export_jsonl(jsonl) == 1
    record = json.loads(jsonl.read_text(encoding="utf-8").strip())
    assert record["apiKey"] == SIGNUP_KEY

    csv_path = tmp_path / "accounts.csv"
    assert store.export_csv(csv_path) == 1
    assert csv_path.read_text(encoding="utf-8").splitlines()[0].startswith("email,password,api_key")
    store.close()


def test_config_defaults_and_overrides(tmp_path):
    cfg = Config.load(None, {"count": 3, "concurrency": 2, "mail_provider": "mailtm"})
    assert cfg.count == 3
    assert cfg.mail.provider == "mailtm"
    assert cfg.keys_per_account == 1
    assert cfg.captcha.provider in ("none", "capsolver", "2captcha", "browser", "manual")
    assert cfg.key_name("someone@example.com").startswith("someone")
    assert isinstance(cfg.mail, MailConfig) and isinstance(cfg.captcha, CaptchaConfig)
