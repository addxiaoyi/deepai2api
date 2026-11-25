"""Allmodels dashboard signup protocol, reconstructed from the shipped SPA.

The dashboard is a Vite/React app over **Better Auth**, with a thin REST layer
for the workspace objects. Signing up means four calls:

    POST /api/auth/sign-up/email      json {name,email,password,callbackURL}
                                      header x-turnstile-token
                                      -> 200 + session cookie, verification mail sent
    GET  /api/auth/verify-email?token -> 302 + verified session (follow the mail link)
    GET  /api/me/overview             -> org / projects / guardrails (provisioned async)
    GET  /api/me/signup-key           -> {"key": "<plaintext>"}  the auto-provisioned key

and, for extra keys per account:

    POST /api/keys                    json {displayName,projectId,guardrailId,environment}
                                      -> {"key": "<plaintext>"}
    GET  /api/keys                    -> {"keys":[{id,keyPrefix,last4,status,...}]}
    POST /api/keys/{id}/revoke

Notes that matter operationally:
  * `gmail.com` / `googlemail.com` are refused client-side, so no Gmail address.
  * Turnstile is enforced server-side (`{"code":"captcha_failed"}`) — a token with
    the `signup` action has to be presented, or the request dies at 403.
  * the session cookie is what authorises every `/api/me/*` call afterwards, so a
    signup client should keep its cookie jar per account.
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
    PasswordRejected,
    RateLimited,
    SignupError,
    VerificationPending,
)
from .net import build_client, chrome_user_agent, request

BASE = "https://dash.allmodels.io"
TURNSTILE_SITE_KEY = "0x4AAAAAAFoFe99_cOH0RnFT"
TURNSTILE_ACTION = "signup"

SIGNUP = "/api/auth/sign-up/email"
SIGNIN = "/api/auth/sign-in/email"
SESSION = "/api/auth/get-session"
VERIFY = "/api/auth/verify-email"
OVERVIEW = "/api/me/overview"
SIGNUP_KEY = "/api/me/signup-key"
KEYS = "/api/keys"

BLOCKED_DOMAINS = {"gmail.com", "googlemail.com"}

_CF_MARKERS = ("just a moment", "cf-browser-verification", "challenge-platform", "attention required")
_EMAIL_TAKEN_CODES = ("user_already_exists", "email_already_exists", "user_exists")
_EMAIL_BAD_CODES = ("invalid_email", "email_not_allowed", "disposable_email")
_PASSWORD_BAD_CODES = ("password_too_short", "password_too_long", "invalid_password", "password_compromised")


def _looks_blocked(response: httpx.Response) -> bool:
    if response.status_code in (403, 503) and "cloudflare" in response.headers.get("server", "").lower():
        body = (response.text or "")[:2000].lower()
        if any(marker in body for marker in _CF_MARKERS):
            return True
    body = (response.text or "")[:4000].lower()
    return any(marker in body for marker in _CF_MARKERS)


def _error_code(response: httpx.Response) -> tuple[str, str]:
    """Better Auth answers `{code,message}`; the REST layer answers `{error}`."""
    try:
        payload = response.json()
    except (json.JSONDecodeError, ValueError):
        return "", (response.text or "")[:300]
    if isinstance(payload, dict):
        code = str(payload.get("code") or payload.get("error") or "")
        message = str(payload.get("message") or payload.get("error_description") or payload.get("error") or "")
        return code, message
    return "", str(payload)[:300]


def _raise_for(path: str, response: httpx.Response) -> None:
    """Map one failed dashboard call onto the runner's retry vocabulary."""
    code, message = _error_code(response)
    lowered = f"{code} {message}".lower()
    status = response.status_code
    detail = {"status": status, "code": code, "message": message[:400]}

    if _looks_blocked(response):
        raise Blocked(f"{path}: edge answered with a bot challenge", detail=detail)
    if status == 429:
        raise RateLimited(f"{path}: rate limited", detail=detail)
    if "captcha" in lowered:
        raise CaptchaRequired(f"{path}: turnstile rejected the token ({message or code})", detail=detail)
    if any(k in lowered for k in _EMAIL_TAKEN_CODES) or "already exists" in lowered or "already registered" in lowered:
        raise EmailTaken(f"{path}: address already registered", detail=detail)
    if any(k in lowered for k in _EMAIL_BAD_CODES) or "invalid email" in lowered:
        raise EmailRejected(f"{path}: address rejected ({message or code})", detail=detail)
    if any(k in lowered for k in _PASSWORD_BAD_CODES):
        raise PasswordRejected(f"{path}: password rejected ({message or code})", detail=detail)
    if status in (401, 403):
        raise SignupError(f"{path}: not authorised (HTTP {status})", detail=detail)
    raise SignupError(f"{path}: failed (HTTP {status})", detail=detail)


