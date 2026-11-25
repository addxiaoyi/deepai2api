"""SMS leg. Poe wants a phone number after the email is verified.

`provider: off` (default) is a legitimate mode: the account is created and stored
as `phone_required`, and an operator can attach SMS later. Providers below are
HTTP contracts, not promises — each vendor rotates its API, so every call keeps
the raw response in the exception detail for debugging.
"""

from __future__ import annotations

import time
from dataclasses import dataclass
from typing import Any

import httpx

from .errors import SmsFailed
from .net import build_client, request

SMS_ACTIVATE = "https://api.sms-activate.org/stub/handler"
SMSPOOL = "https://api.smspool.net"


@dataclass(slots=True)
class Rental:
    phone: str
    activation_id: str
    provider: str


class SmsProvider:
    name = "off"

    def rent(self) -> Rental:  # pragma: no cover - interface
        raise SmsFailed("sms provider is off")

    def wait_code(self, rental: Rental, timeout: float = 180.0, poll: float = 6.0) -> str:
        raise SmsFailed("sms provider is off")

    def release(self, rental: Rental) -> None:
        return None

    def finish(self, rental: Rental) -> None:
        return None


class ManualSms(SmsProvider):
    """Operator reads the SMS and types the code into the running console."""

    name = "manual"

    def rent(self) -> Rental:
        phone = input("Poe 要手机号：请输入带国家码的号码（如 +14155550123）: ").strip()
        if not phone.startswith("+"):
            phone = "+" + phone.lstrip("+")
        return Rental(phone=phone, activation_id="manual", provider=self.name)

    def wait_code(self, rental: Rental, timeout: float = 180.0, poll: float = 6.0) -> str:
        code = input("收到短信后输入验证码: ").strip()
        if not code:
            raise SmsFailed("no code entered")
        return code


class SmsActivate(SmsProvider):
    """sms-activate.org classic handler (`action=getNumber|getStatus|setStatus`)."""

    name = "sms_activate"

    def __init__(self, cfg, *, proxy: str = "") -> None:
        self.cfg = cfg
        self.client = build_client(proxy=proxy, timeout=40)

    def _call(self, **params: Any) -> str:
        params.setdefault("api_key", self.cfg.api_key)
        response = request(self.client, "GET", SMS_ACTIVATE, params=params)
        text = (response.text or "").strip()
        if text.startswith("ACCESS_") or text.startswith("BAD_") or text.startswith("NO_"):
            raise SmsFailed(f"sms-activate: {text}", detail=text)
        return text

    def rent(self) -> Rental:
        if not self.cfg.api_key:
            raise SmsFailed("sms provider needs an api key")
        raw = self._call(
            action="getNumber",
            service=self.cfg.service,
            country=self.cfg.country,
            maxPrice=str(self.cfg.max_price),
        )
        if ":" not in raw:
            raise SmsFailed(f"sms-activate returned {raw!r}")
        activation_id, phone = raw.split(":", 1)
        return Rental(phone="+" + phone.strip(), activation_id=activation_id.strip(), provider=self.name)

    def wait_code(self, rental: Rental, timeout: float = 180.0, poll: float = 6.0) -> str:
        deadline = time.time() + timeout
        while time.time() < deadline:
            status = self._call(action="getStatus", id=rental.activation_id)
            if status.startswith("STATUS_OK"):
                return status.split(":", 1)[1].strip()
            if status == "STATUS_CANCEL":
                raise SmsFailed("sms-activate cancelled the rental")
            time.sleep(poll)
        raise SmsFailed("sms code timed out")

    def finish(self, rental: Rental) -> None:
        try:
            self._call(action="setStatus", status="6", id=rental.activation_id)
        except SmsFailed:
            pass

    def release(self, rental: Rental) -> None:
        try:
            self._call(action="setStatus", status="8", id=rental.activation_id)
        except SmsFailed:
            pass

    def close(self) -> None:
        self.client.close()


class SmsPool(SmsProvider):
    """smspool.net purchase/check contract."""

    name = "smspool"

    def __init__(self, cfg, *, proxy: str = "") -> None:
        self.cfg = cfg
        self.client = build_client(proxy=proxy, timeout=40)

    def rent(self) -> Rental:
        if not self.cfg.api_key:
            raise SmsFailed("sms provider needs an api key")
        payload = {
            "key": self.cfg.api_key,
            "service": self.cfg.service,
            "country": self.cfg.country,
            "pricing_option": 0,
        }
        response = request(self.client, "POST", f"{SMSPOOL}/purchase/phone", data=payload)
        try:
            body = response.json()
        except Exception as exc:  # noqa: BLE001
            raise SmsFailed(f"smspool: {response.text[:200]}") from exc
        if not body.get("success"):
            raise SmsFailed(f"smspool refused: {body}")
        return Rental(
            phone="+" + str(body.get("number")),
            activation_id=str(body.get("order_id")),
            provider=self.name,
        )

    def wait_code(self, rental: Rental, timeout: float = 180.0, poll: float = 6.0) -> str:
        deadline = time.time() + timeout
        while time.time() < deadline:
            response = request(
                self.client,
                "POST",
                f"{SMSPOOL}/sms/check",
                data={"key": self.cfg.api_key, "orderid": rental.activation_id},
            )
            body = response.json() if response.headers.get("content-type", "").startswith("application/json") else {}
            if body.get("sms"):
                return str(body["sms"])
            if body.get("status") == 3:
                raise SmsFailed("smspool cancelled the order")
            time.sleep(poll)
        raise SmsFailed("sms code timed out")

    def close(self) -> None:
        self.client.close()


def build_provider(cfg, *, proxy: str = "") -> SmsProvider:
    name = (cfg.provider or "off").lower()
    if name in ("", "off", "none"):
        return SmsProvider()
    if name == "manual":
        return ManualSms()
    if name == "sms_activate":
        return SmsActivate(cfg, proxy=proxy)
    if name == "smspool":
        return SmsPool(cfg, proxy=proxy)
    raise SmsFailed(f"unknown sms provider {cfg.provider!r}")
