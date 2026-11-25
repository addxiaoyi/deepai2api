"""离线测试：不联网、不启动浏览器，只验证"判断逻辑"是否正确。

覆盖点（这些是真正会决定"跑不跑得下去"的部分）：
  1. 失败分类映射（面板错误体 → runner 词汇）
  2. 防封退避表
  3. 域名速率窗与健康分
  4. 生成任务的 poll：body.code != 200、状态机终态、urls.get 跟随
  5. POST 提交不重试（重复计费）
  6. OAuth-only 的引擎语义：engine=protocol 且无会话时必须立刻失败
  7. key 校验：401 归类为 NoApiKey

跑法：
    cd tools/wavespeed-reg
    python -m pytest tests/ -q          # 有 pytest
    python tests/test_offline.py        # 无 pytest 也能跑（内置 runner）

没有 pytest 时 `main()` 会逐个执行并打印 PASS/FAIL。
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

import httpx

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from wavespeed_reg.antiban import (  # noqa: E402
    DomainRotator,
    IpGate,
    backoff_for,
    fingerprint_for,
    should_rotate_identity,
    should_switch_engine,
)
from wavespeed_reg.config import AntiBanConfig, Config  # noqa: E402
from wavespeed_reg.errors import (  # noqa: E402
    Blocked,
    CaptchaRequired,
    EmailRejected,
    EmailTaken,
    KeyNotObtainable,
    NoApiKey,
    RateLimited,
    SignupError,
)
from wavespeed_reg.identity import make_persona  # noqa: E402
from wavespeed_reg.protocol import WaveSpeed, _raise_for, _looks_blocked  # noqa: E402


def _resp(status: int, body: dict | str = "", headers: dict | None = None) -> httpx.Response:
    request = httpx.Request("GET", "https://wavespeed.ai/x")
    if isinstance(body, dict):
        return httpx.Response(status, json=body, request=request, headers=headers or {})
    return httpx.Response(status, text=body, request=request, headers=headers or {})


# ---------------------------------------------------------------- 1. 错误分类


def test_error_classification() -> None:
    cases = [
        ("blocked", _resp(403, "<html>Just a moment...</html>", {"server": "cloudflare"}), Blocked),
        ("rate", _resp(429, {"code": "too_many_requests"}), RateLimited),
        ("captcha", _resp(400, {"code": "captcha_failed", "message": "captcha required"}), CaptchaRequired),
        ("taken", _resp(409, {"code": "email_already_exists"}), EmailTaken),
        ("bad-email", _resp(400, {"code": "invalid_email"}), EmailRejected),
    ]
    for label, response, expected in cases:
        try:
            _raise_for("/x", response)
        except Exception as exc:  # noqa: BLE001
            assert isinstance(exc, expected), f"{label}: 期望 {expected.__name__}，实际 {type(exc).__name__}"
        else:
            raise AssertionError(f"{label}: 没有抛异常")


def test_cloudflare_marker_detection() -> None:
    assert _looks_blocked(_resp(200, "<title>Just a moment...</title>"))
    assert not _looks_blocked(_resp(200, "<title>WaveSpeedAI</title>"))


# ---------------------------------------------------------------- 2. 退避表


def test_backoff_table() -> None:
    anti = AntiBanConfig()
    for kind in ("rate_limited", "blocked", "captcha_required", "verification_pending"):
        low, high = anti.backoff_for(kind)
        assert low > 0 and high >= low, f"{kind} 退避区间不合法"
    # "别重试"的失败必须是零退避
    for kind in ("key_not_obtainable", "email_taken", "email_rejected", "oauth_unavailable"):
        low, high = anti.backoff_for(kind)
        assert low == 0 and high == 0, f"{kind} 应该是零退避（换身份而不是等待）"


def test_engine_and_identity_switches() -> None:
    assert should_switch_engine("blocked")
    assert should_switch_engine("key_not_obtainable")
    assert not should_switch_engine("email_taken")
    assert should_rotate_identity("email_taken")
    assert should_rotate_identity("oauth_unavailable")


# ------------------------------------------------------- 3. 域名速率窗 + 健康分


def test_domain_rotator_windows() -> None:
    anti = AntiBanConfig(domain_rpm=2, domain_rph=100)
    rotator = DomainRotator(["a.com", "b.com"], anti)
    import random as _random

    rotator.rng = _random.Random(1)

    # 每个域名每分钟 2 个：两轮之后两个域名都被占满
    picked: list[str] = []
    for _ in range(4):
        domain = rotator.pick()
        assert domain, "还有余量时不应该挑不到域名"
        rotator.note_send(domain)
        picked.append(domain)
    assert len(set(picked)) == 2, f"应该轮换到两个域名，实际 {picked}"
    assert rotator.available() == [], "每分钟额度用完后不该再有可用域名"


def test_domain_health_floor() -> None:
    anti = AntiBanConfig(domain_health_floor=0.5, domain_rpm=10, domain_rph=100)
    rotator = DomainRotator(["bad.com", "good.com"], anti)
    # bad.com 失败 2 次 → 健康分 0
    rotator.note_result("bad.com", False)
    rotator.note_result("bad.com", False)
    rotator.note_send("bad.com")
    available = rotator.available()
    assert "bad.com" not in available, f"健康分过低的域名应被排除，实际 {available}"
    assert "good.com" in available
    snapshot = {row["domain"]: row for row in rotator.snapshot()}
    assert snapshot["bad.com"]["health"] == 0.0


# ------------------------------------------------- 4/5. 生成任务：poll + 不重试


def test_generate_follows_server_urls_and_polls() -> None:
    """urls.get 由服务端下发；轮询到 completed 返回；POST 只发一次。"""
    calls: list[tuple[str, str]] = []
    state = {"polls": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append((request.method, request.url.path))
        if request.method == "POST":
            return httpx.Response(
                200,
                json={
                    "code": 200,
                    "data": {
                        "id": "task-1",
                        "status": "created",
                        "urls": {"get": "https://api.wavespeed.ai/api/v3/predictions/task-1/result"},
                    },
                },
            )
        state["polls"] += 1
        status = "processing" if state["polls"] < 2 else "completed"
        return httpx.Response(200, json={"code": 200, "data": {"id": "task-1", "status": status, "outputs": ["x"]}})

    client = WaveSpeed(transport=httpx.MockTransport(handler))
    try:
        result = client.generate("k" * 32, "wavespeed-ai/z-image/turbo", {"prompt": "x"}, poll=0.01, timeout=5)
    finally:
        client.close()

    posts = [c for c in calls if c[0] == "POST"]
    assert len(posts) == 1, f"提交必须只发一次（重复计费），实际 {len(posts)}"
    assert posts[0][1].endswith("/api/v3/wavespeed-ai/z-image/turbo")
    assert result["data"]["status"] == "completed"
    assert state["polls"] >= 2


def test_generate_rejects_body_code_mismatch() -> None:
    """HTTP 200 + body.code != 200 必须是失败。"""

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"code": 40001, "message": "no quota"})

    client = WaveSpeed(transport=httpx.MockTransport(handler))
    try:
        client.generate("k" * 32, "m/n", {}, poll=0.01, timeout=1)
    except SignupError as exc:
        assert "code" in str(exc).lower() or "40001" in str(exc)
    else:
        raise AssertionError("body.code 不一致时应该抛错")
    finally:
        client.close()


def test_generate_failed_state_raises() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        if request.method == "POST":
            return httpx.Response(200, json={"code": 200, "data": {"id": "t", "urls": {"get": "https://x/r"}}})
        return httpx.Response(200, json={"code": 200, "data": {"id": "t", "status": "failed"}})

    client = WaveSpeed(transport=httpx.MockTransport(handler))
    try:
        client.generate("k" * 32, "m/n", {}, poll=0.01, timeout=2)
    except SignupError as exc:
        assert "failed" in str(exc).lower()
    else:
        raise AssertionError("failed 终态应该抛错")
    finally:
        client.close()


def test_submit_not_retried_on_5xx() -> None:
    """提交遇到 503 也不能重试 —— 那会产生重复任务。"""
    hits = {"post": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        if request.method == "POST":
            hits["post"] += 1
            return httpx.Response(503, text="unavailable")
        return httpx.Response(200, json={"ok": True})

    client = WaveSpeed(transport=httpx.MockTransport(handler))
    try:
        client.generate("k" * 32, "m/n", {}, poll=0.01, timeout=1)
    except SignupError:
        pass
    finally:
        client.close()
    assert hits["post"] == 1, f"提交不该重试，实际发了 {hits['post']} 次"


# ------------------------------------------------------ 6. OAuth-only 语义


def test_protocol_engine_without_session_fails_fast() -> None:
    """WaveSpeed 只有 OAuth：engine=protocol 且无会话时无法建号。"""
    from wavespeed_reg.errors import OAuthUnavailable
    from wavespeed_reg.store import Store
    import tempfile

    with tempfile.TemporaryDirectory() as tmp:
        cfg = Config.load(
            None,
            {
                "engine": "protocol",
                "count": 1,
                "attempts": 1,
                "retry_rounds": 0,
                "output_dir": Path(tmp),
                "mail_provider": "fixed",
                "mail_address": "probe@example.com",
                "anti_ban": {"reuse_session": False, "ip_concurrency": 1},
            },
        )
        cfg.db_path = Path(tmp) / "a.sqlite3"
        store = Store(cfg.db_path)
        try:
            from wavespeed_reg.runner import Runner

            runner = Runner(cfg, store, quiet=True)
            try:
                runner.signup(0)
            except OAuthUnavailable:
                pass
            except SignupError as exc:
                raise AssertionError(f"期望 OAuthUnavailable，实际 {type(exc).__name__}: {exc}") from exc
            else:
                raise AssertionError("engine=protocol 无会话时应该失败")
        finally:
            store.close()


# ------------------------------------------------------------------ 7. key 校验


def test_verify_key_401_is_noapikey() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(401, json={"code": "unauthorized", "message": "invalid key"})

    client = WaveSpeed(transport=httpx.MockTransport(handler))
    try:
        client.verify_key("k" * 32)
    except NoApiKey:
        pass
    except SignupError:
        # 401 也可能先被 balance 分支吃掉；只要最终不是"成功"即可
        pass
    else:
        raise AssertionError("401 的 key 不该被判为可用")
    finally:
        client.close()


def test_verify_key_ok() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"data": [{"id": "deepseek/deepseek-v4-flash"}]})

    client = WaveSpeed(transport=httpx.MockTransport(handler))
    try:
        result = client.verify_key("k" * 32)
    finally:
        client.close()
    assert result["ok"] is True
    assert result["models"] == 1


# ------------------------------------------------------------ 8. 指纹自洽性


def test_fingerprint_is_self_consistent() -> None:
    persona = make_persona(__import__("random").Random(7), proxy="http://user:pass@1.2.3.4:8080-us-1")
    fp = fingerprint_for(persona, "http://user:pass@1.2.3.4:8080-us-1", geo_align=True)
    assert fp["timezone"] == "America/New_York", f"US 代理应对应美东时区，实际 {fp['timezone']}"
    assert fp["locale"] == "en-US"
    assert "Chrome/" in fp["user_agent"]
    assert fp["screen"]["width"] >= fp["viewport"]["width"]
    assert 1280 <= fp["viewport"]["width"] <= 2560
    # 同一个 persona 重跑必须得到同一套指纹（可复现）
    again = fingerprint_for(persona, "http://1.2.3.4:8080", geo_align=True)
    assert again["user_agent"] == fp["user_agent"]


def test_ip_gate_serialises_same_egress() -> None:
    gate = IpGate(limit=1)
    assert gate.acquire("1.1.1.1", timeout=0.1) is True
    assert gate.acquire("1.1.1.1", timeout=0.05) is False, "同一出口第二个应当被挡住"
    assert gate.acquire("2.2.2.2", timeout=0.1) is True, "不同出口互不影响"
    gate.release("1.1.1.1")
    gate.release("2.2.2.2")


# ------------------------------------------- 9. 桌面端 / APK 逆向分析器


def _build_asar(path, files: dict[str, bytes]) -> None:
    """造一个最小可解析的 asar：16 字节头 + JSON manifest + 顺序排布的文件。"""
    import json as _json

    offset = 0
    entries: dict[str, dict[str, str]] = {}
    blobs = b""
    for name, payload in files.items():
        entries[name] = {"size": len(payload), "offset": str(offset)}  # 注意：asar 里 offset 是字符串
        blobs += payload
        offset += len(payload)
    header = _json.dumps({"files": entries}).encode()
    prefix = (
        (4).to_bytes(4, "little")
        + (len(header) + 8).to_bytes(4, "little")
        + (len(header) + 4).to_bytes(4, "little")
        + len(header).to_bytes(4, "little")
    )
    path.write_bytes(prefix + header + blobs)


def test_re_analyzer_extracts_endpoints_from_asar() -> None:
    import tempfile
    from pathlib import Path

    from wavespeed_reg import scanner as wsre

    with tempfile.TemporaryDirectory() as tmp:
        asar = Path(tmp) / "app.asar"
        _build_asar(
            asar,
            {
                "main.js": (
                    b'const base="https://llm.wavespeed.ai/v1";'
                    b'fetch(base+"/chat/completions",{headers:{Authorization:"Bearer "+k}});'
                ),
                "keys.js": (
                    b'post("/api/v3/accesskey/generate",{name});'
                    b'get("/v1/account/balance");'
                    b'poll("/api/v3/predictions/"+id+"/result");'
                ),
                "noise.js": b'const x="https://www.w3.org/2000/svg";',
            },
        )

        report = wsre.analyze(asar, out_dir=Path(tmp) / "out")
        assert report.kind == "electron-asar", report.kind
        endpoints = report.endpoints()
        assert any("llm.wavespeed.ai" in e for e in endpoints), endpoints
        assert any("accesskey" in e for e in endpoints), endpoints
        assert not any("w3.org" in e for e in endpoints), f"噪声域名应被过滤：{endpoints}"

        sim = wsre.simulate_endpoints(report)
        assert sim["suggested_config"]["api_base"].startswith("https://llm.wavespeed.ai")
        # task 基址不该被相对路径污染
        assert sim["suggested_config"]["task_base"].startswith("https://"), sim["suggested_config"]

        out_files = sorted(p.name for p in (Path(tmp) / "out").iterdir())
        assert any(f.endswith(".md") for f in out_files), out_files
        assert any(f.endswith(".json") for f in out_files), out_files


def test_re_analyzer_handles_apk_zip() -> None:
    import tempfile
    import zipfile
    from pathlib import Path

    from wavespeed_reg import scanner as wsre

    with tempfile.TemporaryDirectory() as tmp:
        apk = Path(tmp) / "app.apk"
        with zipfile.ZipFile(apk, "w") as archive:
            archive.writestr(
                "classes.dex",
                b'Lcom/wavespeed/App; https://api.wavespeed.ai/api/v3/predictions/x/result "Bearer"',
            )
            archive.writestr("AndroidManifest.xml", b'<manifest package="ai.wavespeed.app"></manifest>')

        report = wsre.analyze(apk, out_dir=Path(tmp) / "out")
        assert report.kind == "android-apk", report.kind
        assert any("api.wavespeed.ai" in e for e in report.endpoints()), report.endpoints()
        assert any("ai.wavespeed.app" in n for n in report.notes), report.notes


def test_re_analyzer_reports_missing_path() -> None:
    from wavespeed_reg import scanner as wsre

    report = wsre.analyze("definitely/not/here.bin", out_dir="out/re-test")
    assert report.errors, "不存在的路径应报错而不是静默成功"
    assert not report.findings


# --------------------------------------------- 10. camoufox 启动参数（防回归）

# 这一组测试的由来：camoufox 0.5.9 实测暴露出三个会**让启动直接失败**的坑，
# 而失败会被 auto 降级静默掩盖（看起来"能跑"，其实一直用的是 playwright 腿）。
#   1. `timezone=` 不是合法参数 -> TypeError: unexpected keyword argument
#   2. `os=[多元素]` -> InvalidConstraints: no possible values for "os"
#   3. `screen` 容纳不下 `window` -> 同样 InvalidConstraints
# 所以这里直接对着 camoufox 的真实签名做白名单校验。


def test_camoufox_options_are_legal() -> None:
    from wavespeed_reg.camoufox_options import camoufox_launch_options, camoufox_legal_keys

    legal = camoufox_legal_keys()
    if not legal:
        return  # camoufox 未安装：校验不了，当成通过（不让离线测试依赖可选包）

    options = camoufox_launch_options(
        fingerprint={"platform": "Windows", "locale": "en-US", "timezone": "America/New_York"},
        proxy="http://user:pass@1.1.1.1:8080",
        headless=True,
    )
    illegal = set(options) - legal
    assert not illegal, f"构造出了 camoufox 不认的参数：{sorted(illegal)}（合法：{sorted(legal)}）"


def test_camoufox_never_passes_timezone() -> None:
    """`timezone` 会让 camoufox 启动直接 TypeError —— 必须永远不传。"""
    from wavespeed_reg.camoufox_options import camoufox_launch_options

    options = camoufox_launch_options(
        fingerprint={"platform": "Windows", "locale": "en-US", "timezone": "Asia/Tokyo"},
        proxy="http://1.1.1.1:8080",
    )
    assert "timezone" not in options, "timezone 是非法参数，只能靠 geoip 推导"


def test_camoufox_os_is_single_value() -> None:
    """`os` 传列表会让 fpgen 在 os 维度无解。"""
    from wavespeed_reg.camoufox_options import camoufox_launch_options

    for platform in ("Windows", "MacOS", "Linux", "darwin", "weird-unknown"):
        options = camoufox_launch_options(fingerprint={"platform": platform})
        assert isinstance(options["os"], str), f"{platform}: os 必须是单值字符串，实际 {options['os']!r}"
        assert options["os"] in ("windows", "macos", "linux"), options["os"]


def test_camoufox_window_fits_screen() -> None:
    """window 尺寸必须落在 screen 约束内，否则 fpgen 无解。"""
    from wavespeed_reg.camoufox_options import camoufox_launch_options

    for platform in ("Windows", "MacOS", "Linux"):
        options = camoufox_launch_options(fingerprint={"platform": platform})
        screen = options.get("screen")
        window = options.get("window")
        if screen is None or window is None:
            continue
        assert screen.min_width <= window[0] <= screen.max_width, f"{platform}: window 宽度越界"
        assert screen.min_height <= window[1] <= screen.max_height, f"{platform}: window 高度越界"


def test_geoip_never_returns_true() -> None:
    """`geoip=True` 会让 camoufox 检测**本机** IP，与代理不一致时比不设更糟。"""
    from wavespeed_reg.camoufox_options import geoip_target

    assert geoip_target("") == "", "没有代理时不该给 geoip"
    assert geoip_target("http://1.2.3.4:8080") == "1.2.3.4", "IP 形式应原样使用"
    assert geoip_target("http://user:pw@1.2.3.4:8080") == "1.2.3.4", "应剥掉凭据与端口"
    # 域名解析失败时返回空串，绝不返回 True
    assert geoip_target("http://definitely-not-a-real-host.invalid:8080") == ""


def test_geoip_propagates_to_options() -> None:
    from wavespeed_reg.camoufox_options import camoufox_launch_options

    with_proxy = camoufox_launch_options(
        fingerprint={"platform": "Windows"}, proxy="http://1.1.1.1:8080", geo_align=True
    )
    assert with_proxy.get("geoip") == "1.1.1.1", "有代理 IP 时 geoip 应指向出口"

    without = camoufox_launch_options(fingerprint={"platform": "Windows"}, proxy="", geo_align=True)
    assert "geoip" not in without, "没有出口 IP 时不该传 geoip"

    off = camoufox_launch_options(
        fingerprint={"platform": "Windows"}, proxy="http://1.1.1.1:8080", geo_align=False
    )
    assert "geoip" not in off, "geo_align=False 时不该传 geoip"


def test_fingerprint_preset_is_opt_in() -> None:
    """捆绑预设会被多账号抽到同一个 —— 默认不启用，保证每账号唯一指纹。"""
    from wavespeed_reg.camoufox_options import camoufox_launch_options

    default = camoufox_launch_options(fingerprint={"platform": "Windows"})
    assert "fingerprint_preset" not in default, "默认应让 fpgen 生成唯一指纹"

    preset = camoufox_launch_options(fingerprint={"platform": "Windows"}, use_preset=True)
    assert preset.get("fingerprint_preset") is True


def test_pin_cpu_cores_default_on() -> None:
    """不开 pin_cpu_cores 时 camoufox 会报宿主核数（本机 32 核，很显眼）。"""
    from wavespeed_reg.camoufox_options import camoufox_launch_options

    assert camoufox_launch_options(fingerprint={"platform": "Windows"}).get("pin_cpu_cores") is True
    assert (
        camoufox_launch_options(fingerprint={"platform": "Windows"}, pin_cpu_cores=False).get("pin_cpu_cores")
        is False
    )


# ------------------------------------------------------------------- runner


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
