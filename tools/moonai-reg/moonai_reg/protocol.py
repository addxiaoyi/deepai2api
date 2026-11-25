"""协议引擎：注册 → 邮箱验证 → 登录 → OAuth → 令牌 → 组织 → API Key。

全部端点都是线上实测出来的（取证表见 docs/MOONAI-POOL.md）：

    1. GET  {auth}/auth/signup/                     取 csrfmiddlewaretoken + csrftoken
    2. POST {auth}/auth/signup/                     username/email/password1/agree_*
                                                    → 302 /auth/email-verification-sent/
    3. GET  {auth}/auth/verify-email/?token=<uuid>  邮件里的链接 → 302 dashboard?registration_success=true
    4. GET  {auth}/auth/signin/                     取 csrf
       POST {auth}/auth/signin/                     username=<email>&password=…  → sessionid
    5. GET  {core}/api/v1/auth/url                  返回 authentik auth_url
                                                    （默认 redirect_uri = dashboard.mooonai.com/oauth，
                                                     传别的值会 400 Mismatching redirect URI）
    6. GET  {auth_url}                              带 sessionid → 302 /oauth?code=…&state=…
    7. POST {core}/api/v1/auth/token                form auth_response_url=<完整回调 URL>
                                                    → access_token(86400s) + refresh_token + id_token
    8. POST {core}/api/v1/auth/userinfo             Bearer → sub / preferred_username / email_verified
    9. GET  {core}/api/v1/organizations/            → {count, results[]}
       POST {core}/api/v1/organizations             {"name": …}（列表为空时自动建）
   10. POST {core}/api/v1/organizations/{org}/api-keys  {"name": …} → {"key": "api_…"}
   11. POST {core}/api/v1/services/llm-proxy/chat/completions  Bearer access_token → 自检

注意第 10 步的 `api_…` **不是**模型调用凭据（它给 Agent 渠道 Webhook 用）；
调模型必须用第 7 步的 access_token —— 这就是反代层 `authMode: "user-token"` 的原因。
"""

from __future__ import annotations

import json
import re
import time
from dataclasses import dataclass, field

import httpx

from .config import (
    ALLOWED_MODELS,
    API_KEYS_PATH,
    DEFAULT_MODEL,
    LLM_PROXY_PATH,
    ORGS_PATH,
    ORG_CREATE_PATH,
    SIGNIN_PATH,
    SIGNUP_PATH,
    TOKEN_PATH,
    USERINFO_PATH,
)
from .errors import (
    Blocked,
    EmailRejected,
    EmailTaken,
    LoginRejected,
    MoonAIError,
    NoApiKey,
    NoToken,
    UpstreamError,
)
from .identity import Persona
from .net import Session

CSRF_RE = re.compile(r'name="csrfmiddlewaretoken"\s+value="([^"]+)"')
ERROR_RE = re.compile(r'data-error[^>]*>([^<]{2,200})<', re.I)
EMAIL_TAKEN_HINTS = (
    "уже существует",
    "already exists",
    "already registered",
    "уже зарегистрирован",
    "email is taken",
    "пользователь с таким",
)
EMAIL_REJECTED_HINTS = (
    "используйте свою почту",
    "please use your own email",
    "введите корректный",
    "invalid email",
    "домен",
    "disposable",
    "одноразов",
)


def _json(res: httpx.Response) -> dict:
    try:
        data = res.json()
    except (json.JSONDecodeError, ValueError) as cause:
        raise UpstreamError(f"上游返回非 JSON：{res.text[:160]}") from cause
    return data if isinstance(data, dict) else {"data": data}


def _redirect_location(res: httpx.Response, base: str) -> str:
    location = res.headers.get("location") or ""
    if not location:
        return ""
    if location.startswith("http"):
        return location
    return httpx.URL(base).join(location).__str__()


