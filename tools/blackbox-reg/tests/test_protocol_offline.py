"""离线用例：不打真实站点，用 httpx.MockTransport 把 Auth.js 的响应摆出来。

覆盖的是"协议理解是否正确"，不是"站点今天是否在线"：
    * Auth.js 的 csrf → signin/google → 302 到 Google 的链路
    * 回调落地 session → /api/auth/session 能读出 user
    * 面板 Key 的候选路径探测（命中即缓存，第二次不再探测）
    * 建 Key 兜底（列表为空 / 只回 plaintext）
    * 推理自检 403 → PlanRequired
    * TOTP 与 CLI 沙箱配置的形状
"""

from __future__ import annotations

import json
import os
import sys
import tempfile
import unittest
from urllib.parse import urlparse

import httpx

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from blackbox_reg import net as netmod  # noqa: E402
from blackbox_reg import protocol as P  # noqa: E402
from blackbox_reg.clienv import settings_payload  # noqa: E402
from blackbox_reg.config import RunConfig  # noqa: E402
from blackbox_reg.errors import KeyNotObtainable, PlanRequired, SessionExpired  # noqa: E402
from blackbox_reg.identity import GoogleIdentity, make_persona, totp_now  # noqa: E402

BASE = "https://app.blackbox.ai"
API = "https://enterprise.blackbox.ai"


class FakeSite:
    """一个可编程的"站点"：按 (method, path) 返回预设响应。"""

    def __init__(self):
        self.routes: dict[tuple[str, str], httpx.Response] = {}
        self.calls: list[tuple[str, str]] = []
        self.session_cookie = "__Secure-authjs.session-token=abc123; Path=/; Secure; HttpOnly"

    def add(self, method: str, url: str, status: int = 200, *, body=None, headers=None, text: str | None = None):
        parsed = urlparse(url)
        key = (method.upper(), parsed.path)
        if body is not None:
            content = json.dumps(body).encode()
            hdrs = {"content-type": "application/json"}
        else:
            content = (text or "").encode()
            hdrs = {"content-type": "text/html"}
        hdrs.update(headers or {})
        self.routes[key] = httpx.Response(status, content=content, headers=hdrs)
        return self

    def handler(self, request: httpx.Request) -> httpx.Response:
        path = request.url.path
        self.calls.append((request.method, path))
        route = self.routes.get((request.method, path))
        if route is None:
            # 未注册的路径按"这个接口不存在"处理，正好模拟面板接口探测
            return httpx.Response(404, json={"error": "not found"})
        response = httpx.Response(
            route.status_code,
            content=route.content,
            headers=dict(route.headers),
            request=request,
        )
        return response


def patch_transport(fake: FakeSite):
    """把 net.make_client 换成走 MockTransport 的客户端（cookie 行为不变）。"""
    original = netmod.make_client

    def factory(*, persona, proxy="", timeout=45.0):
        return httpx.Client(
            transport=httpx.MockTransport(fake.handler),
            headers=persona.headers(),
            timeout=timeout,
            follow_redirects=False,
        )

    netmod.make_client = factory
    return original


def make_config(tmpdir: str, **overrides) -> RunConfig:
    config = RunConfig()
    config.base_url = BASE
    config.api_base = API
    config.out_dir = tmpdir
    config.probe = False
    for key, value in overrides.items():
        setattr(config, key, value)
    return config


