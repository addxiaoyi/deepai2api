"""Poe GraphQL operation templates observed on the live signup flow (2026-11-25).

Poe posts a named operation plus a content hash; the hash is per-deployment, so it
lives here as data and can be overridden from config without touching code. If a
hash goes stale the server answers `OPERATION_HASH_MISMATCH` and the runner
re-captures it from the site's own request (see `protocol.capture_templates`).
"""

from __future__ import annotations

SEND_CODE = "MainSignupLoginSection_sendVerificationCodeMutation_Mutation"
SIGNUP_CODE = "SignupOrLoginWithCodeSection_signupWithVerificationCodeMutation_Mutation"

TEMPLATES: dict[str, dict] = {
    SEND_CODE: {
        "hash": "1dd8b92bf3739a39e34d06d5b673ec25870b9727354f3f9b26361eba2430a452",
        "variables": {
            "emailAddress": "",
            "phoneNumber": None,
            "recaptchaToken": "",
        },
    },
    SIGNUP_CODE: {
        "hash": "bed7d65dacf2bc72c6c94c4718fbb7edf2f2629c4efa604575a8e0f7e5e9fb86",
        "variables": {
            "verificationCode": "",
            "emailAddress": "",
            "phoneNumber": None,
            "subscriptions": [],
        },
    },
}

# Statuses the sendVerificationCode mutation can answer with, and what they mean.
SEND_CODE_STATUS = {
    "user_with_confirmed_email_not_found": "signup",   # good: no account yet, code sent
    "user_with_unconfirmed_email": "signup",           # resend on a half-finished signup
    "user_exists": "exists",                           # already registered -> login path
    "invalid_email": "invalid",
    "automated_request_detected": "automated",
}


def build(query_name: str, **variables) -> dict:
    template = TEMPLATES[query_name]
    merged = dict(template["variables"])
    merged.update(variables)
    return {
        "queryName": query_name,
        "variables": merged,
        "extensions": {"hash": template["hash"]},
    }
