"""Offline tests: no network, no browser — the protocol layer only.

Run from tools/deepai-reg:  python -m unittest discover -s tests -v
"""

from __future__ import annotations

import json
import sys
import unittest
from pathlib import Path

import httpx

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from deepai_reg.errors import Blocked, EmailRejected, EmailTaken  # noqa: E402
from deepai_reg.protocol import DeepAI, _first_error  # noqa: E402

PROFILE = {
    "pk": 16280856,
    "username": "marcus.whitaker42",
    "email": "marcus.whitaker42@example.com",
    "userprofile": {"api_key": "5970e32a-ca88-474c-b517-bf5ad299cfda"},
    "userprofile2": {"plan": "free", "user_has_deepai_pro": False},
}


class FakeDeepAI(DeepAI):
    """DeepAI with a scripted transport; records what we sent."""

    def __init__(self, handler, **kwargs):
        super().__init__(**kwargs)
        self.client.close()
        self.client = httpx.Client(
            transport=httpx.MockTransport(handler), trust_env=False, follow_redirects=True
        )
        self.sent: list[httpx.Request] = []


def happy_handler(request: httpx.Request) -> httpx.Response:
    path = request.url.path
    if request.url.host == "deepai.org" and path == "/":
        return httpx.Response(200, text="<html>home</html>", headers={"set-cookie": "csrftoken=abc123; Path=/"})
    if path == "/get_user_login_type":
        return httpx.Response(200, json={"user_exists": False, "login_with_password": True})
    if path == "/save_custom_signup_data":
        return httpx.Response(200, json={"ok": True})
    if path == "/daily-time-sync/registration/":
        return httpx.Response(201, json={"key": "regkey"})
    if path == "/daily-time-sync/user/":
        return httpx.Response(200, json=PROFILE)
    return httpx.Response(404, json={"detail": "not found"})


class TestParsing(unittest.TestCase):
    def test_first_error_flattens_drf_bodies(self):
        self.assertEqual(_first_error({"email": ["Please use your own email address."]}), "Please use your own email address.")
        self.assertEqual(_first_error(["boom"]), "boom")
        self.assertEqual(_first_error({"x": "y"}), "y")
        self.assertEqual(_first_error({}), "")

    def test_api_key_extraction(self):
        self.assertEqual(DeepAI.api_key_from(PROFILE), "5970e32a-ca88-474c-b517-bf5ad299cfda")
        self.assertEqual(DeepAI.api_key_from({"api_key": "abcdef0123456789"}), "abcdef0123456789")
        self.assertEqual(DeepAI.api_key_from({}), "")


class TestSignupFlow(unittest.TestCase):
    def test_happy_path_returns_account(self):
        client = FakeDeepAI(happy_handler)
        account = client.signup("Marcus.Whitaker42@example.com", "S3cret!pass99")
        self.assertEqual(account["api_key"], "5970e32a-ca88-474c-b517-bf5ad299cfda")
        self.assertEqual(account["username"], "marcus.whitaker42")
        self.assertEqual(account["plan"], "free")
        self.assertFalse(account["is_pro"])
        self.assertEqual(account["api_key_source"], "profile")
        self.assertEqual(account["registration_key"], "regkey")

    def test_registration_posts_the_dialog_fields(self):
        seen: dict[str, str] = {}

        def handler(request: httpx.Request) -> httpx.Response:
            if request.url.path == "/daily-time-sync/registration/":
                seen.update(
                    {k: v for k, v in (p.split("=", 1) for p in request.content.decode().split("&"))}
                )
            return happy_handler(request)

        client = FakeDeepAI(handler)
        client.signup("a.b1@example.com", "pw!A1234567", username="a.b1")
        self.assertEqual(seen["email"], "a.b1%40example.com")
        self.assertEqual(seen["username"], "a.b1")
        self.assertEqual(seen["password1"], seen["password2"])

    def test_disposable_domain_is_reported(self):
        def handler(request: httpx.Request) -> httpx.Response:
            if request.url.path == "/daily-time-sync/registration/":
                return httpx.Response(400, json={"email": ["Please use your own email address."]})
            return happy_handler(request)

        client = FakeDeepAI(handler)
        with self.assertRaises(EmailRejected):
            client.signup("x.y1@euploading.com", "pw!A1234567")

    def test_existing_account_short_circuits(self):
        def handler(request: httpx.Request) -> httpx.Response:
            if request.url.path == "/get_user_login_type":
                return httpx.Response(200, json={"user_exists": True})
            return happy_handler(request)

        client = FakeDeepAI(handler)
        with self.assertRaises(EmailTaken):
            client.signup("a.b1@example.com", "pw!A1234567")

    def test_cloudflare_page_escalates(self):
        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(403, text="<html>Just a moment...</html>")

        client = FakeDeepAI(handler)
        with self.assertRaises(Blocked):
            client.signup("a.b1@example.com", "pw!A1234567")

    def test_profile_without_key_falls_back_to_desktop_auth(self):
        profile = json.loads(json.dumps(PROFILE))
        profile["userprofile"] = {}

        def handler(request: httpx.Request) -> httpx.Response:
            if request.url.path == "/daily-time-sync/user/":
                return httpx.Response(200, json=profile)
            if request.url.path == "/deepai_code/desktop_auth/":
                return httpx.Response(
                    302,
                    headers={"location": "http://127.0.0.1:47000/auth-callback?code=abc123&state=s"},
                )
            if request.url.path == "/deepai_code/desktop_auth/exchange/v2/":
                return httpx.Response(404, json={"detail": "gone"})
            if request.url.path == "/deepai_code/desktop_auth/exchange/":
                return httpx.Response(200, json={"api_key": "desktop-key-0001", "username": "marcus"})
            return happy_handler(request)

        client = FakeDeepAI(handler)
        client.client.follow_redirects = False
        account = client.signup("a.b1@example.com", "pw!A1234567")
        self.assertEqual(account["api_key"], "desktop-key-0001")
        self.assertEqual(account["api_key_source"], "desktop_auth")


if __name__ == "__main__":
    unittest.main()
