"""Protocol engine: Poe's own GraphQL, called from inside an already-trusted page.

Why in-page instead of httpx: the mutation needs a live reCAPTCHA v3 token and a
`poe-formkey` that the app derives per page load. Both are available in the page
context; httpx would have to reimplement them (and Poe's edge would still score
the request by IP/device). So the "protocol" path keeps the browser only as a
trust anchor and does every signup step as a JSON call — no form typing, no
selector fragility, ~3x faster than the DOM path.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from . import gql_templates
from .browser import PoeSession
from .errors import (
    AutomatedRequest,
    EmailExists,
    EmailRejected,
    PhoneRequired,
    PoeError,
)

OVERRIDE = Path(__file__).resolve().parent.parent / "out" / "gql_templates.json"


def load_templates(path: Path | None = None) -> None:
    """Merge a captured template set (see `refresh`) over the bundled defaults."""
    target = path or OVERRIDE
    if not target.exists():
        return
    try:
        data = json.loads(target.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return
    for name, template in data.items():
        if isinstance(template, dict) and template.get("hash"):
            gql_templates.TEMPLATES[name] = {
                "hash": template["hash"],
                "variables": template.get("variables") or gql_templates.TEMPLATES.get(name, {}).get("variables", {}),
            }


class PoeProtocol:
    def __init__(self, session: PoeSession, *, log=None) -> None:
        self.session = session
        self.log = log or (lambda *_a, **_k: None)

    # -------------------------------------------------------------- step one
    def send_code(self, email: str) -> dict[str, Any]:
        """Ask Poe to mail a code.

        Preferred: a signed GraphQL call with a reCAPTCHA token minted from the
        page. Poe lazy-loads reCAPTCHA on submit, so when the token cannot be
        produced out-of-band this falls back to letting the app submit once and
        capturing its response — still one call, no second DOM automation later.
        """
        try:
            token = self.session.recaptcha_token("signup")
            payload = self.session.gql(
                gql_templates.build(
                    gql_templates.SEND_CODE, emailAddress=email, recaptchaToken=token
                )
            )
            node = (payload.get("data") or {}).get("sendVerificationCode") or {}
            self.log("protocol: send_code via in-page GraphQL")
        except PoeError as exc:
            self.log(f"protocol: token unavailable ({exc}); using the app's own submit")
            from .browser import PoeBrowser

            node = PoeBrowser(self.session, log=self.log).submit_email_and_capture(email)
            self.log("protocol: send_code via app submit (response captured)")

        status = str(node.get("status") or "")
        mapped = gql_templates.SEND_CODE_STATUS.get(status, status or "unknown")
        self.log(f"protocol: send_code -> {status or mapped}")

        if mapped == "automated":
            raise AutomatedRequest(
                "Poe flagged the request as automated", detail=node.get("errorMessage")
            )
        if mapped == "exists":
            raise EmailExists("Poe already has an account for this address", detail=node)
        if mapped == "invalid":
            raise EmailRejected(node.get("errorMessage") or "email rejected", detail=node)
        if mapped != "signup":
            raise PoeError(f"send_code returned {status!r}", detail=node)
        return node

    # -------------------------------------------------------------- step two
    def signup_with_code(self, email: str, code: str) -> dict[str, Any]:
        body = gql_templates.build(
            gql_templates.SIGNUP_CODE,
            verificationCode=code,
            emailAddress=email,
        )
        payload = self.session.gql(body)
        node = (payload.get("data") or {}).get("signupWithVerificationCode") or {}
        status = str(node.get("status") or "").lower()
        self.log(f"protocol: signup_with_code -> {status or 'ok'}")

        if status in ("invalid_code", "invalid_verification_code", "code_expired"):
            raise PoeError("verification code rejected", detail=node)
        if status == "automated_request_detected":
            raise AutomatedRequest("Poe flagged the signup as automated", detail=node)
        if status in ("recently_verified_phone_number_not_found", "phone_number_required"):
            raise PhoneRequired(
                "email verified, but Poe only creates the account once a phone was verified recently",
                detail=node,
            )
        if status in ("success", "ok", "signed_up", ""):
            return node
        # Unknown status: keep the payload for the failure record.
        raise PoeError(f"signup_with_code returned {status!r}", detail=node)

    # ------------------------------------------------------------ convenience
    def signup(self, email: str, code_fetcher) -> dict[str, Any]:
        """send_code -> poll the mailbox -> signup_with_code."""
        self.send_code(email)
        code = code_fetcher()
        if not code:
            raise PoeError("no verification code arrived")
        return self.signup_with_code(email, code)
