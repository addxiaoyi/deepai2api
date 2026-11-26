"""OpenCode Zen 的站点事实（唯一需要改的文件）。"""

from __future__ import annotations

from linekit.site import Site

SITE = Site(
    id="opencode-zen",
    name="OpenCode Zen",
    base_url="https://opencode.ai",
    api_base="https://opencode.ai/zen/v1",
    signup_path="/api/auth/sign-up/email",
    signin_path="/api/auth/sign-in/email",
    session_path="/api/auth/get-session",
    health_path="/api/auth/ok",
    send_verify_path="/api/auth/send-verification-email",
    verify_path="/api/auth/verify-email",
    credits_path="",
    key_candidates=(
        ("/zen/api/keys", "GET"),
        ("/zen/api/keys", "POST"),
        ("/api/zen/keys", "GET"),
        ("/api/zen/keys", "POST"),
        ("/api/keys", "GET"),
        ("/api/keys", "POST"),

    ),
    key_prefix="sk-",
    models=(
        "mimo-v2.5-free",
        "nemotron-3-ultra-free",

    ),
    free_suffix="-free",
    notes="30 RPM / 500 RPD；免费模型带 -free 后缀",
)
