"""离线测试：验证码取值（manual / none / 缺参）+ 票据进请求。"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parent))

from linekit.captcha import manual, resolve  # noqa: E402
from linekit.errors import CaptchaRequired  # noqa: E402
from linekit.site import Site  # noqa: E402

SITE = Site(
    id="demo",
    name="Demo",
    base_url="https://demo.test",
    api_base="https://demo.test/v1",
    captcha_params=("ticket", "randstr"),
    login_url="https://demo.test/login",
)


def test_manual_strips_and_keeps_values():
    assert manual({"ticket": " t ", "randstr": " r ", "x": ""}) == {"ticket": "t", "randstr": "r"}
    assert manual(None) == {}


def test_resolve_manual_requires_every_param():
    assert resolve(SITE, provider="manual", values={"ticket": "t", "randstr": "r"}) == {
        "ticket": "t",
        "randstr": "r",
    }
    with pytest.raises(CaptchaRequired):
        resolve(SITE, provider="manual", values={"ticket": "t"})


def test_resolve_none_returns_empty():
    assert resolve(SITE, provider="none", values={"ticket": "t"}) == {}


def test_resolve_auto_prefers_given_values():
    assert resolve(SITE, provider="auto", values={"ticket": "T", "randstr": "R"}) == {
        "ticket": "T",
        "randstr": "R",
    }


def test_resolve_unknown_provider_is_typed():
    with pytest.raises(CaptchaRequired):
        resolve(SITE, provider="telepathy")


def test_site_without_captcha_needs_nothing():
    plain = Site(id="p", name="P", base_url="https://p.test", api_base="https://p.test/v1")
    assert resolve(plain, provider="auto") == {}
    assert plain.captcha_params == ()