@dataclass
class Registration:
    """一次注册产出的全部凭据。"""

    email: str
    password: str
    username: str
    name: str = ""
    apiKey: str = ""
    keyId: str = ""
    organizationId: str = ""
    organizationName: str = ""
    accessToken: str = ""
    refreshToken: str = ""
    accessTokenExpiresAt: int = 0
    emailVerified: bool = False
    subject: str = ""
    engine: str = "protocol"
    domain: str = ""
    proxy: str = ""
    extraKeys: list[dict] = field(default_factory=list)
    cookies: list[dict] = field(default_factory=list)
    note: str = ""
    probe: dict = field(default_factory=dict)

    @property
    def keyPrefix(self) -> str:
        return self.apiKey[:8] if self.apiKey else ""

    @property
    def keyLast4(self) -> str:
        return self.apiKey[-4:] if self.apiKey else ""

    def to_record(self) -> dict:
        return {
            "email": self.email,
            "password": self.password,
            "name": self.name or self.username,
            "apiKey": self.apiKey,
            "keyId": self.keyId,
            "keyPrefix": self.keyPrefix,
            "keyLast4": self.keyLast4,
            "keySource": "org-api-keys" if self.apiKey else "",
            "extraKeys": self.extraKeys,
            "organizationId": self.organizationId,
            "accessToken": self.accessToken,
            "refreshToken": self.refreshToken,
            "accessTokenExpiresAt": self.accessTokenExpiresAt,
            "emailVerified": self.emailVerified,
            "status": "active" if (self.apiKey or self.accessToken) else "pending",
            "engine": self.engine,
            "domain": self.domain,
            "proxy": self.proxy,
            "note": self.note,
            "cookies": self.cookies,
            "createdAt": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        }