class ProtocolCase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="bbx-test-")
        self.fake = FakeSite()
        self._original = patch_transport(self.fake)
        self.config = make_config(self.tmp)
        self.persona = make_persona(seed="unit")

    def tearDown(self):
        netmod.make_client = self._original
        for root, _dirs, files in os.walk(self.tmp, topdown=False):
            for name in files:
                os.unlink(os.path.join(root, name))
            for name in _dirs:
                os.rmdir(os.path.join(root, name))
        os.rmdir(self.tmp)

    # ------------------------------------------------------------- OAuth
    def test_signin_google_redirect(self):
        self.fake.add("GET", f"{BASE}/login", 200, text="<html>login</html>")
        self.fake.add("GET", f"{BASE}/api/auth/csrf", 200, body={"csrfToken": "csrf-1"})
        self.fake.add(
            "POST",
            f"{BASE}/api/auth/signin/google",
            302,
            headers={
                "location": (
                    "https://accounts.google.com/o/oauth2/v2/auth?response_type=code"
                    "&client_id=985058387028-iaa60lr5e586kvhjp085qcojv3jv2p7h.apps.googleusercontent.com"
                    "&redirect_uri=https%3A%2F%2Fapp.blackbox.ai%2Fapi%2Fauth%2Fcallback%2Fgoogle"
                    "&state=xyz&code_challenge=abc&code_challenge_method=S256&scope=openid+profile+email"
                )
            },
        )
        with P.BlackboxProtocol(config=self.config, persona=self.persona) as proto:
            proto.warm()
            url = proto.begin_google_oauth()
        self.assertIn("accounts.google.com", url)
        self.assertIn("client_id=985058387028", url)
        self.assertIn("code_challenge_method=S256", url)
        # csrf 拿过、signin 发过
        self.assertIn(("GET", "/api/auth/csrf"), self.fake.calls)
        self.assertIn(("POST", "/api/auth/signin/google"), self.fake.calls)

    def test_callback_sets_session_and_reads_user(self):
        self.fake.add(
            "GET",
            f"{BASE}/api/auth/callback/google",
            302,
            headers={"location": f"{BASE}/", "set-cookie": self.fake.session_cookie},
        )
        self.fake.add("GET", f"{BASE}/api/auth/session", 200, body={"user": {"id": "u1", "email": "a@own.test", "plan": "pro"}})
        with P.BlackboxProtocol(config=self.config, persona=self.persona) as proto:
            info = proto.exchange_callback(f"{BASE}/api/auth/callback/google?code=c1&state=s1")
        self.assertEqual(info["user"]["email"], "a@own.test")
        with P.BlackboxProtocol(config=self.config, persona=self.persona) as proto:
            self.assertEqual(proto.plan(), "pro")

    def test_google_consent_page_raises(self):
        self.fake.add("GET", f"{BASE}/api/auth/csrf", 200, body={"csrfToken": "c"})
        self.fake.add(
            "POST",
            f"{BASE}/api/auth/signin/google",
            302,
            headers={"location": "https://accounts.google.com/o/oauth2/v2/auth?client_id=x"},
        )
        self.fake.add("GET", "https://accounts.google.com/o/oauth2/v2/auth", 200, text="<html>consent approve</html>")
        with P.BlackboxProtocol(config=self.config, persona=self.persona) as proto:
            url = proto.begin_google_oauth()
            with self.assertRaises(Exception) as ctx:
                proto.follow_google(url)
        self.assertIn("consent", str(ctx.exception).lower() + " consent")

    # ------------------------------------------------------------- Key
    def test_key_discovery_picks_first_working_path_and_caches(self):
        self.fake.add("GET", f"{BASE}/api/auth/session", 200, body={"user": {"email": "a@own.test"}})
        # 前两个候选 404，第三个命中
        self.fake.add(
            "GET",
            f"{BASE}/api/user/api-keys",
            200,
            body={"keys": [{"id": "k1", "key": "sk-abcdefghijklmnop1234", "keyPrefix": "sk-abcde", "last4": "1234"}]},
        )
        with P.BlackboxProtocol(config=self.config, persona=self.persona) as proto:
            rows = proto.list_keys()
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["key"], "sk-abcdefghijklmnop1234")
        with open(os.path.join(self.tmp, "key-endpoints.json"), encoding="utf-8") as handle:
            cache = json.load(handle)
        self.assertEqual(cache["list"], "/api/user/api-keys")

        # 第二次：直接走缓存，不再打 404 的候选
        self.fake.calls.clear()
        with P.BlackboxProtocol(config=self.config, persona=self.persona) as proto:
            proto.list_keys()
        self.assertEqual(self.fake.calls, [("GET", "/api/user/api-keys")])

    def test_no_key_endpoint_raises(self):
        with P.BlackboxProtocol(config=self.config, persona=self.persona) as proto:
            with self.assertRaises(KeyNotObtainable):
                proto.list_keys()

    def test_create_key_when_list_empty(self):
        self.fake.add("GET", f"{BASE}/api/keys", 200, body={"keys": []})
        self.fake.add("POST", f"{BASE}/api/keys", 201, body={"key": "sk-brandnewkey000000000"})
        with P.BlackboxProtocol(config=self.config, persona=self.persona) as proto:
            key, row = proto.create_key("bbx-20260101")
        self.assertEqual(key, "sk-brandnewkey000000000")
        self.assertIn(("POST", "/api/keys"), self.fake.calls)

    def test_full_run_uses_listed_key(self):
        self.fake.add("GET", f"{BASE}/login", 200, text="ok")
        self.fake.add("GET", f"{BASE}/api/auth/providers", 200, body={"google": {"id": "google"}})
        self.fake.add("GET", f"{BASE}/api/auth/session", 200, body={"user": {"id": "u9", "email": "b@own.test"}})
        self.fake.add("GET", f"{BASE}/api/keys", 200, body={"keys": [{"id": "k9", "key": "sk-listedkey0000000009"}]})
        with P.BlackboxProtocol(config=self.config, persona=self.persona) as proto:
            reg = proto.run(GoogleIdentity(email="b@own.test"))
        self.assertEqual(reg.api_key, "sk-listedkey0000000009")
        self.assertEqual(reg.engine, "protocol")
        record = reg.to_record()
        self.assertEqual(record["email"], "b@own.test")
        self.assertTrue(record["apiKey"].startswith("sk-"))

    def test_probe_403_raises_plan_required(self):
        self.fake.add("GET", f"{BASE}/login", 200, text="ok")
        self.fake.add("GET", f"{BASE}/api/auth/session", 200, body={"user": {"email": "c@own.test"}})
        self.fake.add("GET", f"{BASE}/api/keys", 200, body={"keys": [{"key": "sk-planlocked00000000"}]})
        self.fake.add("POST", f"{API}/v1/chat/completions", 403, body={"error": {"message": "needs Pro"}})
        config = make_config(self.tmp, probe=True)
        with P.BlackboxProtocol(config=config, persona=self.persona) as proto:
            with self.assertRaises(PlanRequired):
                proto.run(GoogleIdentity(email="c@own.test"))


