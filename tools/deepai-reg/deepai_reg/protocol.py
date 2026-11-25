"""DeepAI signup protocol, reconstructed from the site's own inline JS.

The signup dialog on https://deepai.org/ posts to `api.deepai.org`:

    POST /get_user_login_type          form: email           -> {user_exists, ...}
    POST /save_custom_signup_data      json: referrer/...    -> 200 (soft, ignorable)
    POST /daily-time-sync/registration/ form: email, username, password1, password2
                                       -> 201 {"key": "<token>"} + sessionid cookie
    GET  /daily-time-sync/user/        -> user object, api_key under userprofile

That last call is the interesting one: a fresh account exposes its DeepAI API key
immediately, so no desktop-auth round trip is required. The PKCE desktop flow is
kept as a fallback for accounts where the profile hides the key.
"""

from __future__ import annotations

import base64
import hashlib
import json
import re
import secrets
from typing import Any

import httpx

from .errors import (
    Blocked,
    EmailRejected,
    EmailTaken,
    NoApiKey,
    PasswordRejected,
    RateLimited,
    SignupError,
)
from .net import build_client, firefox_user_agent, request

ORIGIN = "https://deepai.org"
API = "https://api.deepai.org"

LOOKUP = f"{API}/get_user_login_type"
SIGNUP_DATA = f"{API}/save_custom_signup_data"
REGISTER = f"{API}/daily-time-sync/registration/"
PROFILE = f"{API}/daily-time-sync/user/"
LOGIN = f"{API}/daily-time-sync/login/"
DESKTOP_AUTH = f"{ORIGIN}/deepai_code/desktop_auth/"
EXCHANGE = f"{ORIGIN}/deepai_code/desktop_auth/exchange/"

_CF_MARKERS = ("just a moment", "cf-browser-verification", "challenge-platform", "attention required")
_EMAIL_REJECTS = (
    "use your own email",
    "disposable",
    "valid email",
    "not a valid",
    "temporary email",
)
_EMAIL_TAKEN = ("already exists", "already registered", "already in use")
_PASSWORD_REJECTS = ("password", "too common", "too short", "too similar")


def _b64url(raw: bytes) -> str:
    return base64.urlsafe_b64encode(raw).rstrip(b"=").decode()


def _first_error(payload: Any) -> str:
    """DRF error bodies are {"field": ["message", ...]} — flatten to one line."""
    if isinstance(payload, dict):
        for value in payload.values():
            if isinstance(value, list) and value:
                return str(value[0])
            if isinstance(value, str):
                return value
    if isinstance(payload, list) and payload:
        return str(payload[0])
    return ""


def _looks_blocked(response: httpx.Response) -> bool:
    if response.status_code in (403, 503) and response.headers.get("server", "").lower() == "cloudflare":
        return True
    if response.status_code == 102:
        return True
    body = (response.text or "")[:4000].lower()
    return any(marker in body for marker in _CF_MARKERS)


