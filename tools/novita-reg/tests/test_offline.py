"""Novita 离线测试：不联网、不起浏览器。

覆盖点：
  1. 错误映射（401 / 402 / 403 model_access_denied / 403 实名 / 429）
  2. 防封退避表（含 verify_required / quota_exhausted 的零退避）
  3. 域名速率窗
  4. 多 key 上限与 keys 表
  5. 余额单位换算（1/10000 USD）
  6. 异步任务状态机（QUEUED → PROCESSING → SUCCEED / FAILED）
  7. 注册请求体字段（cloudflareToken / confirmPassword / agreementVersion）
  8. engine=protocol 能建号（与 wavespeed 的 OAuth-only 不同）
  9. 同设备多账号隔离计划

跑法：
    cd tools/novita-reg && python tests/test_offline.py
"""

from __future__ import annotations

import sys
import tempfile
from pathlib import Path

import httpx

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from novita_reg.antiban import (  # noqa: E402
    DomainRotator,
    key_policy_payload,
    profile_isolation_plan,
    should_rotate_identity,
    should_switch_engine,
)
from novita_reg.config import AntiBanConfig, Config, KeyPolicyConfig  # noqa: E402
from novita_reg.errors import (  # noqa: E402
    Blocked,
    CaptchaRequired,
    EmailTaken,
    KeyNotObtainable,
    NoApiKey,
    QuotaExhausted,
    RateLimited,
    SignupError,
    VerifyRequired,
)
from novita_reg.protocol import (  # noqa: E402
    DEFAULT_AGREEMENT_VERSION,
    TURNSTILE_FIELD,
    Novita,
    _normalize_expire,
    _raise_for,
)
from novita_reg.store import Store  # noqa: E402


def _resp(status: int, body: dict | str = "", headers: dict | None = None) -> httpx.Response:
    request = httpx.Request("GET", "https://api-server.novita.ai/x")
    if isinstance(body, dict):
        return httpx.Response(status, json=body, request=request, headers=headers or {})
    return httpx.Response(status, text=body, request=request, headers=headers or {})


# ---------------------------------------------------------------- 1. 错误映射


def test_error_status_mapping() -> None:
    cases = [
        ("blocked", _resp(403, "<html>Just a moment...</html>", {"server": "cloudflare"}), Blocked),
        ("quota", _resp(402, {"errorNo": "INSUFFICIENT_BALANCE"}), QuotaExhausted),
        ("rate", _resp(429, {"errorNo": "TOO_MANY_REQUESTS"}), RateLimited),
        ("captcha", _resp(400, {"errorNo": "TURNSTILE_ERROR", "errorInfo": "captcha failed"}), CaptchaRequired),
        ("taken", _resp(400, {"errorNo": "USER_ALREADY_EXIST"}), EmailTaken),
        ("verify", _resp(403, {"errorNo": "NEED_IDENTITY", "errorInfo": "Model requires identity verification"}), VerifyRequired),
    ]
    for label, response, expected in cases:
        try:
            _raise_for("/x", response)
        except Exception as exc:  # noqa: BLE001
            assert isinstance(exc, expected), f"{label}: 期望 {expected.__name__}，实际 {type(exc).__name__}"
        else:
            raise AssertionError(f"{label}: 没有抛异常")


def test_403_model_access_denied_is_not_blocked() -> None:
    """403 model_access_denied 是"这把 key 的策略问题"，不是边缘拦截。"""
    response = _resp(403, {"errorNo": "model_access_denied", "errorInfo": "key policy"})
    try:
        _raise_for("/openai/v1/chat/completions", response)
    except Blocked:
        raise AssertionError("model_access_denied 不该被当成 Cloudflare 拦截")
    except SignupError:
        pass


# ---------------------------------------------------------------- 2. 退避表


def test_backoff_table() -> None:
    anti = AntiBanConfig()
    for kind in ("rate_limited", "blocked", "captcha_required", "verification_pending"):
        low, high = anti.backoff_for(kind)
        assert low > 0 and high >= low, f"{kind} 退避区间不合法"
    for kind in ("verify_required", "quota_exhausted", "key_not_obtainable", "email_taken"):
        low, high = anti.backoff_for(kind)
        assert low == 0 and high == 0, f"{kind} 应该零退避（换账号而不是等待）"


