"""离线用例：Novita 协议 + 防封策略 + 配置归一。

不联网、不注册真实账号，全部用 `httpx.MockTransport` 复刻线上形为
（响应体形状来自 2026-11 对 api-server.novita.ai 的实测，见 _raw 取证）。

跑法：
    python -m pytest tests/test_novita_offline.py -q
或：
    python -m unittest discover -s tests
"""

from __future__ import annotations

import json
import sys
import unittest
from pathlib import Path

import httpx

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from novita_reg.antiban import DomainRotator, IpGate  # noqa: E402
from novita_reg.config import AntiBanConfig, Config, _split_api_base  # noqa: E402
from novita_reg.errors import (  # noqa: E402
    CaptchaRequired,
    EmailRejected,
    EmailTaken,
    KeyNotObtainable,
    RateLimited,
)
from novita_reg.protocol import Novita, probe  # noqa: E402


def json_response(payload: dict, status: int = 200) -> httpx.Response:
    return httpx.Response(status, json=payload)


class Panel:
    """一个可编程的假面板：按路径返回真实形状。"""

    def __init__(self) -> None:
        self.calls: list[tuple[str, str, dict]] = []
        self.register_response = json_response({})
        self.login_response = json_response({"token": "jwt-abc"})
        self.key_response = json_response({"key": "sk_live_0123456789abcdef", "id": "k1"})
        self.verify_response = json_response({})
        self.info_response = json_response({"user": {"nickname": "ada"}})
        self.list_response = json_response({"keys": [{"id": "k0", "status": "active"}]})
        self.models_response = json_response({"data": [{"id": "deepseek/deepseek-v4.1-flash"}]})

    def __call__(self, request: httpx.Request) -> httpx.Response:
        path = request.url.path
        body: dict = {}
        if request.content:
            try:
                body = json.loads(request.content.decode())
            except json.JSONDecodeError:
                body = {}
        self.calls.append((request.method, path, body))
        if path.endswith("/v1/user/register"):
            return self.register_response
        if path.endswith("/v1/user/login"):
            return self.login_response
        if path.endswith("/v1/user/email/verify"):
            return self.verify_response
        if path.endswith("/v1/user/info"):
            return self.info_response
        if path.endswith("/v2/user/key"):
            return self.key_response if request.method == "POST" else self.list_response
        if path.endswith("/v1/models"):
            return self.models_response
        return httpx.Response(404, json={"code": 404, "reason": "UNKNOWN"})


class ProtocolTests(unittest.TestCase):
    def make(self) -> tuple[Novita, Panel]:
        panel = Panel()
        client = Novita(
            panel="https://novita.ai",
            panel_api="https://api-server.novita.ai",
            api_base="https://api.novita.ai",
            openai_base="https://api.novita.ai/openai",
            transport=httpx.MockTransport(panel),
        )
        return client, panel

    def test_register_body_matches_frontend(self) -> None:
        client, panel = self.make()
        client.register("ada@example.com", "Passw0rd!2345", turnstile="tok")
        method, path, body = panel.calls[-1]
        self.assertEqual(method, "POST")
        self.assertEqual(path, "/v1/user/register")
        for field in ("email", "password", "confirmPassword", "cloudflareToken", "agreementVersion"):
            self.assertIn(field, body)
        self.assertEqual(body["confirmPassword"], body["password"])
        self.assertEqual(body["cloudflareToken"], "tok")
        self.assertTrue(body["allowNotification"])

    def test_captcha_error_is_typed(self) -> None:
        client, panel = self.make()
        panel.register_response = json_response(
            {"code": 500, "reason": "CLOUDFLARE_CHECK_ERROR", "message": "cloudflare token check error"},
            status=500,
        )
        with self.assertRaises(CaptchaRequired):
            client.register("a@b.co", "x", turnstile="")

    def test_email_rejected_and_taken(self) -> None:
        client, panel = self.make()
        panel.register_response = json_response(
            {"code": 400, "reason": "INVALID_EMAIL", "message": "please use your own email address"}, status=400
        )
        with self.assertRaises(EmailRejected):
            client.register("a@temp.io", "x", turnstile="t")

        panel.register_response = json_response(
            {"code": 400, "reason": "USER_ALREADY_EXIST", "message": "user exist"}, status=400
        )
        with self.assertRaises(EmailTaken):
            client.register("a@b.co", "x", turnstile="t")

    def test_login_stores_jwt_and_authorises(self) -> None:
        client, panel = self.make()
        payload = client.login("a@b.co", "x", turnstile="t")
        self.assertEqual(payload["token"], "jwt-abc")
        self.assertEqual(client.token, "jwt-abc")

    def test_create_key_returns_plaintext_once(self) -> None:
        client, panel = self.make()
        body = client.create_key(name="gw-1", expire_time="30d")
        self.assertEqual(body["key"], "sk_live_0123456789abcdef")
        _, path, sent = panel.calls[-1]
        self.assertEqual(path, "/v2/user/key")
        self.assertEqual(sent["expireTime"], 30 * 86400, "expireTime 必须先归一成秒")

    def test_create_key_without_plaintext_raises(self) -> None:
        client, panel = self.make()
        panel.key_response = json_response({"id": "k1", "maskedKey": "sk_****"}, status=200)
        with self.assertRaises(KeyNotObtainable):
            client.create_key(name="gw-1")

    def test_rate_limit_maps_to_kind(self) -> None:
        client, panel = self.make()
        panel.login_response = json_response({"code": 429, "message": "too many"}, status=429)
        with self.assertRaises(RateLimited):
            client.login("a@b.co", "x", turnstile="t")

    def test_verify_token_extracted_from_link(self) -> None:
        from novita_reg.protocol import _token_from_link

        self.assertEqual(
            _token_from_link("https://novita.ai/settings/verify?token=abc.def-1"),
            "abc.def-1",
        )
        self.assertEqual(_token_from_link("https://novita.ai/settings/verify"), "")

    def test_probe_reports_sitekey(self) -> None:
        panel = Panel()
        report = probe(
            "https://novita.ai",
            panel_api="https://api-server.novita.ai",
            api_base="https://api.novita.ai",
            transport=httpx.MockTransport(panel),
        )
        self.assertIn("turnstile_site_key", report)
        self.assertEqual(report["turnstile_field"], "cloudflareToken")


