"""线描述：一条 OpenAI 兼容线要说的全部"站点事实"。

worker 侧（python）和网关侧（node）用同一份字段名，方便对照与复用。
每个 `tools/<line>-reg/<line>_reg/site.py` 只填这一个 dataclass。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any


@dataclass(slots=True)
class Site:
    """一条线的静态描述。"""

    id: str
    name: str
    base_url: str                       # 站点首页，例如 https://api.agnes-ai.cn
    api_base: str                        # OpenAI 兼容基址（含 /v1）
    signup_path: str = "/api/auth/sign-up/email"      # Better Auth 标准
    signin_path: str = "/api/auth/sign-in/email"
    session_path: str = "/api/auth/get-session"
    health_path: str = "/api/auth/ok"
    send_verify_path: str = "/api/auth/send-verification-email"
    verify_path: str = "/api/auth/verify-email"
    credits_path: str = ""               # 有就顺手读一下额度
    # 注册形态：better-auth（默认）| code-signup（邮箱验证码）
    flow: str = "better-auth"
    verify_code_path: str = ""           # GET ?email=&purpose=register -> 发码
    register_path: str = ""              # POST {email,password,password_confirm,code}
    token_path: str = ""                 # POST Bearer {name, api_key_profile} -> key
    token_json_path: str = "data.access_token"
    key_json_path: str = "data.key"
    # 发码时可能需要的验证码参数（腾讯云验证码是 ticket + randstr）
    captcha_params: tuple[str, ...] = ()
    login_url: str = ""                 # captcha=browser 时打开的页面
    key_candidates: tuple[tuple[str, str], ...] = ()   # (path, method)
    key_prefix: str = "sk-"
    models: tuple[str, ...] = ()
    free_suffix: str = ""                # 聚合站：免费模型后缀（如 ":free" / "-free"）
    notes: str = ""
    extra_headers: dict[str, str] = field(default_factory=dict)
    create_key_body: tuple[dict[str, Any], ...] = ({"name": "dcg"}, {})

    def url(self, path: str) -> str:
        return f"{self.base_url.rstrip('/')}{path}"

    def describe(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "name": self.name,
            "base_url": self.base_url,
            "api_base": self.api_base,
            "key_prefix": self.key_prefix,
            "models": list(self.models),
            "free_suffix": self.free_suffix,
            "key_candidates": len(self.key_candidates),
            "flow": self.flow,
            "notes": self.notes,
        }
