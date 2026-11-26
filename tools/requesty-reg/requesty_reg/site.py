"""Requesty 的站点事实（唯一需要改的文件）。"""

from __future__ import annotations

from linekit.site import Site

SITE = Site(
    id="requesty",
    name="Requesty",
    base_url="https://app.requesty.ai",
    api_base="https://router.requesty.ai/v1",
    signup_path="/api/auth/sign-up/email",
    signin_path="/api/auth/sign-in/email",
    session_path="/api/auth/get-session",
    health_path="/api/auth/ok",
    send_verify_path="/api/auth/send-verification-email",
    verify_path="/api/auth/verify-email",
    credits_path="",
    key_candidates=(
        ("/api/api-keys", "GET"),
        ("/api/api-keys", "POST"),
        ("/api/keys", "GET"),
        ("/api/keys", "POST"),
        ("/api/user/api-keys", "GET"),
        ("/api/user/api-keys", "POST"),

    ),
    key_prefix="sk-",
    models=(
        "nvidia/nemotron-3-ultra",
        "gemma-4-31b",

    ),
    free_suffix="",
    notes="200 RPD 共享；聚合网关，模型名以 /v1/models 为准",
)