class AntiBanTests(unittest.TestCase):
    def test_camel_case_from_gateway_actually_applies(self) -> None:
        """网关用 camelCase 下发；丢弃了就等于防封没生效。"""
        anti = AntiBanConfig.from_dict(
            {
                "mailDomains": ["a.com"],
                "domainRpm": 1,
                "ipConcurrency": 2,
                "jitterMs": [10, 20],
                "geoAlign": False,
                "sessionTtlHours": 12,
                "cooldownAfterBatch": 0,
                "backoff": {"blocked": [1, 2]},
            }
        )
        self.assertEqual(anti.mail_domains, ["a.com"])
        self.assertEqual(anti.domain_rpm, 1)
        self.assertEqual(anti.ip_concurrency, 2)
        self.assertEqual(anti.jitter_ms, [10, 20])
        self.assertFalse(anti.geo_align)
        self.assertEqual(anti.session_ttl_hours, 12)
        self.assertEqual(anti.batch_pause, [0, 0], "标量 0 不能把 list 字段污染成 int")
        self.assertEqual(anti.backoff["blocked"], [1, 2])

    def test_domain_rotator_respects_minute_window(self) -> None:
        anti = AntiBanConfig.from_dict({"domainRpm": 1, "domainRph": 100})
        rotator = DomainRotator(["a.com", "b.com"], anti)
        first = rotator.pick()
        self.assertIn(first, ("a.com", "b.com"))
        rotator.note_send(first)
        # a.com 已经用掉这分钟唯一的额度 → 只能选另一个
        second = rotator.pick()
        self.assertNotEqual(second, first)
        rotator.note_send(second)
        self.assertEqual(rotator.pick(), "", "两个域名都满额时应返回空，而不是超发")

    def test_ip_gate_serialises_same_egress(self) -> None:
        gate = IpGate(1)
        with gate.slot("http://1.2.3.4:8080", timeout=0.2) as got:
            self.assertTrue(got)
            self.assertFalse(gate.acquire("http://1.2.3.4:8080", timeout=0.05))
        self.assertTrue(gate.acquire("http://1.2.3.4:8080", timeout=0.05))
        gate.release("http://1.2.3.4:8080")


class ConfigTests(unittest.TestCase):
    def test_api_base_from_catalog_is_normalised(self) -> None:
        """网关传 catalog 的 apiBase（带 /openai），worker 需要原生根。"""
        root, openai = _split_api_base("https://api.novita.ai/openai", "x")
        self.assertEqual(root, "https://api.novita.ai")
        self.assertEqual(openai, "https://api.novita.ai/openai")

    def test_gateway_overrides_are_honoured(self) -> None:
        cfg = Config.load(
            None,
            {
                "engine": "hybrid",
                "base_url": "https://novita.ai",
                "api_base": "https://api.novita.ai/openai",
                "out_dir": "data/novita-reg",
                "keys_per_account": 3,
                "anti_ban": json.dumps({"mailDomains": ["x.com"], "ipConcurrency": 1}),
                "key_policy": json.dumps({"expire_time": "90d"}),
            },
        )
        self.assertEqual(cfg.engine, "hybrid")
        self.assertEqual(cfg.api_base, "https://api.novita.ai")
        self.assertEqual(cfg.openai_base, "https://api.novita.ai/openai")
        self.assertEqual(str(cfg.output_dir).replace("\\", "/"), "data/novita-reg")
        self.assertEqual(cfg.key_policy.per_account, 3)
        self.assertEqual(cfg.key_policy.expire_time, "90d")
        self.assertEqual(cfg.anti_ban.mail_domains, ["x.com"])

    def test_env_proxy_needs_explicit_opt_in(self) -> None:
        """机器上的 HTTPS_PROXY 不能静默接管注册流量（doctor 会误报不可达）。"""
        import os

        saved = {k: os.environ.get(k) for k in ("NOVITA_PROXY", "HTTPS_PROXY", "NOVITA_USE_ENV_PROXY")}
        try:
            for key in saved:
                os.environ.pop(key, None)
            os.environ["HTTPS_PROXY"] = "http://corp-proxy:8080"
            self.assertEqual(Config.load(None, {}).proxies, [], "通用代理默认不采用")

            os.environ["NOVITA_USE_ENV_PROXY"] = "1"
            self.assertEqual(Config.load(None, {}).proxies, ["http://corp-proxy:8080"])

            os.environ["NOVITA_PROXY"] = "socks5://127.0.0.1:7890"
            self.assertEqual(Config.load(None, {}).proxies, ["socks5://127.0.0.1:7890"], "显式代理优先")
        finally:
            for key, value in saved.items():
                if value is None:
                    os.environ.pop(key, None)
                else:
                    os.environ[key] = value
    def test_output_paths_are_provider_scoped(self) -> None:
        cfg = Config.load(None, {"out_dir": "data/novita-reg"})
        name = Path(cfg.db_path).name
        self.assertEqual(name, "accounts.sqlite3")
        self.assertIn("novita-reg", str(cfg.db_path).replace("\\", "/"))


if __name__ == "__main__":
    unittest.main(verbosity=2)
