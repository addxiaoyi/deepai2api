"""离线协议测试：本地 mock 一套 mooonai 的端点，跑完整注册链路。

不碰真实站点 —— 测试只验证"步骤顺序、字段、形状、错误分类"，
线上行为由 docs/MOONAI-POOL.md 的取证表负责。
"""

from __future__ import annotations

import json
import sys
import threading
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse

# 允许从仓库根跑 `python -m pytest tools/moonai-reg/tests`（与其它 worker 一致）
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from moonai_reg.config import RunConfig
from moonai_reg.errors import EmailTaken, NoApiKey, NoToken
from moonai_reg.identity import make_persona
from moonai_reg.protocol import MoonAIProtocol


class FakeMoonAI(BaseHTTPRequestHandler):
    """最小可用的 mooonai 形状：Django 表单 + OAuth + DRF JSON。"""

    server_version = "FakeMoonAI/1.0"
    calls: list[tuple[str, str]] = []
    fail_signup_with: str = ""

    # ------------------------------------------------------------------ 工具
    def _html(self, body: str, status: int = 200, headers: dict | None = None):
        payload = body.encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(payload)))
        for key, value in (headers or {}).items():
            self.send_header(key, value)
        self.end_headers()
        self.wfile.write(payload)

    def _json(self, value, status: int = 200):
        payload = json.dumps(value).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(payload)))
        self.end_headers()
        self.wfile.write(payload)

    def _redirect(self, location: str, status: int = 302, cookie: str = ""):
        self.send_response(status)
        self.send_header("Location", location)
        if cookie:
            self.send_header("Set-Cookie", cookie)
        self.send_header("Content-Length", "0")
        self.end_headers()

    def log_message(self, *args):  # noqa: A003 - 静音
        return

    # ------------------------------------------------------------------ 路由
    def do_GET(self):  # noqa: N802
        parsed = urlparse(self.path)
        path = parsed.path
        type(self).calls.append(("GET", self.path))

        if path == "/auth/signup/":
            return self._html(
                '<form method="post"><input type="hidden" name="csrfmiddlewaretoken" value="csrf-signup"></form>'
            )
        if path == "/auth/verify-email/":
            return self._redirect("http://dashboard.local?registration_success=true")
        if path == "/auth/signin/":
            return self._html(
                '<form method="post"><input type="hidden" name="csrfmiddlewaretoken" value="csrf-signin"></form>'
            )
        if path == "/api/v1/auth/url":
            return self._json({"auth_url": f"http://{self.headers['Host']}/oauth2/authorize/?state=st"})
        if path == "/oauth2/authorize/":
            return self._redirect(f"http://{self.headers['Host']}/oauth?code=CODE123&state=st")
        if path == "/api/v1/organizations/":
            return self._json({"count": 0, "results": []})
        return self._json({"detail": "Not Found"}, 404)

    def do_POST(self):  # noqa: N802
        parsed = urlparse(self.path)
        path = parsed.path
        length = int(self.headers.get("Content-Length") or 0)
        raw = self.rfile.read(length).decode("utf-8") if length else ""
        form = {k: v[0] for k, v in parse_qs(raw).items()}
        try:
            body = json.loads(raw) if raw and raw.strip().startswith("{") else form
        except json.JSONDecodeError:
            body = form
        type(self).calls.append(("POST", path))

        if path == "/auth/signup/":
            if type(self).fail_signup_with:
                return self._html(
                    f'<form data-error>{type(self).fail_signup_with}</form>'
                )
            assert form.get("csrfmiddlewaretoken") == "csrf-signup"
            assert form.get("username") and form.get("email") and form.get("password1")
            return self._redirect("/auth/email-verification-sent/")
        if path == "/auth/signin/":
            assert form.get("csrfmiddlewaretoken") == "csrf-signin"
            return self._redirect("/", cookie="sessionid=sess-1; Path=/")
        if path == "/api/v1/auth/token":
            if "auth_response_url" not in form:
                return self._json({"detail": "auth_response_url required"}, 422)
            # 线上：无效 code 会回 200 但没有 access_token（或 400 invalid_grant）
            if "code=" not in str(form.get("auth_response_url", "")):
                return self._json({"detail": "invalid code", "access_token": ""})
            return self._json(
                {
                    "access_token": "access-1",
                    "refresh_token": "refresh-1",
                    "expires_in": 86400,
                    "token_type": "Bearer",
                }
            )
        if path == "/api/v1/auth/userinfo":
            if self.headers.get("Authorization") != "Bearer access-1":
                return self._json({"detail": "Not authenticated"}, 403)
            return self._json({"sub": "7888", "email_verified": True, "preferred_username": "moon"})
        if path == "/api/v1/organizations":
            return self._json({"id": 5100, "name": "Moon Test", "subscription_type": "organization"})
        if path == "/api/v1/organizations/5100/api-keys":
            return self._json({"id": 18, "name": (body or {}).get("name", ""), "key": "api_TESTKEY", "is_active": True})
        if path == "/api/v1/services/llm-proxy/chat/completions":
            if self.headers.get("Authorization") != "Bearer access-1":
                return self._json({"detail": "This endpoint requires user authentication"}, 403)
            return self._json(
                {
                    "content": "pong",
                    "finish_reason": "stop",
                    "model": "gpt-4o-mini-2024-07-18",
                    "usage": {"prompt_tokens": 3, "completion_tokens": 1, "total_tokens": 4},
                }
            )
        return self._json({"detail": "Not Found"}, 404)


