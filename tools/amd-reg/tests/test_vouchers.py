"""离线测试：算力券兑换 / 免费模型探测 / 多号汇总到主号。"""

from __future__ import annotations

import json
import sys
from pathlib import Path

import httpx

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parent))
sys.path.insert(0, str(HERE.parents[2] / "tools" / "linekit"))

from amd_reg import cli, vouchers  # noqa: E402

COOKIE_A = "SESSION=a"
COOKIE_B = "SESSION=b"

CONFIG = {
    "code": 0,
    "data": {
        "Points": 500,
        "Hours": 25,
        "Limit": 2,
        "NeedPhone": False,
        "NeedEmail": True,
    },
}

RECORDS = {
    "SESSION=a": [
        {"Id": "r1", "VoucherCode": "AMD-AAAA-1111", "Hours": 25, "Points": 500, "Status": 1, "CreateTime": "2026-11-20"},
    ],
    "SESSION=b": [
        {"Id": "r2", "VoucherCode": "AMD-BBBB-2222", "Hours": 10, "Points": 200, "Status": 1, "CreateTime": "2026-11-21"},
        {"Id": "r3", "VoucherCode": "AMD-CCCC-3333", "Hours": 5, "Points": 100, "Status": 1, "CreateTime": "2026-11-22"},
    ],
}


class VoucherBackend:
    def __init__(self):
        self.exchanged: list[dict] = []
        self.calls: list[str] = []

    def handler(self, request: httpx.Request) -> httpx.Response:
        cookie = request.headers.get("cookie", "")
        path = request.url.path
        self.calls.append(path)

        if path.endswith("/User/GetCurrentUserDataSummary"):
            points = 900 if cookie == COOKIE_A else 300
            return httpx.Response(200, json={"code": 0, "data": {"nickName": cookie[-1], "points": points}})
        if path.endswith("/User/GetLabVoucherExchangeConfig"):
            return httpx.Response(200, json=CONFIG)
        if path.endswith("/User/GetLabVoucherExchangeRecords"):
            body = json.loads(request.content or b"{}")
            assert "PageIndex" in body and "PageSize" in body and "Target" in body, body
            return httpx.Response(200, json={"code": 0, "data": {"list": RECORDS.get(cookie, [])}})
        if path.endswith("/User/GetCreativeFactoryExchangeStatus"):
            return httpx.Response(200, json={"code": 0, "data": {"CanExchange": False}})
        if path.endswith("/User/ExchangeLabVoucher"):
            body = json.loads(request.content or b"{}")
            self.exchanged.append({"cookie": cookie, "body": body})
            return httpx.Response(200, json={"code": 0, "data": {"VoucherCode": "AMD-NEW-9999"}})
        if path.endswith("/TokenFactory/GetInfo"):
            return httpx.Response(200, json={"code": 404})
        if path.endswith("/User/GetTokenFactoryInfo"):
            return httpx.Response(
                200,
                json={
                    "code": 0,
                    "data": {
                        "Title": "Token Factory",
                        "Access": {"BaseUrl": {"Value": "https://llm.amd.example/v1", "Copyable": True}},
                        "Pricing": {"Items": [{"Key": "input", "Label": "输入", "Value": "free"}]},
                    },
                },
            )
        if path.endswith("/tokenfactory"):
            return httpx.Response(200, text="<html>no url here</html>")
        return httpx.Response(404, text="not found")


def patch(monkeypatch, backend):
    real = httpx.Client

    def patched(*args, **kwargs):
        kwargs["transport"] = httpx.MockTransport(backend.handler)
        return real(*args, **kwargs)

    monkeypatch.setattr(httpx.Client, "__new__", httpx.Client.__new__)  # no-op guard
    monkeypatch.setattr(httpx, "Client", patched)





def test_config_and_targets(monkeypatch):
    backend = VoucherBackend()
    patch(monkeypatch, backend)
    config = vouchers.voucher_config(COOKIE_A)
    targets = vouchers.select_exchange_targets(config)
    assert targets["cost"] == 500
    assert targets["hours"] == 25
    assert targets["limit"] == 2
    assert targets["needEmail"] is True


