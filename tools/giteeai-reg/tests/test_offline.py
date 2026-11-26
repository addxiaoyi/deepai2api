"""离线测试：Gitee AI 令牌校验（MockTransport）+ 入池。"""

from __future__ import annotations

import json
import sys
from pathlib import Path

import httpx

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parent))
sys.path.insert(0, str(HERE.parents[2] / "tools" / "linekit"))

from giteeai_reg import cli  # noqa: E402
from giteeai_reg.site import API_BASE, IMAGES_PATH, MODEL, MODELS_PATH  # noqa: E402

TOKEN_OK = "a" * 32
TOKEN_BAD = "b" * 32


def fake_transport(request: httpx.Request) -> httpx.Response:
    auth = request.headers.get("authorization", "")
    if request.url.path == "/v1" + MODELS_PATH:
        if auth == f"Bearer {TOKEN_OK}":
            return httpx.Response(200, json={"data": [{"id": MODEL}, {"id": "Qwen-Image-Edit"}]})
        return httpx.Response(401, json={"error": {"message": "invalid token"}})
    if request.url.path == "/v1" + IMAGES_PATH:
        return httpx.Response(200, json={"data": [{"url": "https://example.test/x.png"}]})
    return httpx.Response(404, text="not found")


def test_site_facts():
    assert API_BASE == "https://ai.gitee.com/v1"
    assert MODELS_PATH == "/models"
    assert IMAGES_PATH == "/images/generations"
    assert MODEL == "z-image-turbo"


def test_verify_accepts_good_token(monkeypatch):
    real = httpx.Client

    def patched(*args, **kwargs):
        kwargs["transport"] = httpx.MockTransport(fake_transport)
        return real(*args, **kwargs)

    monkeypatch.setattr(httpx, "Client", patched)
    result = cli.verify(TOKEN_OK)
    assert result["ok"] is True
    assert MODEL in result["models"]


def test_verify_rejects_bad_token(monkeypatch):
    real = httpx.Client

    def patched(*args, **kwargs):
        kwargs["transport"] = httpx.MockTransport(fake_transport)
        return real(*args, **kwargs)

    monkeypatch.setattr(httpx, "Client", patched)
    result = cli.verify(TOKEN_BAD)
    assert result["ok"] is False
    assert result["status"] == 401


def test_run_imports_only_the_good_token(tmp_path, monkeypatch, capsys):
    real = httpx.Client

    def patched(*args, **kwargs):
        kwargs["transport"] = httpx.MockTransport(fake_transport)
        return real(*args, **kwargs)

    monkeypatch.setattr(httpx, "Client", patched)
    tokens = tmp_path / "tokens.txt"
    tokens.write_text(f"{TOKEN_OK}\n{TOKEN_BAD}\n# comment\n", encoding="utf-8")

    code = cli.main(["run", "--tokens-file", str(tokens), "--output", str(tmp_path), "--json"])
    out = capsys.readouterr().out.strip().split("\n")
    events = [json.loads(line) for line in out]
    assert any(e["type"] == "account" for e in events)
    assert any(e["type"] == "failed" for e in events)
    assert any(e["type"] == "done" for e in events)
    assert code == 0

    keys = cli.main(["keys", "--output", str(tmp_path)])
    assert keys == 0
    assert TOKEN_OK in capsys.readouterr().out