def test_switch_predicates() -> None:
    assert should_switch_engine("blocked")
    assert should_switch_engine("key_not_obtainable")
    assert not should_switch_engine("verify_required")
    assert should_rotate_identity("email_taken")
    assert not should_rotate_identity("verify_required")


# ------------------------------------------------------------ 3. 域名速率窗


def test_domain_windows() -> None:
    import random as _random

    anti = AntiBanConfig(domain_rpm=2, domain_rph=100)
    rotator = DomainRotator(["a.com", "b.com"], anti)
    rotator.rng = _random.Random(3)
    picked = []
    for _ in range(4):
        domain = rotator.pick()
        assert domain, "还有余量时不该挑不到"
        rotator.note_send(domain)
        picked.append(domain)
    assert len(set(picked)) == 2
    assert rotator.available() == []


# ------------------------------------------------------ 4. 多 key 与 keys 表


def test_store_multiple_keys() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        store = Store(Path(tmp) / "a.sqlite3")
        try:
            ok = store.add(
                {
                    "email": "multi@example.com",
                    "password": "pw",
                    "name": "Multi",
                    "api_key": "sk_" + "a" * 32,
                    "api_key_source": "panel-api",
                    "key_prefix": "sk_aaaaa",
                    "key_last4": "aaaa",
                    "extra_keys": [
                        {"key": "sk_" + "b" * 32, "source": "created"},
                        {"key": "sk_" + "c" * 32, "source": "created"},
                    ],
                    "expire_time": "90d",
                    "email_verified": True,
                },
                engine="protocol",
                proxy="",
            )
            assert ok
            assert store.count() == 1
            assert store.key_count() == 3, f"主 key + 2 个额外 key，实际 {store.key_count()}"
            keys = store.keys_for("multi@example.com")
            assert len(keys) == 3
            assert all(k["key"].startswith("sk_") for k in keys)
            records = store.pool_records()
            assert len(records) == 1
            assert len(records[0]["extraKeys"]) == 2
        finally:
            store.close()


def test_store_duplicate_email_rejected() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        store = Store(Path(tmp) / "a.sqlite3")
        try:
            base = {"email": "dup@example.com", "api_key": "sk_" + "d" * 32, "name": "Dup"}
            assert store.add(base, engine="protocol", proxy="") is True
            assert store.add({**base, "api_key": "sk_" + "e" * 32}, engine="protocol", proxy="") is False
            assert store.count() == 1
        finally:
            store.close()


# --------------------------------------------------------- 5. 余额单位换算


def test_balance_unit_conversion() -> None:
    """余额接口返回 1/10000 USD 的整数。"""

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"availableBalance": 125000})

    client = Novita(transport=httpx.MockTransport(handler), token="sk_" + "x" * 32)
    try:
        result = client.balance()
    finally:
        client.close()
    assert result["usd"] == 12.5, f"125000/10000 应为 12.5 美元，实际 {result['usd']}"


# ----------------------------------------------------- 6. 异步任务状态机


