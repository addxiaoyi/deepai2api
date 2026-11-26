"""Cerebras Cloud 的站点事实（唯一需要改的文件）。"""

from __future__ import annotations

from linekit.site import Site

SITE = Site(
    id="cerebras",
    name="Cerebras Cloud",
    base_url="https://cloud.cerebras.ai",
    api_base="https://api.cerebras.ai/v1",
    signup_path="/api/auth/sign-up/email",
    signin_path="/api/auth/sign-in/email",
    session_path="/api/auth/get-session",
    health_path="/api/auth/ok",
    send_verify_path="/api/auth/send-verification-email",
    verify_path="/api/auth/verify-email",
    credits_path="",
    key_candidates=(
        ("/api/keys", "GET"),
        ("/api/keys", "POST"),
        ("/api/auth/api-keys", "GET"),
        ("/api/auth/api-keys", "POST"),
        ("/api/user/api-keys", "GET"),
        ("/api/user/api-keys", "POST"),

    ),
    key_prefix="csk-",
    models=(
        "llama-3.3-70b",
        "qwen-3-235b-a22b",

    ),
    free_suffix="",
    notes="30 RPM / 14400 RPD / 60k TPM；Key 在 console 签发，端点由 doctor 校验",
)
