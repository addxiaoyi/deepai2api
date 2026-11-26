"""UnoRouter 的站点事实（唯一需要改的文件）。"""

from __future__ import annotations

from linekit.site import Site

SITE = Site(
    id="unorouter",
    name="UnoRouter",
    base_url="https://api.unorouter.com",
    api_base="https://api.unorouter.com/v1",
    signup_path="/api/auth/sign-up/email",
    signin_path="/api/auth/sign-in/email",
    session_path="/api/auth/get-session",
    health_path="/api/auth/ok",
    send_verify_path="/api/auth/send-verification-email",
    verify_path="/api/auth/verify-email",
    credits_path="",
    # 2026-11 实测：POST https://api.unorouter.com/api/token/ → 401
    # {"code":"AUTH_UNAUTHORIZED","message":"Unauthorized, invalid access token"}
    # 注意尾斜杠（不带会 307 到 /api/token/）；GET 会被 Cloudflare 挡成 403
    key_candidates=(
        ("/api/token/", "POST"),
        ("/api/token", "POST"),
        ("/api/keys/", "GET"),
        ("/api/keys/", "POST"),
    ),
    key_prefix="sk-",
    models=(
        "deepseek-v4-flash:free",
        "glm-5.2:free",

    ),
    free_suffix=":free",
    notes="190+ :free 模型；单模型 1 RPM，靠模型轮换；发 Key 端点 /api/token/（需登录，401=存在）",
    login_url="https://unorouter.com/token",
)