def test_records_normalize_voucher_codes(monkeypatch):
    backend = VoucherBackend()
    patch(monkeypatch, backend)
    rows = vouchers.voucher_records(COOKIE_B)
    assert [row["voucher"] for row in rows] == ["AMD-BBBB-2222", "AMD-CCCC-3333"]
    assert rows[0]["hours"] == 10


def test_account_vouchers_sums_hours(monkeypatch):
    backend = VoucherBackend()
    patch(monkeypatch, backend)
    info = vouchers.account_vouchers(COOKIE_B)
    assert info["hours"] == 15.0
    assert info["newest"]["voucher"] == "AMD-BBBB-2222"


def test_exchange_voucher_sends_amount(monkeypatch):
    backend = VoucherBackend()
    patch(monkeypatch, backend)
    vouchers.exchange_voucher(COOKIE_A, amount=2)
    body = backend.exchanged[0]["body"]
    assert body["Amount"] == 2 and body["Count"] == 2


def test_token_factory_probes_candidates_and_caches(monkeypatch, tmp_path):
    backend = VoucherBackend()
    patch(monkeypatch, backend)
    cache = tmp_path / "tf.json"
    info = vouchers.token_factory(COOKIE_A, cache_path=cache)
    assert info["baseUrl"] == "https://llm.amd.example/v1"
    assert info["from"].endswith("/User/GetTokenFactoryInfo")
    assert info["items"][0]["key"] == "input"
    assert cache.exists()

    again = vouchers.token_factory(COOKIE_A, cache_path=cache)
    assert again["from"] == "cache"


def test_build_ledger_aggregates(monkeypatch):
    backend = VoucherBackend()
    patch(monkeypatch, backend)
    rows = [
        {"email": "a@x", "ok": True, **vouchers.account_vouchers(COOKIE_A)},
        {"email": "b@x", "ok": True, **vouchers.account_vouchers(COOKIE_B)},
    ]
    ledger = vouchers.build_ledger(rows)
    assert ledger["totalHours"] == 40.0
    assert ledger["totalVouchers"] == 3
    codes = [code for row in ledger["accounts"] for code in row["codes"]]
    assert "AMD-AAAA-1111" in codes and "AMD-CCCC-3333" in codes


def test_consolidate_writes_ledger_and_uses_master(monkeypatch, tmp_path, capsys):
    backend = VoucherBackend()
    patch(monkeypatch, backend)

    # 通过 import 把两个号写进 worker 的库
    cli.main(["import", "--cookie", COOKIE_A, "--email", "a@example.com", "--output", str(tmp_path), "--json"])
    cli.main(["import", "--cookie", COOKIE_B, "--email", "b@example.com", "--output", str(tmp_path), "--json"])
    capsys.readouterr()

    code = cli.main(
        ["consolidate", "--output", str(tmp_path), "--master", "a@example.com", "--redeem", "--amount", "1", "--json"]
    )
    events = [json.loads(line) for line in capsys.readouterr().out.strip().split("\n") if line.strip()]
    done = [e for e in events if e["type"] == "done"][0]["summary"]
    assert code == 0
    assert done["master"] == "a@example.com"
    assert done["totalHours"] == 40.0
    assert done["redeemed"]["code"] == 0
    # 兑换用的是主号 cookie
    assert backend.exchanged[-1]["cookie"] == COOKIE_A
    ledger = json.loads((tmp_path / "vouchers.json").read_text(encoding="utf-8"))
    assert ledger["totalVouchers"] == 3


def test_vouchers_cli_prints_ledger(monkeypatch, tmp_path, capsys):
    backend = VoucherBackend()
    patch(monkeypatch, backend)
    cli.main(["import", "--cookie", COOKIE_A, "--email", "a@example.com", "--output", str(tmp_path), "--json"])
    capsys.readouterr()
    code = cli.main(["vouchers", "--output", str(tmp_path), "--json"])
    events = [json.loads(line) for line in capsys.readouterr().out.strip().split("\n") if line.strip()]
    assert code == 0
    assert any(e["type"] == "voucher" and e["hours"] == 25.0 for e in events)