def test_async_task_state_machine() -> None:
    state = {"polls": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        if request.method == "POST":
            assert request.url.path == "/v3/async/txt2img"
            return httpx.Response(200, json={"task_id": "t-1"})
        state["polls"] += 1
        status = ["TASK_STATUS_QUEUED", "TASK_STATUS_PROCESSING", "TASK_STATUS_SUCCEED"][min(state["polls"] - 1, 2)]
        return httpx.Response(200, json={"task": {"task_id": "t-1", "status": status}})

    client = Novita(transport=httpx.MockTransport(handler))
    try:
        task_id = client.async_image("sk_" + "y" * 32, "sd_xl_base_1.0.safetensors", {"prompt": "x"})
        assert task_id == "t-1"
        result = client.poll_task("sk_" + "y" * 32, task_id, poll=0.01, timeout=5)
    finally:
        client.close()
    assert result["task"]["status"] == "TASK_STATUS_SUCCEED"
    assert state["polls"] >= 3


def test_async_task_failed_raises() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        if request.method == "POST":
            return httpx.Response(200, json={"task_id": "t-2"})
        return httpx.Response(200, json={"task": {"status": "TASK_STATUS_FAILED", "reason": "nsfw"}})

    client = Novita(transport=httpx.MockTransport(handler))
    try:
        client.poll_task("sk_" + "z" * 32, "t-2", poll=0.01, timeout=2)
    except SignupError as exc:
        assert "fail" in str(exc).lower()
    else:
        raise AssertionError("FAILED 终态应该抛错")
    finally:
        client.close()


# ------------------------------------------------ 7. 注册请求体字段正确性


def test_register_payload_fields() -> None:
    """请求体必须带上 cloudflareToken / confirmPassword / agreementVersion。"""
    captured: dict = {}

    def handler(request: httpx.Request) -> httpx.Response:
        import json

        captured["path"] = request.url.path
        captured["body"] = json.loads(request.content.decode())
        return httpx.Response(200, json={"errorNo": 0})

    client = Novita(transport=httpx.MockTransport(handler))
    try:
        client.register("a@example.com", "Pw123456!", turnstile="tok-abc")
    finally:
        client.close()

    assert captured["path"] == "/v1/user/register"
    body = captured["body"]
    assert body["email"] == "a@example.com"
    assert body["password"] == "Pw123456!"
    assert body["confirmPassword"] == "Pw123456!", "confirmPassword 必须与 password 一致"
    assert body[TURNSTILE_FIELD] == "tok-abc", f"token 字段名应为 {TURNSTILE_FIELD}"
    assert body["agreementVersion"] == DEFAULT_AGREEMENT_VERSION
    assert body["allowNotification"] is True


def test_register_http_200_with_errorno_raises() -> None:
    """HTTP 200 + errorNo != 0 依然是失败。"""

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"errorNo": "USER_ALREADY_EXIST", "errorInfo": "exists"})

    client = Novita(transport=httpx.MockTransport(handler))
    try:
        client.register("dup@example.com", "Pw123456!", turnstile="t")
    except EmailTaken:
        pass
    else:
        raise AssertionError("body.errorNo 非 0 时应该抛错")
    finally:
        client.close()


# --------------------------------------------------- 8. 建 key 拿明文


def test_create_key_extracts_plaintext() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path == "/v2/user/key"
        return httpx.Response(200, json={"key": "sk_" + "k" * 40, "id": "key-1", "expireTime": 7776000})

    client = Novita(transport=httpx.MockTransport(handler))
    try:
        result = client.create_key(name="test", expire_time="90d")
    finally:
        client.close()
    assert result["key"].startswith("sk_")
    assert result["id"] == "key-1"


def test_create_key_without_plaintext_raises() -> None:
    """只回 maskedKey 时抛 KeyNotObtainable（提示换浏览器腿）。"""

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"errorNo": "KEY_LIMIT_EXCEEDED"})

    client = Novita(transport=httpx.MockTransport(handler))
    try:
        client.create_key(name="test")
    except KeyNotObtainable:
        pass
    else:
        raise AssertionError("拿不到明文时应抛 KeyNotObtainable")
    finally:
        client.close()


def test_expire_normalisation() -> None:
    assert _normalize_expire("permanent") == -1
    assert _normalize_expire("90d") == 90 * 86400
    assert _normalize_expire("30d") == 30 * 86400
    assert _normalize_expire("24h") == 86400
    assert _normalize_expire(1234) == 1234


# ------------------------------------------------ 9. 同设备多账号隔离


def test_profile_isolation_plan() -> None:
    a = profile_isolation_plan("a@example.com", root="profiles", seed=11)
    b = profile_isolation_plan("b@example.com", root="profiles", seed=22)
    assert a["profile_dir"] != b["profile_dir"], "两个账号必须有不同的 profile 目录"
    assert a["noise_seed"] != b["noise_seed"]
    assert a["isolated"] is True
    plain = profile_isolation_plan("a@example.com", root="", isolate=False)
    assert plain["isolated"] is False


def test_key_policy_payload() -> None:
    assert key_policy_payload(open_model_access=False) == {}
    assert key_policy_payload(open_model_access=True) == {"modelAccess": "all"}
    assert key_policy_payload(open_model_access=True, model_ids=["a/b"]) == {"modelAccess": ["a/b"]}


def test_verify_key_requires_sk_prefix() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"data": [{"id": "m"}]})

    client = Novita(transport=httpx.MockTransport(handler))
    try:
        client.verify_key("not-an-sk-key")
    except NoApiKey:
        pass
    else:
        raise AssertionError("非 sk_ 前缀应直接拒绝")
    finally:
        client.close()


def test_verify_key_ok() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        # openai_base 已含 /openai，所以完整路径是 /openai/v1/models
        assert request.url.path == "/openai/v1/models", request.url.path
        return httpx.Response(200, json={"data": [{"id": "deepseek/deepseek-v4.1-flash"}, {"id": "zai-org/glm-5.3"}]})

    client = Novita(transport=httpx.MockTransport(handler))
    try:
        result = client.verify_key("sk_" + "q" * 32)
    finally:
        client.close()
    assert result["ok"] is True
    assert result["models"] == 2


