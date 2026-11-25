"""Protocol implementation for gab.ai signup/login and API key minting.

Reverse notes (verified by live probes in this workspace):

Signup (public):
  POST https://gab.ai/json/auth/signup/email
    body: { termsVersion, email, password, turnstileToken, referralCode?, clientInfo? }
  -> { error:false, user:{...}, tokens:{access,refresh,expiresAt} }

Login (public):
  POST https://gab.ai/json/auth/login/email
    body: { email, password, turnstileToken?, clientInfo? }

API keys (requires bearer access token):
  GET  https://gab.ai/json/api-keys
  POST https://gab.ai/json/api-keys   body: { name? }
  DEL  https://gab.ai/json/api-keys/:id

Email verify (optional):
  POST https://gab.ai/json/auth/verify-email   body:{token}
  POST https://gab.ai/json/account/resend-verify-email

Developer API requires Plus; the created `gab_…` key will answer 403 plus_required.
"""

from __future__ import annotations

import json
import re
import time
from typing import Any, Callable

import httpx

from .errors import (
    Blocked,
    CaptchaRequired,
    EmailRejected,
    EmailTaken,
    NoApiKey,
    PlusRequired,
    RateLimited,
    SignupError,
    VerificationPending,
)
from .net import build_client, request

BASE = "https://gab.ai"
TURNSTILE_SITE_KEY = "0x4AAAAAAAQeXKSGTzxYAKT8"
TURNSTILE_ACTION = "signup"
TERMS_VERSION = "2026-11-09"

SIGNUP = "/json/auth/signup/email"
LOGIN = "/json/auth/login/email"
VERIFY_EMAIL = "/json/auth/verify-email"
RESEND_VERIFY = "/json/account/resend-verify-email"

API_KEYS = "/json/api-keys"

PLUS_REQUIRED_CODE = "plus_required"

_CF_MARKERS = ("just a moment", "cf-browser-verification", "challenge-platform", "attention required")


def _looks_blocked(response: httpx.Response) -> bool:
    if response.status_code in (403, 503) and "cloudflare" in response.headers.get("server", "").lower():
        body = (response.text or "")[:2000].lower()
        if any(marker in body for marker in _CF_MARKERS):
            return True
    body = (response.text or "")[:4000].lower()
    return any(marker in body for marker in _CF_MARKERS)


def _error_payload(response: httpx.Response) -> tuple[bool, str, Any]:
    try:
        payload = response.json()
    except (json.JSONDecodeError, ValueError):
        return False, (response.text or "")[:300], None
    if isinstance(payload, dict):
        err = bool(payload.get("error") is True)
        msg = str(payload.get("message") or payload.get("error_description") or payload.get("error") or "")
        return err, msg, payload
    return False, str(payload)[:300], payload


def _raise_for(path: str, response: httpx.Response) -> None:
    status = response.status_code
    err, message, payload = _error_payload(response)
    lowered = f"{message}".lower()

    detail = {"status": status, "message": message[:400], "payload": payload if isinstance(payload, (dict, list)) else None}

    if _looks_blocked(response):
        raise Blocked(f"{path}: edge answered with a bot challenge", detail=detail)
    if status == 429:
        raise RateLimited(f"{path}: rate limited", detail=detail)

    # explicit API errors
    if "captcha" in lowered or "turnstile" in lowered:
        raise CaptchaRequired(f"{path}: turnstile rejected the token ({message})", detail=detail)

    if "already exists" in lowered or "already" in lowered and "account" in lowered:
        raise EmailTaken(f"{path}: address already registered", detail=detail)

    if any(k in lowered for k in ("invalid email", "email not allowed", "disposable")):
        raise EmailRejected(f"{path}: address rejected ({message})", detail=detail)

    # verify token failures
    if path == VERIFY_EMAIL and ("invalid" in lowered or "expired" in lowered):
        raise VerificationPending(f"{path}: verification token invalid ({message})", detail=detail)

    if status in (401, 403):
        raise SignupError(f"{path}: not authorised (HTTP {status})", detail=detail)

    raise SignupError(f"{path}: failed (HTTP {status})", detail=detail)