class RateLimitCase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="bbx-rl-")
        self.fake = FakeSite()
        self._original = patch_transport(self.fake)
        self.config = make_config(self.tmp)
        self.persona = make_persona(seed="rl")

    def tearDown(self):
        netmod.make_client = self._original
        for root, _dirs, files in os.walk(self.tmp, topdown=False):
            for name in files:
                os.unlink(os.path.join(root, name))
            for name in _dirs:
                os.rmdir(os.path.join(root, name))
        os.rmdir(self.tmp)

    def test_429_maps_to_rate_limited(self):
        from blackbox_reg.errors import RateLimited

        self.fake.add("GET", f"{BASE}/api/auth/session", 429, body={"error": "too many"})
        with P.BlackboxProtocol(config=self.config, persona=self.persona, ) as proto:
            proto.session.retries = 1
            with self.assertRaises(RateLimited):
                proto.session_info()

    def test_401_maps_to_session_expired(self):
        self.fake.add("GET", f"{BASE}/api/keys", 401, body={"error": "no"})
        with P.BlackboxProtocol(config=self.config, persona=self.persona) as proto:
            with self.assertRaises(SessionExpired):
                proto.session.get(f"{BASE}/api/keys")


class UtilityCase(unittest.TestCase):
    def test_totp_rfc_vector(self):
        # RFC 6238 附录 B：seed "12345678901234567890"（base32）在 T=59 时是 287082
        secret = "GEZDGNBVGY3TQOJQGEZDGNBVGY3TQOJQ"
        self.assertEqual(totp_now(secret, at=59), "287082")

    def test_cli_settings_shape(self):
        payload = settings_payload(base_url="http://127.0.0.1:8787/p/blackbox/v1/", api_key="sk-dcg-x", model="blackboxai/blackbox-pro")
        auth = payload["security"]["auth"]
        self.assertEqual(auth["selectedType"], "blackbox-api")
        self.assertEqual(auth["selectedProvider"], "blackbox")
        self.assertEqual(auth["blackbox"]["baseUrl"], "http://127.0.0.1:8787/p/blackbox/v1")
        self.assertEqual(auth["blackbox"]["model"], "blackboxai/blackbox-pro")


if __name__ == "__main__":
    unittest.main()