# ------------------------------------------------------------- 10. 配置


def test_config_overrides() -> None:
    cfg = Config.load(
        None,
        {
            "key_policy": {"per_account": 5, "expire_time": "30d", "open_model_access": True},
            "anti_ban": {"domain_rpm": 7, "isolate_profile": False},
        },
    )
    assert cfg.key_policy.per_account == 5
    assert cfg.key_policy.expire_time == "30d"
    assert cfg.key_policy.open_model_access is True
    assert cfg.anti_ban.domain_rpm == 7
    assert cfg.anti_ban.isolate_profile is False
    assert cfg.captcha.site_key.startswith("0x4AAAAAA")


def test_key_policy_defaults() -> None:
    policy = KeyPolicyConfig()
    assert policy.per_account == 1
    assert policy.expire_time == "permanent"
    assert policy.open_model_access is False


# ---------------------------------------------11. camoufox 启动参数（防回归）

# 来自 camoufox 0.5.9 的实测结论：timezone 非法（启动 TypeError）、
# os 传列表会让 fpgen 无解、screen 必须容纳 window、geoip 只接受 IP。
# 这些坑会被 engine=auto 的降级静默掩盖，所以必须用测试钉住。


def test_camoufox_options_are_legal() -> None:
    from novita_reg.camoufox_options import camoufox_launch_options, camoufox_legal_keys

    legal = camoufox_legal_keys()
    if not legal:
        return  # camoufox 未安装
    options = camoufox_launch_options(
        fingerprint={"platform": "Windows", "locale": "en-US", "timezone": "America/New_York"},
        proxy="http://user:pass@8.8.8.8:8080",
    )
    assert not (set(options) - legal), f"非法参数：{sorted(set(options) - legal)}"


def test_camoufox_never_passes_timezone() -> None:
    from novita_reg.camoufox_options import camoufox_launch_options

    options = camoufox_launch_options(
        fingerprint={"platform": "MacOS", "locale": "ja-JP", "timezone": "Asia/Tokyo"},
        proxy="http://1.1.1.1:8080",
    )
    assert "timezone" not in options


def test_camoufox_os_is_single_value_and_window_fits() -> None:
    from novita_reg.camoufox_options import camoufox_launch_options

    for platform in ("Windows", "MacOS", "Linux"):
        options = camoufox_launch_options(fingerprint={"platform": platform})
        assert isinstance(options["os"], str), f"{platform}: os 必须是单值"
        screen, window = options.get("screen"), options.get("window")
        if screen and window:
            assert screen.min_width <= window[0] <= screen.max_width
            assert screen.min_height <= window[1] <= screen.max_height


def test_geoip_only_accepts_ip() -> None:
    """`geoip=True` 会拿本机 IP（与代理不一致更糟）；域名会 InvalidIP 硬报错。"""
    from novita_reg.camoufox_options import camoufox_launch_options, geoip_target

    assert geoip_target("") == ""
    assert geoip_target("http://1.2.3.4:8080") == "1.2.3.4"
    assert geoip_target("http://nope.invalid:8080") == ""
    options = camoufox_launch_options(fingerprint={"platform": "Windows"}, proxy="http://1.1.1.1:8080")
    assert options.get("geoip") == "1.1.1.1"


def test_novita_isolation_still_intact() -> None:
    """camoufox 参数修复不能破坏 novita 的多账号隔离。"""
    from novita_reg.antiban import profile_isolation_plan

    a = profile_isolation_plan("a@x.com", root="p", seed=1)
    b = profile_isolation_plan("b@x.com", root="p", seed=2)
    assert a["profile_dir"] != b["profile_dir"]
    assert a["noise_seed"] != b["noise_seed"]


def main() -> int:
    tests = [
        (name, obj)
        for name, obj in sorted(globals().items())
        if name.startswith("test_") and callable(obj)
    ]
    failed = 0
    for name, fn in tests:
        try:
            fn()
            print(f"PASS {name}")
        except Exception as exc:  # noqa: BLE001
            failed += 1
            print(f"FAIL {name}: {type(exc).__name__}: {exc}")
    print(f"\n{len(tests) - failed}/{len(tests)} passed")
    return 0 if failed == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
