"""离线测试：AMD 开发者云的 cookie 导入 / 积分 / 实例（MockTransport，不联网）。"""

from __future__ import annotations

import json
import sys
from pathlib import Path

import httpx

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parent))
sys.path.insert(0, str(HERE.parents[2] / "tools" / "linekit"))

from amd_reg import cli, site  # noqa: E402

GOOD = "session=good-cookie"
BAD = "session=expired"

ACCOUNT = {
    "nickName": "ada",
    "userId": "u-77",
    "points": 100,
}


def fake(request: httpx.Request) -> httpx.Response:
    cookie = request.headers.get("cookie", "")
    path = request.url.path
    if cookie != GOOD and not request.headers.get("authorization"):
        return httpx.Response(401, json={"code": 401, "message": "unauthorized"})
    if path.endswith(site.SUMMARY):
        return httpx.Response(200, json={"code": 0, "data": ACCOUNT})
    if path.endswith(site.POINTS):
        return httpx.Response(200, json={"code": 0, "data": [{"points": 100, "reason": "signup"}]})
    if path.endswith(site.HAS_INSTANCE):
        return httpx.Response(200, json={"code": 0, "data": False})
    if path.endswith(site.LAUNCH):
        body = json.loads(request.content or b"{}")
        return httpx.Response(200, json={"code": 0, "data": {"instanceId": "exp-1", "echo": body}})
    if path.endswith(site.STATUS):
        return httpx.Response(200, json={"code": 0, "data": {"status": "running", "gpu": "MI300X"}})
    if path.endswith(site.REMOVE):
        return httpx.Response(200, json={"code": 0, "data": True})
    return httpx.Response(404, text="not found")


def patch(monkeypatch):
    real = httpx.Client

    def patched(*args, **kwargs):
        kwargs["transport"] = httpx.MockTransport(fake)
        return real(*args, **kwargs)

    monkeypatch.setattr(httpx, "Client", patched)


def test_site_paths_match_bundle():
    assert site.API == "https://developer.amd.com.cn/api"
    assert site.SUMMARY == "/User/GetCurrentUserDataSummary"
    assert site.LAUNCH == "/Experiment/Launch"
    assert site.REMOVE == "/Experiment/Remove"
    assert site.ALIYUN_SCENE == "/Aliyun/GetEncryptedSceneId"


def test_summary_reads_points(monkeypatch):
    patch(monkeypatch)
    info = cli.summary(GOOD)
    assert info["ok"] is True
    assert info["points"] == 100
    assert info["nickname"] == "ada"
    assert info["hasInstance"] is False


def test_summary_rejects_bad_cookie(monkeypatch):
    patch(monkeypatch)
    info = cli.summary(BAD)
    assert info["ok"] is False
    assert info["status"] == 401


def test_launch_and_remove(monkeypatch):
    patch(monkeypatch)
    launched = cli.launch(GOOD, body={"gpu": "MI300X"})
    assert launched["ok"] is True
    assert launched["payload"]["data"]["instanceId"] == "exp-1"
    assert cli.remove(GOOD)["ok"] is True
    assert cli.instance_status(GOOD)["payload"]["data"]["status"] == "running"


def test_import_puts_cookie_in_pool(monkeypatch, tmp_path, capsys):
    patch(monkeypatch)
    code = cli.main(["import", "--cookie", GOOD, "--email", "a@example.com", "--output", str(tmp_path), "--json"])
    events = [json.loads(line) for line in capsys.readouterr().out.strip().split("\n") if line.strip()]
    assert any(e["type"] == "account" for e in events)
    assert any(e["type"] == "done" for e in events)
    assert code == 0

    codes = cli.main(["export", "--format", "keys", "--output", str(tmp_path), "--out", str(tmp_path / "amd.txt")])
    assert codes == 0
    assert (tmp_path / "amd.txt").read_text(encoding="utf-8").strip() == GOOD


def test_import_rejects_expired(monkeypatch, tmp_path, capsys):
    patch(monkeypatch)
    code = cli.main(["import", "--cookie", BAD, "--email", "b@example.com", "--output", str(tmp_path), "--json"])
    events = [json.loads(line) for line in capsys.readouterr().out.strip().split("\n") if line.strip()]
    assert any(e["type"] == "failed" for e in events)
    assert code == 1