class AllModels:
    """One signup session = one cookie jar = one account."""

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
        self.language = language
        self.user_agent = user_agent or chrome_user_agent()
        self.client = build_client(
            proxy=proxy,
            timeout=timeout,
            user_agent=self.user_agent,
            language=language,
            transport=transport,
        )
        self.client.headers.update({"Origin": self.base, "Referer": f"{self.base}/signup"})

    # ------------------------------------------------------------------ basic
    def __enter__(self) -> "AllModels":
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()

    def close(self) -> None:
        self.client.close()

    def warm(self) -> None:
        """Pick up the edge cookies a first page load would leave behind."""
        response = request(self.client, "GET", f"{self.base}/signup")
        if response.status_code >= 500:
            raise Blocked(f"signup page answered HTTP {response.status_code}")
        if _looks_blocked(response) and "text/html" not in response.headers.get("content-type", ""):
            raise Blocked("signup page hit a bot challenge")

    def cookies(self) -> list[dict[str, str]]:
        out: list[dict[str, str]] = []
        for cookie in self.client.cookies.jar:
            out.append(
                {
                    "name": cookie.name,
                    "value": cookie.value,
                    "domain": cookie.domain or "",
                    "path": cookie.path or "/",
                }
            )
        return out

    # --------------------------------------------------------------- signup
    def signup(
        self,
        email: str,
        password: str,
        *,
        name: str = "",
        turnstile: str = "",
        callback_url: str = "",
    ) -> dict[str, Any]:
        domain = email.rsplit("@", 1)[-1].lower()
        if domain in BLOCKED_DOMAINS:
            raise EmailRejected(f"{domain} is refused by the dashboard; use another domain")

        payload = {
            "name": name or email.split("@")[0],
            "email": email,
            "password": password,
            "callbackURL": callback_url or f"{self.base}/verify-email",
        }
        headers = {"Content-Type": "application/json", "Accept": "application/json"}
        if turnstile:
            headers["x-turnstile-token"] = turnstile
        response = request(
            self.client,
            "POST",
            f"{self.base}{SIGNUP}",
            json=payload,
            headers=headers,
        )
        if response.status_code >= 400:
            _raise_for(SIGNUP, response)
        try:
            body = response.json()
        except json.JSONDecodeError:
            body = {}
        return body if isinstance(body, dict) else {}

    def login(self, email: str, password: str) -> dict[str, Any]:
        response = request(
            self.client,
            "POST",
            f"{self.base}{SIGNIN}",
            json={"email": email, "password": password},
            headers={"Content-Type": "application/json", "Accept": "application/json"},
        )
        if response.status_code >= 400:
            _raise_for(SIGNIN, response)
        try:
            body = response.json()
        except json.JSONDecodeError:
            body = {}
        return body if isinstance(body, dict) else {}

    # ---------------------------------------------------------- verification
    def session(self) -> dict[str, Any] | None:
        response = request(self.client, "GET", f"{self.base}{SESSION}")
        if response.status_code >= 400:
            if response.status_code in (401, 403):
                return None
            _raise_for(SESSION, response)
        try:
            payload = response.json()
        except json.JSONDecodeError:
            return None
        return payload if isinstance(payload, dict) else None

    def verified(self) -> bool:
        payload = self.session() or {}
        user = payload.get("user") or {}
        return user.get("emailVerified") is True

    def verify_link(self, link: str) -> bool:
        """Follow the mail link.

        Better Auth consumes the token and 302s into the SPA; a stale token lands
        on `?error=TOKEN_EXPIRED` instead. We deliberately do not follow the
        redirect — the SPA route is a client-side shell, not an API answer.
        """
        response = request(self.client, "GET", link, follow_redirects=False)
        if 300 <= response.status_code < 400:
            location = response.headers.get("location", "")
            if "error=" in location:
                raise VerificationPending(f"verification link rejected: {location[:160]}")
            return True
        if response.status_code >= 400:
            _raise_for(VERIFY, response)
        final = str(response.url)
        if "error=" in final:
            raise VerificationPending(f"verification link rejected: {final[:160]}")
        return True

    def resend_verification(self, email: str, callback_url: str = "") -> None:
        response = request(
            self.client,
            "POST",
            f"{self.base}/api/auth/send-verification-email",
            json={"email": email, "callbackURL": callback_url or f"{self.base}/verify-email"},
            headers={"Content-Type": "application/json"},
        )
        if response.status_code >= 400:
            _raise_for("/api/auth/send-verification-email", response)

    # ------------------------------------------------------------- workspace
    def overview(self) -> dict[str, Any]:
        response = request(self.client, "GET", f"{self.base}{OVERVIEW}")
        if response.status_code >= 400:
            _raise_for(OVERVIEW, response)
        payload = response.json()
        return payload if isinstance(payload, dict) else {}

    def wait_overview(self, *, timeout: float = 60.0, poll: float = 3.0) -> dict[str, Any]:
        """Provisioning is asynchronous: the first key appears a moment later."""
        deadline = time.time() + timeout
        last: dict[str, Any] = {}
        while time.time() < deadline:
            last = self.overview()
            if (last.get("projects") or []) and (last.get("guardrails") or []):
                return last
            time.sleep(poll)
        return last

    def signup_key(self) -> str:
        response = request(self.client, "GET", f"{self.base}{SIGNUP_KEY}")
        if response.status_code >= 400:
            _raise_for(SIGNUP_KEY, response)
        payload = response.json() if response.content else {}
        key = (payload or {}).get("key") if isinstance(payload, dict) else None
        return key if isinstance(key, str) else ""

    def list_keys(self) -> list[dict[str, Any]]:
        response = request(self.client, "GET", f"{self.base}{KEYS}")
        if response.status_code >= 400:
            _raise_for(KEYS, response)
        payload = response.json() if response.content else {}
        rows = (payload or {}).get("keys") if isinstance(payload, dict) else None
        return rows if isinstance(rows, list) else []

    def create_key(
        self,
        *,
        display_name: str,
        project_id: str = "",
        guardrail_id: str = "",
        environment: str = "live",
    ) -> dict[str, Any]:
        response = request(
            self.client,
            "POST",
            f"{self.base}{KEYS}",
            json={
                "displayName": display_name,
                "projectId": project_id,
                "guardrailId": guardrail_id,
                "environment": environment,
            },
            headers={"Content-Type": "application/json"},
        )
        if response.status_code >= 400:
            _raise_for(KEYS, response)
        payload = response.json() if response.content else {}
        return payload if isinstance(payload, dict) else {}

    def revoke_key(self, key_id: str) -> None:
        response = request(self.client, "POST", f"{self.base}{KEYS}/{key_id}/revoke")
        if response.status_code >= 400:
            _raise_for(f"{KEYS}/{key_id}/revoke", response)

    # ------------------------------------------------------------- composite
    def signup_flow(
        self,
        email: str,
        password: str,
        *,
        name: str = "",
        turnstile: str = "",
        verify: bool = True,
        provider: Any = None,
        box: Any = None,
        verify_timeout: float = 180.0,
        key_name: str = "",
        keys: int = 1,
        log: Callable[[str], None] | None = None,
    ) -> dict[str, Any]:
        """Full flow for one address. Returns the account record to persist."""
        say = log or (lambda _m: None)
        self.warm()
        signup_body = self.signup(email, password, name=name, turnstile=turnstile)

        email_verified: bool | None = None
        if verify and provider is not None and box is not None:
            link = provider.wait_for_link(
                box,
                timeout=verify_timeout,
                must_contain=("verify-email", "token="),
            )
            if not link:
                raise VerificationPending(
                    f"no verification mail for {email} within {verify_timeout:.0f}s"
                )
            self.verify_link(link)
            email_verified = self.verified()
            say(f"邮箱已验证 {email} (emailVerified={email_verified})")
        else:
            # No mailbox loop: still record what the session says, the dashboard
            # may already have marked the address verified.
            email_verified = self.verified()

        overview = self.wait_overview(timeout=60.0)
        org = overview.get("organization") or {}
        projects = overview.get("projects") or []
        guardrails = overview.get("guardrails") or []
        project_id = next((p.get("id") for p in projects if p.get("isDefault")), None) or (
            projects[0].get("id") if projects else ""
        )
        guardrail_id = next((g.get("id") for g in guardrails if g.get("isDefault")), None) or (
            guardrails[0].get("id") if guardrails else ""
        )

        api_key = self.signup_key()
        key_source = "signup-key" if api_key else ""
        if not api_key:
            try:
                created = self.create_key(
                    display_name=key_name or f"{email.split('@')[0]}-{time.strftime('%Y-%m-%d')}",
                    project_id=project_id or "",
                    guardrail_id=guardrail_id or "",
                )
            except SignupError as exc:
                # The account exists; the key did not materialise. That is a
                # different failure than "signup failed" for the retry policy.
                raise NoApiKey(f"signup ok, key creation failed: {exc}", detail=exc.detail) from exc
            api_key = str(created.get("key") or "")
            key_source = "created" if api_key else ""
        if not api_key:
            raise NoApiKey("signup finished, but no API key could be read or minted")

        extra_keys: list[dict[str, str]] = []
        for index in range(max(0, keys - 1)):
            try:
                created = self.create_key(
                    display_name=f"{key_name or email.split('@')[0]}-{index + 2}",
                    project_id=project_id or "",
                    guardrail_id=guardrail_id or "",
                )
            except SignupError:
                break
            token = str(created.get("key") or "")
            if token:
                extra_keys.append({"key": token, "source": "created"})

        listed = self.list_keys()
        record = next((k for k in listed if api_key and str(api_key).startswith(str(k.get("keyPrefix") or "\0"))), None)

        return {
            "email": email,
            "password": password,
            "name": name or (signup_body.get("user") or {}).get("name", ""),
            "api_key": api_key,
            "api_key_source": key_source,
            "extra_keys": extra_keys,
            "key_id": str((record or {}).get("id") or ""),
            "key_prefix": str((record or {}).get("keyPrefix") or ""),
            "key_last4": str((record or {}).get("last4") or ""),
            "project_id": project_id or "",
            "guardrail_id": guardrail_id or "",
            "organization_id": str(org.get("id") or ""),
            "organization_slug": str(org.get("slug") or ""),
            "user_id": str(((self.session() or {}).get("user") or {}).get("id") or ""),
            "email_verified": email_verified,
            "cookies": self.cookies(),
        }


def probe(base_url: str = BASE, *, proxy: str = "", timeout: float = 20.0) -> dict[str, Any]:
    """Cheap liveness check for `doctor`: health, signup page, turnstile presence."""
    client = build_client(proxy=proxy, timeout=timeout)
    try:
        health = request(client, "GET", f"{base_url.rstrip('/')}/api/auth/ok")
        page = request(client, "GET", f"{base_url.rstrip('/')}/signup")
        title = ""
        match = re.search(r"<title>([^<]*)</title>", page.text or "")
        if match:
            title = match.group(1).strip()
        return {
            "ok": health.status_code == 200 and health.text.strip() == '{"ok":true}',
            "auth_ok": f"HTTP {health.status_code} {health.text[:40]}",
            "signup_page": f"HTTP {page.status_code} {len(page.text or '')} bytes",
            "title": title,
            "turnstile_site_key": TURNSTILE_SITE_KEY,
        }
    finally:
        client.close()