def _json_body(response: httpx.Response, path: str) -> Any:
    """Parse a JSON body, turning an HTML wall into a typed `Blocked`.

    Cloudflare answers `200 text/html "Just a moment..."` often enough that a raw
    `response.json()` would blow up with a JSONDecodeError and the runner would
    classify it as an unknown error instead of rotating the proxy.
    """
    try:
        return response.json() if response.content else {}
    except (json.JSONDecodeError, ValueError) as exc:
        if _looks_blocked(response):
            raise Blocked(f"{path}: edge answered with a bot challenge", detail=(response.text or "")[:300]) from exc
        raise SignupError(f"{path}: response is not JSON", detail=(response.text or "")[:300]) from exc


class GabSession:
    """One signup/login session; bearer is stored in-memory."""

    def __init__(
        self,
        *,
        base_url: str = BASE,
        proxy: str = "",
        timeout: float = 45.0,
        language: str = "en-US",
        user_agent: str = "",
        transport: Any = None,
    ) -> None:
        self.base = (base_url or BASE).rstrip("/")
        self.proxy = proxy
        self.client = build_client(proxy=proxy, timeout=timeout, user_agent=user_agent, language=language)
        if transport is not None:
            self.client._transport = transport  # test seam
        self.client.headers.update({"Origin": self.base, "Referer": f"{self.base}/signup", "X-Platform": "web"})
        self.access_token = ""
        self.refresh_token = ""
        self.expires_at = ""

    def __enter__(self) -> "GabSession":
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()

    def close(self) -> None:
        self.client.close()

    def signup(self, *, email: str, password: str, turnstile: str = "", referral_code: str = "", client_info: dict | None = None) -> dict[str, Any]:
        body: dict[str, Any] = {
            "termsVersion": TERMS_VERSION,
            "email": email,
            "password": password,
            "turnstileToken": turnstile,
        }
        if referral_code:
            body["referralCode"] = referral_code
        if client_info:
            body["clientInfo"] = client_info
        response = request(self.client, "POST", f"{self.base}{SIGNUP}", json=body, headers={"Content-Type": "application/json"})
        if _looks_blocked(response):
            raise Blocked(f"{SIGNUP}: edge answered with a bot challenge", detail=(response.text or "")[:300])
        if response.status_code >= 400:
            _raise_for(SIGNUP, response)
        payload = _json_body(response, SIGNUP)
        if isinstance(payload, dict) and payload.get("error") is True:
            raise SignupError(payload.get("message") or "signup failed", detail=payload)
        self._capture_tokens(payload)
        return payload if isinstance(payload, dict) else {}

    def login(self, *, email: str, password: str, turnstile: str = "", client_info: dict | None = None) -> dict[str, Any]:
        body: dict[str, Any] = {"email": email, "password": password, "turnstileToken": turnstile}
        if client_info:
            body["clientInfo"] = client_info
        response = request(self.client, "POST", f"{self.base}{LOGIN}", json=body, headers={"Content-Type": "application/json"})
        if _looks_blocked(response):
            raise Blocked(f"{LOGIN}: edge answered with a bot challenge", detail=(response.text or "")[:300])
        if response.status_code >= 400:
            _raise_for(LOGIN, response)
        payload = _json_body(response, LOGIN)
        if isinstance(payload, dict) and payload.get("error") is True:
            raise SignupError(payload.get("message") or "login failed", detail=payload)
        self._capture_tokens(payload)
        return payload if isinstance(payload, dict) else {}

    def _capture_tokens(self, payload: Any) -> None:
        if not isinstance(payload, dict):
            return
        tokens = payload.get("tokens") or {}
        if isinstance(tokens, dict):
            self.access_token = str(tokens.get("access") or "")
            self.refresh_token = str(tokens.get("refresh") or "")
            self.expires_at = str(tokens.get("expiresAt") or "")

    def bearer_headers(self) -> dict[str, str]:
        if not self.access_token:
            return {}
        return {"Authorization": f"Bearer {self.access_token}", "X-Platform": "web"}

    def api_keys_list(self) -> list[dict[str, Any]]:
        response = request(self.client, "GET", f"{self.base}{API_KEYS}", headers={"Accept": "application/json", **self.bearer_headers()})
        if response.status_code >= 400:
            _raise_for(API_KEYS, response)
        payload = response.json() if response.content else {}
        if isinstance(payload, dict) and payload.get("error") is True:
            raise SignupError(payload.get("message") or "api keys list failed", detail=payload)
        keys = payload.get("apiKeys") if isinstance(payload, dict) else None
        return keys if isinstance(keys, list) else []

    def api_key_create(self, name: str = "") -> dict[str, Any]:
        body: dict[str, Any] = {}
        if name:
            body["name"] = name
        response = request(self.client, "POST", f"{self.base}{API_KEYS}", json=body, headers={"Content-Type": "application/json", **self.bearer_headers()})
        if response.status_code >= 400:
            _raise_for(API_KEYS, response)
        payload = response.json() if response.content else {}
        if isinstance(payload, dict) and payload.get("error") is True:
            raise SignupError(payload.get("message") or "api key create failed", detail=payload)
        api_key = (payload.get("apiKey") or {}).get("key") if isinstance(payload, dict) else ""
        if not isinstance(api_key, str) or not api_key.startswith("gab_"):
            raise NoApiKey("dashboard did not return an apiKey", detail=payload)
        return payload

    def verify_email_token(self, token: str) -> dict[str, Any]:
        response = request(self.client, "POST", f"{self.base}{VERIFY_EMAIL}", json={"token": token}, headers={"Content-Type": "application/json"})
        if response.status_code >= 400:
            _raise_for(VERIFY_EMAIL, response)
        payload = response.json() if response.content else {}
        return payload if isinstance(payload, dict) else {}

    def resend_verification_email(self) -> dict[str, Any]:
        response = request(self.client, "POST", f"{self.base}{RESEND_VERIFY}", headers={"Accept": "application/json", **self.bearer_headers()})
        if response.status_code >= 400:
            _raise_for(RESEND_VERIFY, response)
        payload = response.json() if response.content else {}
        return payload if isinstance(payload, dict) else {}

    def developer_api_check(self, api_key: str) -> None:
        """Hit /v1/chat/completions to see if this key is usable (Plus gate)."""
        if not api_key:
            raise NoApiKey("empty api_key")
        body = {"model": "arya", "messages": [{"role": "user", "content": "ping"}], "max_tokens": 1, "stream": False}
        response = request(
            self.client,
            "POST",
            f"{self.base}/v1/chat/completions",
            json=body,
            headers={"Content-Type": "application/json", "Authorization": f"Bearer {api_key}"},
        )
        if response.status_code == 403:
            # structured OpenAI error: {error:{code:plus_required}}
            try:
                data = response.json()
                code = ((data or {}).get("error") or {}).get("code")
                if str(code) == PLUS_REQUIRED_CODE:
                    raise PlusRequired("developer api requires Plus", detail=data)
            except (ValueError, json.JSONDecodeError):
                pass
        if response.status_code >= 400:
            # keep as generic so runner can mark it and continue
            raise SignupError(f"developer api check failed (HTTP {response.status_code})", detail=(response.text or "")[:300])


def extract_verify_link(messages: list[Any]) -> str:
    """Helper: find /verify-email?token=... in a list of Message objects."""
    blob = "\n".join(getattr(m, "body", "") + "\n" + getattr(m, "html", "") for m in messages)
    m = re.search(r"https?://[^\s\"'<>]*?/verify-email\?token=[A-Za-z0-9._-]+", blob)
    return m.group(0) if m else ""