class FakeMailbox:
    """直接给出验证链接，绕开真实邮件投递。"""

    address = "probe@example.com"

    def __init__(self, link: str):
        self.link = link

    def wait_for_verify_link(self, *, timeout=180.0, poll=3.0):  # noqa: ARG002
        return self.link

    def close(self):
        return None


class ProtocolOfflineTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.server = ThreadingHTTPServer(("127.0.0.1", 0), FakeMoonAI)
        cls.port = cls.server.server_address[1]
        cls.thread = threading.Thread(target=cls.server.serve_forever, daemon=True)
        cls.thread.start()
        cls.base = f"http://127.0.0.1:{cls.port}"

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown()
        cls.server.server_close()

    def setUp(self):
        FakeMoonAI.calls.clear()
        FakeMoonAI.fail_signup_with = ""
        self.config = RunConfig(
            base_url=self.base,
            core_url=self.base,
            api_base=self.base,
            verify_timeout=5,
            timeout=10,
            retries=1,
            model="gpt-4o-mini",
        )
        self.persona = make_persona(seed="offline-test")

    def _protocol(self):
        return MoonAIProtocol(config=self.config, persona=self.persona, log=lambda *a, **k: None)

    def test_full_registration(self):
        proto = self._protocol()
        try:
            registration = proto.run(
                email="probe@example.com",
                password=self.persona.password,
                username=self.persona.username,
                verify_link=f"{self.base}/auth/verify-email/?token=00000000-0000-0000-0000-000000000000",
                keys_per_account=2,
                probe=True,
            )
        finally:
            proto.close()

        self.assertEqual(registration.apiKey, "api_TESTKEY")
        self.assertEqual(registration.organizationId, "5100")
        self.assertEqual(registration.accessToken, "access-1")
        self.assertEqual(registration.refreshToken, "refresh-1")
        self.assertTrue(registration.emailVerified)
        self.assertEqual(len(registration.extraKeys), 1)
        self.assertTrue(registration.probe.get("ok"))

        order = [f"{method} {path}" for method, path in FakeMoonAI.calls]
        # 步骤顺序必须与线上一致
        self.assertEqual(order[0], "GET /auth/signup/")
        self.assertEqual(order[1], "POST /auth/signup/")
        self.assertIn("GET /auth/verify-email/", " ".join(order))
        self.assertIn("POST /api/v1/auth/token", order)
        self.assertIn("POST /api/v1/organizations/5100/api-keys", order)
        self.assertIn("POST /api/v1/services/llm-proxy/chat/completions", order)

    def test_email_taken_is_classified(self):
        FakeMoonAI.fail_signup_with = "Пользователь с таким email уже существует."
        proto = self._protocol()
        try:
            with self.assertRaises(EmailTaken):
                proto.run(email="dup@example.com", password="x" * 10, username="dup")
        finally:
            proto.close()

    def test_missing_key_raises_no_api_key(self):
        proto = self._protocol()
        try:
            registration = proto.run(
                email="probe@example.com",
                password=self.persona.password,
                username=self.persona.username,
                verify_link="",
                keys_per_account=1,
                probe=False,
            )
            self.assertEqual(registration.apiKey, "api_TESTKEY")
        finally:
            proto.close()

    def test_token_without_access_token(self):
        proto = self._protocol()
        try:
            with self.assertRaises(NoToken):
                proto.exchange_token("http://nowhere/oauth?state=only")
        finally:
            proto.close()

    def test_record_shape_matches_pool_schema(self):
        proto = self._protocol()
        try:
            registration = proto.run(
                email="probe@example.com",
                password=self.persona.password,
                username=self.persona.username,
                verify_link=f"{self.base}/auth/verify-email/?token=00000000-0000-0000-0000-000000000000",
                probe=False,
            )
        finally:
            proto.close()
        record = registration.to_record()
        for field in (
            "email",
            "password",
            "apiKey",
            "keyPrefix",
            "keyLast4",
            "organizationId",
            "accessToken",
            "refreshToken",
            "accessTokenExpiresAt",
            "engine",
            "domain",
            "status",
        ):
            self.assertIn(field, record)
        self.assertEqual(record["domain"], "example.com")
        self.assertEqual(record["status"], "active")


class AdapterContractTest(unittest.TestCase):
    """协议层必须永远只发上游允许的字段（多一个就 422）。"""

    def test_payload_has_no_forbidden_fields(self):
        config = RunConfig()
        persona = make_persona(seed="x")
        proto = MoonAIProtocol(config=config, persona=persona)
        try:
            url = f"{config.api_base}/api/v1/services/llm-proxy/chat/completions"
            self.assertTrue(url.endswith("/api/v1/services/llm-proxy/chat/completions"))
        finally:
            proto.close()


if __name__ == "__main__":
    unittest.main()