class MoonAIProtocol:
    """纯 HTTP 引擎。一个实例负责一个账号的全流程。"""

    def __init__(
        self,
        *,
        config,
        persona: Persona,
        proxy: str = "",
        log=None,
    ):
        self.config = config
        self.persona = persona
        self.proxy = proxy
        self.log = log or (lambda *a, **k: None)
        self.auth = config.base_url or "https://auth.mooonai.com"
        self.core = config.api_base or config.core_url or "https://core.mooonai.com"
        self.session = Session(
            persona=persona,
            proxy=proxy,
            timeout=config.timeout,
            retries=config.retries,
            log=self.log,
        )

    # ------------------------------------------------------------------ 工具
    def _csrf(self, url: str, *, referer: str = "") -> tuple[str, str]:
        res = self.session.get(url, headers=self.persona.headers(referer=referer))
        if res.status_code >= 400:
            raise Blocked(f"打不开 {url}（{res.status_code}）", status=res.status_code, detail=res.text[:160])
        match = CSRF_RE.search(res.text)
        if not match:
            raise UpstreamError(f"{url} 里没有 csrfmiddlewaretoken（页面结构变了？）")
        return match.group(1), res.text

    def _form_errors(self, html: str) -> list[str]:
        return [m.group(1).strip() for m in ERROR_RE.finditer(html or "")][:5]

    def _classify_form_error(self, messages: list[str]) -> MoonAIError:
        blob = " ".join(messages).lower()
        if any(hint in blob for hint in EMAIL_TAKEN_HINTS):
            return EmailTaken(f"邮箱已被占用：{messages[:2]}")
        if any(hint in blob for hint in EMAIL_REJECTED_HINTS):
            return EmailRejected(f"邮箱被站点拒绝：{messages[:2]}")
        return EmailRejected(f"注册表单被拒：{messages[:2] or '未知原因'}")

    # ------------------------------------------------------------- 1. 注册
    def signup(self, email: str, password: str, username: str) -> None:
        signup_url = f"{self.auth}{SIGNUP_PATH}"
        token, _ = self._csrf(signup_url)
        res = self.session.form_post(
            signup_url,
            {
                "csrfmiddlewaretoken": token,
                "username": username,
                "email": email,
                "password1": password,
                # 表单里这两个是隐藏且默认勾选的；不提交也能过，但提交更像真人
                "agree_to_data_collection_and_processing": "on",
                "agree_to_terms": "on",
            },
            origin=self.auth,
            referer=signup_url,
        )
        if res.status_code in (301, 302, 303, 307):
            location = _redirect_location(res, signup_url)
            self.log(f"注册成功 → {location}")
            return
        messages = self._form_errors(res.text)
        raise self._classify_form_error(messages)

    # ------------------------------------------------------------- 2. 验证
    def verify_email(self, link: str) -> str:
        if not link:
            raise NoToken("没有拿到验证链接。")
        res = self.session.get(link, headers=self.persona.headers(referer=f"{self.auth}/auth/signup/"))
        if res.status_code >= 400:
            raise UpstreamError(f"验证链接失败（{res.status_code}）", status=res.status_code)
        return _redirect_location(res, link)

    # ------------------------------------------------------------- 3. 登录
    def signin(self, email: str, password: str) -> None:
        signin_url = f"{self.auth}{SIGNIN_PATH}"
        token, _ = self._csrf(signin_url)
        res = self.session.form_post(
            signin_url,
            {"csrfmiddlewaretoken": token, "username": email, "password": password},
            origin=self.auth,
            referer=signin_url,
        )
        if res.status_code in (301, 302, 303, 307):
            return
        messages = self._form_errors(res.text)
        raise LoginRejected(f"登录被拒：{messages[:2] or '密码或账号不被接受'}")

    # ------------------------------------------------------------- 4. OAuth
    def oauth_callback(self) -> str:
        """走一遍 authorize，返回带 code 的回调 URL。"""
        res = self.session.json_call("GET", f"{self.core}/api/v1/auth/url", origin=self.core, referer=f"{self.core}/")
        data = _json(res)
        auth_url = data.get("auth_url") or ""
        if not auth_url:
            raise UpstreamError(f"auth/url 没返回 auth_url：{json.dumps(data)[:200]}")

        hop = self.session.get(auth_url, headers=self.persona.headers(referer=f"{self.core}/"))
        location = _redirect_location(hop, auth_url)
        # 未登录 → authenticate 会跳到 /auth/signin/?next=…
        for _ in range(3):
            if not location:
                raise UpstreamError(f"authorize 没有跳转（HTTP {hop.status_code}）", status=hop.status_code)
            if "code=" in location:
                return location
            if "/auth/signin" in location or "/auth/signup" in location:
                # 会话丢了：重新登录一次再走
                self.signin(self.email_for_retry, self.password_for_retry)
                hop = self.session.get(auth_url, headers=self.persona.headers(referer=f"{self.core}/"))
                location = _redirect_location(hop, auth_url)
                continue
            hop = self.session.get(location, headers=self.persona.headers(referer=auth_url))
            location = _redirect_location(hop, location)
        raise NoToken("authorize 链路没有回到带 code 的回调。")

    # ------------------------------------------------------------- 5. 令牌
    def exchange_token(self, callback_url: str) -> dict:
        res = self.session.form_post(
            f"{self.core}{TOKEN_PATH}",
            {"auth_response_url": callback_url},
            origin=self.core,
            referer=callback_url,
        )
        data = _json(res)
        if not data.get("access_token"):
            raise NoToken(f"令牌交换失败：{json.dumps(data)[:200]}")
        return data

    def userinfo(self, access_token: str) -> dict:
        res = self.session.json_call("POST", f"{self.core}{USERINFO_PATH}", token=access_token, origin=self.core)
        return _json(res)

    # ------------------------------------------------------------- 6. 组织
    def ensure_organization(self, access_token: str, *, name: str = "") -> tuple[str, str]:
        res = self.session.json_call("GET", f"{self.core}{ORGS_PATH}", token=access_token, origin=self.core)
        data = _json(res)
        rows = data.get("results") if isinstance(data.get("results"), list) else []
        if rows:
            first = rows[0] or {}
            return str(first.get("id") or ""), str(first.get("name") or "")
        create = self.session.json_call(
            "POST",
            f"{self.core}{ORG_CREATE_PATH}",
            payload={"name": name or f"Moon {self.persona.first_name}"},
            token=access_token,
            origin=self.core,
            referer=f"{self.core}/",
        )
        created = _json(create)
        org_id = str(created.get("id") or "")
        if not org_id:
            raise UpstreamError(f"建组织失败：{json.dumps(created)[:200]}")
        return org_id, str(created.get("name") or "")

    # ------------------------------------------------------------- 7. API Key
    def create_api_key(self, access_token: str, org_id: str, *, name: str) -> dict:
        res = self.session.json_call(
            "POST",
            f"{self.core}{API_KEYS_PATH.format(org=org_id)}",
            payload={"name": name},
            token=access_token,
            origin=self.core,
            referer=f"{self.core}/",
        )
        data = _json(res)
        if not data.get("key"):
            raise NoApiKey(f"api-keys 没返回 key：{json.dumps(data)[:200]}")
        return data

    def list_api_keys(self, access_token: str, org_id: str) -> list[dict]:
        res = self.session.json_call(
            "GET",
            f"{self.core}{API_KEYS_PATH.format(org=org_id)}",
            token=access_token,
            origin=self.core,
        )
        data = _json(res)
        return data.get("api_keys") or []

    # ------------------------------------------------------------- 8. 自检
    def probe_llm(self, access_token: str, *, model: str = "") -> dict:
        model = model or self.config.model or DEFAULT_MODEL
        if model not in ALLOWED_MODELS:
            model = DEFAULT_MODEL
        started = time.time()
        try:
            res = self.session.json_call(
                "POST",
                f"{self.core}{LLM_PROXY_PATH}",
                payload={"model": model, "messages": [{"role": "user", "content": "ping"}], "max_tokens": 8},
                token=access_token,
                origin=self.core,
            )
            body = res.text[:220]
            ok = res.status_code == 200 and '"content"' in body
            return {"ok": ok, "status": res.status_code, "body": body, "ms": int((time.time() - started) * 1000)}
        except MoonAIError as cause:
            return {"ok": False, "status": getattr(cause, "status", 0), "body": str(cause)[:220], "ms": int((time.time() - started) * 1000)}

    # ------------------------------------------------------------- 主流程
    def run(
        self,
        *,
        email: str,
        password: str,
        username: str,
        verify_link: str = "",
        keys_per_account: int = 1,
        key_name: str = "openai-sdk",
        probe: bool = True,
    ) -> Registration:
        self.email_for_retry = email
        self.password_for_retry = password

        registration = Registration(
            email=email,
            password=password,
            username=username,
            name=f"{self.persona.first_name} {self.persona.last_name}",
            domain=email.split("@", 1)[-1] if "@" in email else "",
            proxy=self.proxy,
            note=self.persona.note,
        )

        self.log("step signup")
        self.signup(email, password, username)

        if verify_link:
            self.log("step verify")
            self.verify_email(verify_link)

        self.log("step signin")
        self.signin(email, password)

        self.log("step oauth")
        callback = self.oauth_callback()
        data = self.exchange_token(callback)
        registration.accessToken = str(data.get("access_token") or "")
        registration.refreshToken = str(data.get("refresh_token") or "")
        expires_in = int(data.get("expires_in") or 86400)
        registration.accessTokenExpiresAt = int((time.time() + expires_in) * 1000)

        info = self.userinfo(registration.accessToken)
        registration.subject = str(info.get("sub") or "")
        registration.emailVerified = bool(info.get("email_verified"))

        self.log("step organization")
        org_id, org_name = self.ensure_organization(registration.accessToken)
        registration.organizationId = org_id
        registration.organizationName = org_name

        self.log("step api-key")
        first = self.create_api_key(registration.accessToken, org_id, name=key_name)
        registration.apiKey = str(first.get("key") or "")
        registration.keyId = str(first.get("id") or "")
        for index in range(1, max(1, keys_per_account)):
            extra = self.create_api_key(registration.accessToken, org_id, name=f"{key_name}-{index + 1}")
            registration.extraKeys.append({"key": extra.get("key"), "id": extra.get("id"), "source": "created"})

        registration.cookies = [
            {"name": cookie.name, "value": cookie.value, "domain": cookie.domain or ""}
            for cookie in self.session.client.cookies.jar
        ]
        registration.status = "active"

        if probe:
            self.log("step probe")
            registration.probe = self.probe_llm(registration.accessToken)
            if not registration.probe.get("ok"):
                self.log(f"自检失败：{registration.probe.get('body', '')[:120]}")

        return registration

    def close(self) -> None:
        self.session.close()
