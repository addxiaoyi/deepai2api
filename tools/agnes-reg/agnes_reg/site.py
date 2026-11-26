"""Agnes AI 的站点事实 —— 从真实注册脚本（agnes_register.py）核对出来的端点。

四步流程（注意：**不是** Better Auth，是邮箱验证码）：

    1) GET  https://platform-backend.agnes-ai.com/api/verification?email=&purpose=register
       -> {"code":200} 并把 6 位验证码发到邮箱
    2) 邮箱里取 6 位码
    3) POST https://platform-backend.agnes-ai.com/api/user/register
       {email, password, password_confirm, code}
       -> {"code":200,"data":{"access_token":..., "user":{"display_id","username","email"}}}
    4) POST https://platform-backend.agnes-ai.com/api/token
       Authorization: Bearer <access_token>
       {name, api_key_profile:"default"}
       -> {"code":200,"data":{"key":"sk-…","name":…,"key_profile":…}}

模型 API 在另一个域：`https://api.agnes-ai.cn/v1`（OpenAI 兼容）。

已知限流：同一个**邮箱域名**注册太频繁会被拒（`too many registration ... domain`），
所以 worker 会按域名轮换 + 冷却（见 linekit.runner._next_domain）。
"""

from __future__ import annotations

from linekit.site import Site

BACKEND = "https://platform-backend.agnes-ai.com/api"

SITE = Site(
    id="agnes",
    name="Agnes AI",
    base_url="https://platform-backend.agnes-ai.com",
    api_base="https://api.agnes-ai.cn/v1",
    flow="code-signup",
    verify_code_path="/api/verification",
    register_path="/api/user/register",
    token_path="/api/token",
    token_json_path="data.access_token",
    key_json_path="data.key",
    # 发码需要腾讯云验证码：ticket + randstr
    # （2026-11 实测 /api/verification 回 {"code":400,"message":"Captcha verification is required"}）
    captcha_params=("ticket", "randstr"),
    login_url="https://platform.agnes-ai.cn/login",
    # code-signup 用不到这些，但保留空值让 doctor 不误报
    signup_path="",
    signin_path="",
    session_path="",
    health_path="",
    send_verify_path="",
    verify_path="",
    credits_path="",
    key_candidates=(),
    key_prefix="sk-",
    models=("agnes-3.0-flash", "agnes-2.5-flash"),
    notes="邮箱验证码注册 + Bearer 建 Key；同域名注册有频率限制",
)
