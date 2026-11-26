"""Pollinations AI 的站点事实（唯一需要改的文件）。"""

from __future__ import annotations

from linekit.site import Site

SITE = Site(
    id="pollinations",
    name="Pollinations AI",
    base_url="https://gen.pollinations.ai",
    api_base="https://gen.pollinations.ai/v1",
    signup_path="",
    signin_path="",
    session_path="/api/auth/get-session",
    health_path="",
    send_verify_path="",
    verify_path="",
    credits_path="",
    key_candidates=(
        
    ),
    key_prefix="",
    models=(
        "openai",
        "openai-large",
        "gemini",

    ),
    free_suffix="",
    notes="匿名线：不需要注册，按 IP 限速（10 RPM / 500 RPD）",
)
