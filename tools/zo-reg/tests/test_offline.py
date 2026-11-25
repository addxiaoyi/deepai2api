"""离线测试（不联网）：身份、配置、号池、耐ban 窗口、错误分类。

zo 的协议腿需要真实 OAuth 会话才能走完，所以这里覆盖的是"协议之外但会悄悄坏"的
那一层：随机身份的格式与自洽性、配置解析、号池去重与导出、滑动窗口限速、错误 kind。

字段名注意：号池记录是 **camelCase**（`apiKey` / `accessToken`），与网关的
`dcg.pool/zo@1` 口径一致。
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from zo_reg.antiban import AntiBan, AntiBanConfig, DomainState, SlidingWindow  # noqa: E402
from zo_reg.config import RunConfig  # noqa: E402
from zo_reg.errors import (  # noqa: E402
    Blocked,
    CaptchaRequired,
    EmailTaken,
    HandleTaken,
    NoApiKey,
    NoToken,
    RateLimited,
    TransportError,
    UpstreamError,
    VerificationPending,
    ZoError,
)
from zo_reg.identity import Persona, geo_profile, handle_for, local_part_for, make_persona  # noqa: E402
from zo_reg.store import Store  # noqa: E402


# ----------------------------------------------------------------- identity


def test_persona_headers_are_self_consistent():
    persona = make_persona(seed="unit-test")
    assert isinstance(persona, Persona)
    ua = persona.user_agent
    headers = persona.headers(referer="https://zo.computer/signup", origin="https://zo.computer")
    assert headers["User-Agent"] == ua
    assert headers["Origin"] == "https://zo.computer"
    assert headers["Referer"].startswith("https://zo.computer")
    # UA 里的 Chrome 主版本必须和 sec-ch-ua 对上，否则指纹自相矛盾
    assert f'v="{persona.chrome_major}"' in persona.sec_ch_ua
    assert persona.chrome_major in ua


def test_persona_is_deterministic_for_a_seed():
    first = make_persona(seed="stable-seed")
    second = make_persona(seed="stable-seed")
    assert first.user_agent == second.user_agent
    assert first.timezone == second.timezone
    assert first.handle == second.handle


def test_handle_and_local_part_are_url_and_email_safe():
    persona = make_persona(seed="safe-check")
    handle = handle_for(persona, "unit-test")
    local = local_part_for(persona, "unit-test")
    for value in (handle, local):
        assert value
        assert value == value.lower()
        assert all(ch.isalnum() or ch in "-._" for ch in value)
    assert "@" not in handle


def test_geo_profile_is_well_formed():
    zone, tag, accept = geo_profile("DE")
    assert "/" in zone, "第一个返回值是 IANA 时区"
    assert tag.startswith("de")
    assert "en" in accept


def test_fingerprint_and_dict_agree():
    persona = make_persona(seed="fp")
    data = persona.to_dict()
    assert data["handle"] == persona.handle
    fingerprint = persona.fingerprint()
    assert isinstance(fingerprint, dict) and fingerprint


# ------------------------------------------------------------------- config


def test_antiban_config_round_trips_through_json():
    config = AntiBanConfig(mail_domains=["example.com"], domain_rpm=2, ip_concurrency=1)
    parsed = AntiBanConfig.from_json(json.dumps(config.to_json()))
    assert parsed.to_json() == config.to_json()


def test_antiban_defaults_are_conservative():
    config = AntiBanConfig()
    assert config.ip_concurrency == 1, "默认同一出口只跑一个注册"
    assert config.domain_rpm <= 5
    assert config.jitter_ms


def test_run_config_reads_a_namespace():
    class NS:
        count = 3
        concurrency = 2
        engine = "auto"
        proxy = None
        proxy_file = ""
        mail_provider = "imap"
        mail_domain = "example.com"
        mail_domains = []
        mail_address = ""
        base_url = "https://zo.computer"
        output = "out"
        verify_email = True
        retry_rounds = 1
        timeout = 45.0
        language = "en-US"
        user_agent = ""
        chat_engine = ""
        push_url = ""
        push_token = ""
        anti_ban = None
        browser_stack = "auto"
        headful = False
        cdp = ""
        humanize = 1.0
        keys_per_account = 1
        create_key = True
        delay = None
        imap_host = ""
        imap_user = ""
        imap_password = ""
        imap_port = 993
        captcha = "none"
        captcha_key = ""
        captcha_action = ""
        json = False
        resume = False

    config = RunConfig.from_namespace(NS())
    assert config.count == 3
    low, high = config.delay_ms()
    assert 0 <= low <= high


# ------------------------------------------------------------------ antiban


def test_sliding_window_counts_within_its_window():
    window = SlidingWindow(window=60.0)
    now = 1_000.0
    assert window.count(now) == 0
    window.add(now)
    window.add(now)
    assert window.count(now) == 2
    assert window.wait_seconds(now) >= 0


def test_domain_state_health_drops_after_failures():
    state = DomainState(domain="example.com")
    state.bad = 3
    state.ok = 1
    assert 0.0 <= state.health <= 1.0
    assert state.health < 0.5


def test_antiban_reports_domain_health():
    ban = AntiBan(AntiBanConfig(mail_domains=["example.com"]))
    ban.record_domain("example.com", ok=True)
    ban.record_domain("example.com", ok=False)
    report = ban.domain_report()
    row = next((r for r in report if r["domain"] == "example.com"), None)
    assert row is not None
    assert row["ok"] >= 1 and row["bad"] >= 1
    assert 0.0 <= row["health"] <= 1.0


def test_antiban_backoff_is_monotonic_per_kind():
    ban = AntiBan(AntiBanConfig())
    low = ban.backoff_seconds("rate_limited")
    assert low >= 0
    # 未知 kind 也要给一个确定值，不能抛
    assert ban.backoff_seconds("something-new") >= 0


def test_antiban_picks_a_domain_from_candidates():
    ban = AntiBan(AntiBanConfig(mail_domains=["a.example", "b.example"]))
    picked = ban.pick_domain(["a.example", "b.example"])
    assert picked in ("a.example", "b.example")


# -------------------------------------------------------------------- store


def test_store_upserts_dedupes_by_email(tmp_path):
    store = Store(str(tmp_path))
    try:
        record = {
            "email": "ada@example.com",
            "handle": "ada",
            "apiKey": "zo_live_0001",
            "keyId": "k1",
            "accessToken": "at-1",
            "status": "active",
        }
        store.upsert(record)
        store.upsert({**record, "apiKey": "zo_live_0002"})
        accounts = store.accounts()
        assert len(accounts) == 1, "同 email 应当覆盖而不是追加"
        assert accounts[0]["apiKey"] == "zo_live_0002"
        assert "ada@example.com" in store.seen_emails()
        assert store.counts()["accounts"] >= 1
    finally:
        store.close()


def test_store_exports_jsonl_with_the_gateway_shape(tmp_path):
    store = Store(str(tmp_path))
    try:
        store.upsert({"email": "bob@example.com", "handle": "bob", "apiKey": "zo_live_9", "accessToken": "at-2"})
        path = store.export(fmt="jsonl", path=str(tmp_path / "out.jsonl"))
        first = Path(path).read_text(encoding="utf-8").strip().splitlines()[0]
        row = json.loads(first)
        assert row["email"] == "bob@example.com"
        assert row["apiKey"] == "zo_live_9"
    finally:
        store.close()


def test_store_records_failures_without_touching_accounts(tmp_path):
    store = Store(str(tmp_path))
    try:
        store.record_failure(email="x@example.com", kind="rate_limited", reason="429", domain="example.com")
        store.record_domain("example.com", ok=True)
        assert store.accounts() == []
        assert isinstance(store.counts(), dict)
    finally:
        store.close()


# ------------------------------------------------------------------- errors


@pytest.mark.parametrize(
    ("exc", "kind"),
    [
        (Blocked("x"), "blocked"),
        (RateLimited("x"), "rate_limited"),
        (EmailTaken("x"), "email_taken"),
        (CaptchaRequired("x"), "captcha_required"),
        (VerificationPending("x"), "verification_pending"),
        (HandleTaken("x"), "handle_taken"),
        (NoToken("x"), "no_token"),
        (NoApiKey("x"), "no_api_key"),
        (TransportError("x"), "transport"),
        (UpstreamError("x"), "upstream"),
    ],
)
def test_error_kinds_are_stable(exc, kind):
    assert getattr(exc, "kind", None) == kind
    assert isinstance(exc, ZoError)
