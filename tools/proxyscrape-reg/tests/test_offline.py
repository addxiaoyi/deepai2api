"""离线测试：代理解析、协议（注册/登录/试用/凭据）、号池。

用 `httpx.MockTransport` 模拟 dashboard 的 JSON 形状；不联网。
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

import httpx
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from proxyscrape_reg.errors import (  # noqa: E402
    EmailTaken,
    NoCredentials,
    NoTrial,
    ProxyScrapeError,
    RateLimited,
    TurnstileRequired,
)
from proxyscrape_reg.net import normalize_endpoint  # noqa: E402
from proxyscrape_reg.protocol import (  # noqa: E402
    ProxyScrape,
    extract_proxies,
    parse_proxy_text,
)
from proxyscrape_reg.store import Store, split_endpoint  # noqa: E402

SUMMARY = {
    "success": True,
    "data": {
        "user": {"email": "ada@example.com"},
        "services": {
            "datacenter_shared": {
                "id": "sub-1",
                "provider": "proxyventure",
                "proxy_credentials": {
                    "host": "proxy.proxyscrape.com",
                    "ports": [8080, 8081],
                    "username": "user-abc",
                    "password": "pass-def",
                },
            }
        },
    },
}


class FakeDashboard:
    def __init__(self, *, claimed: bool = False, credentials: dict | None = None):
        self.claimed = claimed
        self.credentials = credentials if credentials is not None else SUMMARY["data"]["services"]["datacenter_shared"]["proxy_credentials"]
        self.users: dict[str, str] = {}
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

        if path.endswith("/v2/v4/account/auth/register"):
            if not body.get("cf_turnstile_token"):
                return httpx.Response(400, json={"success": False, "message": "No Cloudflare Turnstile token provided."})
            email = body.get("email", "")
            if email in self.users:
                return httpx.Response(400, json={"success": False, "message": "An account already exists with this email"})
            self.users[email] = body.get("password", "")
            return httpx.Response(200, json={"success": True, "data": {"tokens": {"access": "acc-1", "refresh": "ref-1"}}})

        if path.endswith("/v2/v4/account/auth/login"):
            if "captcha" in body.get("email", ""):
                return httpx.Response(400, json={"success": False, "message": "turnstile verification failed"})
            if self.users.get(body.get("email")) != body.get("password"):
                return httpx.Response(401, json={"success": False, "message": "invalid credentials"})
            return httpx.Response(200, json={"success": True, "data": {"tokens": {"access": "acc-1", "refresh": "ref-1"}}})

        if path.endswith("/v2/v4/account/premium/trial-eligibility"):
            return httpx.Response(200, json={"success": True, "data": {"claimed": self.claimed}})

        if path.endswith("/v2/v4/account/premium/claim-trial"):
            if self.claimed:
                return httpx.Response(400, json={"success": False, "message": "Trial already claimed"})
            self.claimed = True
            return httpx.Response(200, json={"success": True, "data": {"claimed": True}})

        if path.endswith("/v2/v4/account/accounts-summary"):
            return httpx.Response(200, json={"success": True, "data": {"services": {"datacenter_shared": {"id": "sub-1", "proxy_credentials": self.credentials}}}})

        if "/proxy-setup/" in path or "/proxy-list/" in path:
            if not self.credentials:
                return httpx.Response(404, json={"success": False, "message": "no subscription"})
            return httpx.Response(200, text="proxy.proxyscrape.com:9000:user-xyz:pass-xyz\n")

        if path.endswith("/v2/v4/account/api-keys"):
            return httpx.Response(200, json={"success": True, "data": [{"id": "k1", "key": "ps_123"}]})

        if path.endswith("/v2/v4/account/verify-email"):
            return httpx.Response(302, headers={"location": "https://dashboard.proxyscrape.com/v2/overview"})

        return httpx.Response(404, json={"success": False, "message": "not found"})


def session_for(fake: FakeDashboard) -> ProxyScrape:
    return ProxyScrape(transport=httpx.MockTransport(fake.handler), timeout=5.0)


# ------------------------------------------------------------------ 代理解析


def test_normalize_endpoint_shapes():
    assert normalize_endpoint("1.2.3.4", 8080) == "http://1.2.3.4:8080"
    assert normalize_endpoint("h", "8080", "u", "p") == "http://u:p@h:8080"
    assert normalize_endpoint("", 8080) == ""
    assert normalize_endpoint("h", "not-a-port") == ""


def test_extract_proxies_walks_nested_payloads():
    found = extract_proxies(SUMMARY)
    assert "http://user-abc:pass-def@proxy.proxyscrape.com:8080" in found
    assert "http://user-abc:pass-def@proxy.proxyscrape.com:8081" in found
    assert len(found) == 2


def test_extract_proxies_finds_ip_port_pairs():
    payload = {"nodes": [{"ip": "10.0.0.5", "port": 3128, "user": "a", "pass": "b"}]}
    found = extract_proxies(payload)
    assert found == ["http://a:b@10.0.0.5:3128"]


def test_parse_proxy_text_handles_panel_formats():
    text = "\n".join(
        [
            "# comment",
            "http://u:p@1.2.3.4:8080",
            "5.6.7.8:3128:user:pass",
            "9.9.9.9:1080",
            "",
        ]
    )
    rows = parse_proxy_text(text)
    assert "http://u:p@1.2.3.4:8080" in rows
    assert "http://user:pass@5.6.7.8:3128" in rows
    assert "http://9.9.9.9:1080" in rows
    assert len(rows) == 3


def test_split_endpoint_round_trips():
    parts = split_endpoint("http://u:p@host.example:8080")
    assert parts["host"] == "host.example"
    assert parts["port"] == 8080
    assert parts["username"] == "u"
    assert parts["password"] == "p"


# ---------------------------------------------------------------------- 协议


def test_register_requires_turnstile():
    fake = FakeDashboard()
    with session_for(fake) as client:
        with pytest.raises(TurnstileRequired):
            client.register("ada@example.com", "Xk9#mQ2vLp7!aB")


def test_register_then_login_captures_tokens():
    fake = FakeDashboard()
    with session_for(fake) as client:
        client.register("ada@example.com", "Xk9#mQ2vLp7!aB", turnstile="ts-1")
        assert client.access_token == "acc-1"
        assert client.refresh_token == "ref-1"
    with session_for(fake) as again:
        again.login("ada@example.com", "Xk9#mQ2vLp7!aB")
        assert again.access_token == "acc-1"


def test_email_taken_maps_to_typed_error():
    fake = FakeDashboard()
    with session_for(fake) as client:
        client.register("dup@example.com", "Xk9#mQ2vLp7!aB", turnstile="ts")
        with pytest.raises(EmailTaken):
            client.register("dup@example.com", "Xk9#mQ2vLp7!aB", turnstile="ts")


def test_trial_claim_is_idempotent_and_typed():
    fake = FakeDashboard(claimed=False)
    with session_for(fake) as client:
        client.register("trial@example.com", "Xk9#mQ2vLp7!aB", turnstile="ts")
        assert client.trial_claimed() is False
        client.claim_trial()
        assert client.trial_claimed() is True
        with pytest.raises(NoTrial):
            client.claim_trial()


def test_proxies_reads_credentials_and_setup_pages():
    fake = FakeDashboard()
    with session_for(fake) as client:
        client.register("p@example.com", "Xk9#mQ2vLp7!aB", turnstile="ts")
        rows = client.proxies()
        assert "http://user-abc:pass-def@proxy.proxyscrape.com:8080" in rows
        # 产品级 proxy-setup 的文本也会被解析进来
        assert any("user-xyz" in row for row in rows)


def test_proxies_without_credentials_is_typed():
    fake = FakeDashboard(credentials={})
    with session_for(fake) as client:
        client.register("n@example.com", "Xk9#mQ2vLp7!aB", turnstile="ts")
        with pytest.raises(NoCredentials):
            client.proxies()


def test_run_flow_end_to_end():
    fake = FakeDashboard()
    with session_for(fake) as client:
        record = client.run_flow(
            "flow@example.com",
            "Xk9#mQ2vLp7!aB",
            turnstile_factory=lambda: "ts-flow",
            claim=True,
        )
        assert record["email"] == "flow@example.com"
        assert record["trial_claimed"] is True
        assert len(record["proxies"]) >= 2
        assert record["api_keys"][0]["key"] == "ps_123"


# ---------------------------------------------------------------------- 号池


def test_store_keeps_accounts_and_dedupes_proxies(tmp_path):
    store = Store(tmp_path / "ps.sqlite3")
    try:
        record = {
            "email": "a@example.com",
            "password": "pw",
            "access_token": "acc",
            "trial_claimed": True,
            "proxies": [
                "http://u:p@h1:8080",
                "http://u:p@h1:8080",
                "http://u:p@h2:9090",
            ],
        }
        assert store.add(record, engine="protocol", proxy="") is True
        assert store.proxies() == ["http://u:p@h1:8080", "http://u:p@h2:9090"]
        counts = store.counts()
        assert counts == {"accounts": 1, "proxies": 2, "trial_claimed": 1}
        assert store.accounts()[0]["proxy_count"] == 3
    finally:
        store.close()


def test_store_exports_proxy_list_for_downstream(tmp_path):
    store = Store(tmp_path / "ps.sqlite3")
    try:
        store.add(
            {"email": "b@example.com", "password": "pw", "trial_claimed": True, "proxies": ["http://u:p@h:1"]},
            engine="protocol",
            proxy="",
        )
        target = tmp_path / "proxies.txt"
        assert store.export_proxies(target) == 1
        assert target.read_text(encoding="utf-8").strip() == "http://u:p@h:1"
    finally:
        store.close()


def test_store_records_failures(tmp_path):
    store = Store(tmp_path / "ps.sqlite3")
    try:
        store.record_failure(email="x@example.com", kind="turnstile_required", reason="no token")
        summary = store.failure_summary()
        assert summary[0]["kind"] == "turnstile_required"
    finally:
        store.close()