class DeepAI:
    """One signup session = one cookie jar = one account."""

    def __init__(
        self,
        *,
        proxy: str = "",
        timeout: float = 45.0,
        language: str = "en-US",
        user_agent: str = "",
    ) -> None:
        self.proxy = proxy
        self.language = language
        self.user_agent = user_agent or firefox_user_agent()
        self.client = build_client(
            proxy=proxy, timeout=timeout, user_agent=self.user_agent, language=language
        )
        self.client.headers.update({"Origin": ORIGIN, "Referer": f"{ORIGIN}/"})

    # ------------------------------------------------------------------ basic
    def __enter__(self) -> "DeepAI":
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()

    def close(self) -> None:
        self.client.close()

    def warm(self) -> None:
        """Pick up csrftoken/sessionid the way a first page load would."""
        response = request(self.client, "GET", f"{ORIGIN}/")
        if _looks_blocked(response):
            raise Blocked("DeepAI edge answered the homepage with a bot challenge")

    def _csrf_headers(self) -> dict[str, str]:
        token = self.client.cookies.get("csrftoken", "")
        headers = {
            "X-CSRFToken": token,
            "X-Requested-With": "XMLHttpRequest",
            "Accept": "application/json, text/plain, */*",
        }
        return headers

    # ---------------------------------------------------------------- lookups
    def lookup(self, email: str) -> dict[str, Any]:
        response = request(
            self.client,
            "POST",
            LOOKUP,
            data={"email": email},
            headers=self._csrf_headers(),
        )
        if _looks_blocked(response):
            raise Blocked("login-type lookup hit a bot challenge")
        if response.status_code == 429:
            raise RateLimited("login-type lookup rate limited", detail=response.text[:300])
        if response.status_code >= 400:
            raise SignupError(
                f"login-type lookup failed (HTTP {response.status_code})",
                detail=response.text[:300],
            )
        try:
            return response.json()
        except json.JSONDecodeError as exc:
            raise SignupError("login-type lookup returned non-JSON") from exc

    # --------------------------------------------------------------- register
    def save_signup_context(self, username: str) -> None:
        """Optional attribution blob the real dialog sends; failures are cosmetic."""
        payload = {
            "referrer": "[][]",
            "signup_page": "/",
            "desired_username": username,
            "language": self.language,
        }
        try:
            request(
                self.client,
                "POST",
                SIGNUP_DATA,
                json=payload,
                headers=self._csrf_headers(),
            )
        except SignupError:
            pass

    def register(self, email: str, password: str, *, username: str | None = None) -> dict[str, Any]:
        form = {
            "email": email,
            "username": username or email,
            "password1": password,
            "password2": password,
        }
        response = request(
            self.client,
            "POST",
            REGISTER,
            data=form,
            headers=self._csrf_headers(),
        )
        if _looks_blocked(response):
            raise Blocked("registration hit a bot challenge")
        if response.status_code == 429:
            raise RateLimited("registration rate limited", detail=response.text[:300])
        if response.status_code in (200, 201):
            body: dict[str, Any] = {}
            try:
                body = response.json()
            except json.JSONDecodeError:
                pass
            body.setdefault("status", response.status_code)
            return body

        detail = ""
        try:
            payload = response.json()
            detail = _first_error(payload)
        except json.JSONDecodeError:
            detail = (response.text or "")[:300]

        lowered = detail.lower()
        if any(marker in lowered for marker in _EMAIL_REJECTS):
            raise EmailRejected(detail or "address rejected", detail=detail)
        if any(marker in lowered for marker in _EMAIL_TAKEN):
            raise EmailTaken(detail or "address already registered", detail=detail)
        if any(marker in lowered for marker in _PASSWORD_REJECTS):
            raise PasswordRejected(detail or "password rejected", detail=detail)
        raise SignupError(f"registration failed (HTTP {response.status_code})", detail=detail)

    # ---------------------------------------------------------------- profile
    def profile(self) -> dict[str, Any]:
        response = request(self.client, "GET", PROFILE, headers=self._csrf_headers())
        if _looks_blocked(response):
            raise Blocked("profile call hit a bot challenge")
        if response.status_code in (401, 403):
            raise SignupError("profile call says we are not signed in", detail=response.text[:200])
        if response.status_code >= 400:
            raise SignupError(
                f"profile call failed (HTTP {response.status_code})", detail=response.text[:200]
            )
        try:
            payload = response.json()
        except json.JSONDecodeError as exc:
            raise SignupError("profile call returned non-JSON") from exc
        if not isinstance(payload, dict):
            raise SignupError("profile call returned an unexpected shape")
        return payload

    @staticmethod
    def api_key_from(profile: dict[str, Any]) -> str:
        candidates = (
            (profile.get("userprofile") or {}).get("api_key"),
            profile.get("api_key"),
            (profile.get("userprofile2") or {}).get("api_key"),
        )
        for value in candidates:
            if isinstance(value, str) and re.fullmatch(r"[0-9a-zA-Z-]{16,64}", value.strip()):
                return value.strip()
        return ""

    # ------------------------------------------------- desktop auth fallback
    def desktop_signin(self, *, timeout: float = 30.0) -> dict[str, Any]:
        """RFC 8252 loopback + PKCE exchange, mirrored from the shipped desktop app.

        We never bind the loopback port: the redirect target only has to look
        plausible, and the `code` is readable from the 302 `Location` header.
        """
        verifier = _b64url(secrets.token_bytes(32))
        challenge = _b64url(hashlib.sha256(verifier.encode()).digest())
        state = secrets.token_hex(32)
        port = secrets.randbelow(20000) + 45000
        url = (
            f"{DESKTOP_AUTH}?state={state}&port={port}"
            f"&code_challenge={challenge}&code_challenge_method=S256"
        )
        response = request(self.client, "GET", url, follow_redirects=False)
        if _looks_blocked(response):
            raise Blocked("desktop auth page hit a bot challenge")
        code = ""
        if response.is_redirect:
            location = response.headers.get("location", "")
            match = re.search(r"[?&]code=([A-Za-z0-9._~-]+)", location)
            if match:
                code = match.group(1)
        if not code:
            match = re.search(r'"code"\s*:\s*"([A-Za-z0-9._~-]+)"', response.text or "")
            if match:
                code = match.group(1)
        if not code:
            raise SignupError(
                f"desktop auth did not return a code (HTTP {response.status_code})",
                detail=(response.headers.get("location") or response.text or "")[:200],
            )

        last: httpx.Response | None = None
        for path in ("v2/", ""):
            target = f"{EXCHANGE}{path}"
            last = request(
                self.client,
                "POST",
                target,
                json={"code": code, "code_verifier": verifier},
                headers={"Content-Type": "application/json"},
                timeout=timeout,
            )
            if last.status_code == 404 and path == "v2/":
                continue
            break
        assert last is not None
        if _looks_blocked(last):
            raise Blocked("desktop auth exchange hit a bot challenge")
        if last.status_code >= 400:
            raise SignupError(
                f"desktop auth exchange failed (HTTP {last.status_code})",
                detail=last.text[:300],
            )
        payload = last.json()
        key = payload.get("api_key") if isinstance(payload, dict) else None
        if not isinstance(key, str) or not key.strip():
            raise NoApiKey("desktop auth exchange returned no api_key")
        return payload

    # ------------------------------------------------------------- composite
    def signup(self, email: str, password: str, *, username: str | None = None) -> dict[str, Any]:
        """Full flow for one address. Returns the account record to persist."""
        self.warm()

        info = self.lookup(email)
        if info.get("user_exists") is True:
            raise EmailTaken("address already has a DeepAI account", detail=info)

        self.save_signup_context(username or email.split("@")[0])
        registration = self.register(email, password, username=username)

        profile = self.profile()
        api_key = self.api_key_from(profile)
        source = "profile"
        if not api_key:
            exchanged = self.desktop_signin()
            api_key = str(exchanged.get("api_key", "")).strip()
            source = "desktop_auth"
        if not api_key:
            raise NoApiKey("signed up, but no api_key was exposed")

        user_profile = profile.get("userprofile") or {}
        plan = (profile.get("userprofile2") or {}).get("plan") or ""
        return {
            "email": email,
            "password": password,
            "username": profile.get("username") or "",
            "api_key": api_key,
            "api_key_source": source,
            "user_pk": profile.get("pk"),
            "plan": plan,
            "is_pro": bool((profile.get("userprofile2") or {}).get("user_has_deepai_pro")),
            "sessionid": self.client.cookies.get("sessionid", ""),
            "csrftoken": self.client.cookies.get("csrftoken", ""),
            "registration_key": registration.get("key", ""),
            "profile": {
                "pk": profile.get("pk"),
                "username": profile.get("username"),
                "email": profile.get("email"),
                "userprofile": user_profile,
                "userprofile2": profile.get("userprofile2"),
            },
        }


def probe_proxy(proxy: str, timeout: float = 20.0) -> dict[str, Any]:
    """Cheap liveness + egress-IP check for `doctor`."""
    client = build_client(proxy=proxy, timeout=timeout)
    try:
        response = request(client, "GET", "https://api.ipify.org?format=json")
        ip = ""
        try:
            ip = response.json().get("ip", "")
        except json.JSONDecodeError:
            ip = (response.text or "")[:64]
        return {"ok": response.status_code == 200, "status": response.status_code, "ip": ip}
    finally:
        client.close()
