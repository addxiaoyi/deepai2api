"""Engine routing: protocol first, fingerprint browser as the fallback.

No browser and no network here — the session and browser are faked, so this
pins down only the policy: which engine ran, what got stored, which error escapes.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from allmodels_reg.config import Config  # noqa: E402
from allmodels_reg.errors import CaptchaRequired, SignupError  # noqa: E402
from allmodels_reg.runner import Runner  # noqa: E402
from allmodels_reg.store import Store  # noqa: E402

ACCOUNT = {
    "email": "",
    "password": "pw-123456",
    "name": "Test User",
    "api_key": "sk_live_engine_test_0123456789",
    "api_key_source": "signup-key",
    "key_prefix": "sk_live_",
    "key_last4": "6789",
    "project_id": "proj1",
    "guardrail_id": "gr1",
    "email_verified": True,
    "cookies": [],
}


class FakeProvider:
    name = "fixed"

    def allocate(self, local_part: str = "", domain: str = ""):
        class Box:
            address = f"{local_part or 'user'}@example.com"
            provider = "fixed"
            token = ""

        return Box()

    def domains(self) -> list[str]:
        return ["example.com"]

    def messages(self, box):  # pragma: no cover - unused when verify_email=False
        return []

    def wait_for_link(self, *args, **kwargs) -> str:
        return ""

    def close(self) -> None:
        return None


class ProtocolBlocked:
    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def signup_flow(self, email, password, **kwargs):
        raise CaptchaRequired("turnstile rejected the token")


class ProtocolOk:
    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def signup_flow(self, email, password, **kwargs):
        return {**ACCOUNT, "email": email, "password": password}


class FakeBrowser:
    def __init__(self, **kwargs):
        self.kwargs = kwargs

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def signup_flow(self, email, password, **kwargs):
        return {
            **ACCOUNT,
            "email": email,
            "password": password,
            "browser": self.kwargs.get("stack", "auto"),
        }


def make_runner(tmp_path, *, engine, session_factory, browser_factory=None):
    cfg = Config.load(
        None,
        {
            "engine": engine,
            "attempts": 1,
            "retry_rounds": 0,
            "verify_email": False,
            "count": 1,
            "delay": (0, 0),
        },
    )
    store = Store(tmp_path / "accounts.sqlite3")
    runner = Runner(
        cfg,
        store,
        quiet=True,
        mail_factory=lambda _cfg, _proxy: FakeProvider(),
        captcha_solver=lambda **_: "",
        session_factory=session_factory,
        browser_factory=browser_factory or FakeBrowser,
    )
    return runner, store


def test_auto_falls_back_to_the_fingerprint_browser(tmp_path):
    events: list[dict] = []
    runner, store = make_runner(tmp_path, engine="auto", session_factory=lambda **_: ProtocolBlocked())
    runner.on_event = events.append

    account = runner.signup(0)

    assert account["api_key"] == ACCOUNT["api_key"]
    rows = store.rows()
    assert len(rows) == 1
    assert rows[0]["engine"] == "browser"
    assert [e["type"] for e in events].count("account") == 1
    assert next(e for e in events if e["type"] == "account")["engine"] == "browser"
    store.close()


def test_browser_engine_never_tries_the_protocol(tmp_path):
    def boom(**_kwargs):  # pragma: no cover - must not be called
        raise AssertionError("protocol engine must not run when engine=browser")

    runner, store = make_runner(tmp_path, engine="browser", session_factory=boom)
    account = runner.signup(0)
    assert account["browser"] == "auto"
    assert store.rows()[0]["engine"] == "browser"
    store.close()


def test_protocol_engine_propagates_the_block(tmp_path):
    runner, store = make_runner(tmp_path, engine="protocol", session_factory=lambda **_: ProtocolBlocked())
    with pytest.raises(CaptchaRequired):
        runner.signup(0)
    assert store.count() == 0
    store.close()


def test_auto_keeps_protocol_when_it_works(tmp_path):
    runner, store = make_runner(tmp_path, engine="auto", session_factory=lambda **_: ProtocolOk())
    runner.signup(0)
    assert store.rows()[0]["engine"] == "protocol"
    store.close()


def test_browser_events_carry_the_engine(tmp_path):
    events: list[dict] = []
    runner, store = make_runner(tmp_path, engine="browser", session_factory=lambda **_: ProtocolOk())
    runner.on_event = events.append
    try:
        runner.run(1)
    finally:
        store.close()
    start = next(e for e in events if e["type"] == "start")
    assert start["engine"] == "browser"
