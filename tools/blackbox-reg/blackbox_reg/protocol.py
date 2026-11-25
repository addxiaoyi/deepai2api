"""Blackbox AI 站点协议（从线上前端与 CLI 还原，2026-11 取证）。

## 关键事实

1. **没有邮箱密码注册**。`app.blackbox.ai` 是 Auth.js（NextAuth v5），
   `GET /api/auth/providers` 只返回 Google；`POST /api/auth/sign-up/email`
   恒 400 `"Bad request."`（三种 body 组合都试过）。所以"注册"= Google OAuth 授权。
2. 授权链路（全部实测）：
       GET  /api/auth/csrf                      -> {"csrfToken":"…"}
       POST /api/auth/signin/google             （form: csrfToken, callbackUrl）
            -> 302 accounts.google.com/o/oauth2/v2/auth?client_id=985058387028-…&
               redirect_uri=https://app.blackbox.ai/api/auth/callback/google&
               code_challenge_method=S256&scope=openid profile email
       GET  /api/auth/callback/google?code=&state=   -> Set-Cookie __Secure-authjs.session-token
       GET  /api/auth/session                   -> {"user":{id,email,name,…},"expires":…}
   Auth.js 自己带 PKCE（cookie `__Secure-authjs.pkce.code_verifier`）+ state cookie，
   服务端校验，所以不能手搓 code——必须让 app 侧的 cookie jar 完整走一遍。
3. **面板 Key**（`sk-…`）没有公开接口文档。这里用"候选路径探测 + 命中即缓存"：
   一次探测结果写进 `<out>/key-endpoints.json`，后续复用；也可 `--keys-path` 指定。
4. **推理**是标准 OpenAI 形状，`Authorization: Bearer sk-…`，
   `enterprise.blackbox.ai` 的 `/chat/completions` 与 `/v1/chat/completions` 都在
   （无 key/错 key 回 `{"error":{"message":"blackbox.Error: Invalid API key","type":"auth_error"}}`）。

## 三条腿

    protocol   吃"已登录会话"或"Google cookies / 授权码"——最快，纯 HTTP
    browser    真浏览器过 Google 登录 + 从面板 DOM 读 Key——最稳，慢
    console    console.blackbox.ai 的 Email OTP + Turnstile——需要公司已开通
"""

from __future__ import annotations

import json
import os
import re
import time
from dataclasses import dataclass, field
from typing import Any, Callable
from urllib.parse import parse_qs, urlparse

from . import config as C
from .errors import (
    BlackboxError,
    Blocked,
    KeyNotObtainable,
    NoApiKey,
    OAuthDenied,
    PlanRequired,
    SessionExpired,
    UpstreamError,
    VerificationPending,
)
from .identity import GoogleIdentity, Persona, totp_now
from .net import Session, apply_cookie_rows, host_of

KEY_RE = re.compile(r"\bsk-[A-Za-z0-9_\-]{16,}\b")
KEY_ROW_KEYS = ("keys", "data", "apiKeys", "items", "results")


@dataclass
class Registration:
    """一条注册结果（`to_record()` 就是推给网关的账号对象）。"""

    email: str
    api_key: str = ""
    key_id: str = ""
    key_prefix: str = ""
    key_last4: str = ""
    key_source: str = ""
    extra_keys: list[dict] = field(default_factory=list)
    plan: str = ""
    user_id: str = ""
    engine: str = "protocol"
    cookies: list[dict] = field(default_factory=list)
    note: str = ""
    model: str = ""

    def to_record(self) -> dict[str, Any]:
        return {
            "email": self.email,
            "password": "",
            "name": "",
            "apiKey": self.api_key,
            "keyId": self.key_id,
            "keyPrefix": self.key_prefix or (self.api_key[:8] if self.api_key else ""),
            "keyLast4": self.key_last4 or (self.api_key[-4:] if self.api_key else ""),
            "keySource": self.key_source,
            "extraKeys": self.extra_keys,
            "plan": self.plan,
            "userId": self.user_id,
            "engine": self.engine,
            "cookies": self.cookies,
            "note": self.note,
            "model": self.model,
            "status": "active",
            "createdAt": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        }


def _flatten_keys(payload: Any) -> list[dict]:
    """把各种形状的 Key 列表折成统一的 [{id,key,prefix,last4,name}]。"""
    rows: list[Any] = []
    if isinstance(payload, list):
        rows = payload
    elif isinstance(payload, dict):
        for key in KEY_ROW_KEYS:
            value = payload.get(key)
            if isinstance(value, list):
                rows = value
                break
        if not rows:
            # 可能直接就是一把 key 对象
            rows = [payload]
    out: list[dict] = []
    for row in rows:
        if not isinstance(row, dict):
            continue
        plain = str(row.get("key") or row.get("token") or row.get("value") or row.get("secret") or "")
        prefix = str(row.get("keyPrefix") or row.get("prefix") or row.get("start") or "")
        last4 = str(row.get("last4") or row.get("lastFour") or row.get("suffix") or "")
        if not plain:
            # 只能看到前缀/后四位：拼成一个"占位"值，别让上层以为拿到了真 key
            plain = ""
        out.append(
            {
                "id": str(row.get("id") or row.get("_id") or row.get("keyId") or ""),
                "key": plain,
                "prefix": prefix,
                "last4": last4,
                "name": str(row.get("name") or row.get("displayName") or row.get("label") or ""),
                "createdAt": str(row.get("createdAt") or row.get("created_at") or ""),
            }
        )
    return out


def _find_key_string(payload: Any) -> str:
    """在任意 JSON 里找第一个 `sk-…`（面板"只显示一次"时它会出现在响应体里）。"""
    try:
        text = json.dumps(payload, ensure_ascii=False)
    except (TypeError, ValueError):
        text = str(payload)
    match = KEY_RE.search(text)
    return match.group(0) if match else ""


class BlackboxProtocol:
    """一个账号 = 一个 cookie jar = 一个 persona。"""

    def __init__(
        self,
        *,
        config: Any,
        persona: Persona,
        proxy: str = "",
        log: Callable[[str], None] | None = None,
    ) -> None:
        self.config = config
        self.persona = persona
        self.base = str(config.base_url).rstrip("/")
        self.api_base = str(config.api_base).rstrip("/")
        self.log = log or (lambda _m: None)
        self.session = Session(
            persona=persona,
            proxy=proxy,
            timeout=config.timeout,
            retries=config.retries,
            log=log,
        )
        self._endpoint_cache: dict[str, Any] = {}
        self._endpoint_cache_path = os.path.join(str(config.out_dir), "key-endpoints.json")
        self._endpoint_cache_loaded = False

    # ------------------------------------------------------------------ 生命周期
    def close(self) -> None:
        self.session.close()

    def __enter__(self) -> "BlackboxProtocol":
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()

    # ------------------------------------------------------------------ 预检
    def warm(self) -> None:
        res = self.session.get(f"{self.base}/login", headers=self.persona.headers())
        if res.status_code >= 500:
            raise UpstreamError(f"登录页 HTTP {res.status_code}")

    def providers(self) -> dict:
        """列登录方式。站点改版/路由挪走时返回 {}，由调用方决定怎么继续。"""
        try:
            res = self.session.get(
                f"{self.base}{C.PROVIDERS_PATH}",
                headers={"Accept": "application/json", "User-Agent": self.persona.user_agent},
                allow_auth_failure=True,
            )
        except BlackboxError as cause:
            self.log(f"providers 不可用：{cause.kind}（继续按 Google 流程走）")
            return {}
        if res.status_code >= 400:
            return {}
        try:
            payload = res.json()
        except Exception:  # noqa: BLE001
            payload = {}
        return payload if isinstance(payload, dict) else {}

    def csrf(self) -> str:
        res = self.session.get(
            f"{self.base}{C.CSRF_PATH}",
            headers={
                "Accept": "application/json",
                "User-Agent": self.persona.user_agent,
                "Referer": f"{self.base}/login",
            },
        )
        try:
            payload = res.json()
        except Exception:  # noqa: BLE001
            payload = {}
        token = str((payload or {}).get("csrfToken") or "")
        if not token:
            raise SessionExpired(f"拿不到 csrfToken（HTTP {res.status_code}）")
        return token

    # ------------------------------------------------------------------ OAuth
    def begin_google_oauth(self, callback_url: str = "") -> str:
        """POST /api/auth/signin/google，返回 Google 授权页 URL。"""
        token = self.csrf()
        res = self.session.form_post(
            f"{self.base}{C.SIGNIN_PATH}",
            {"csrfToken": token, "callbackUrl": callback_url or f"{self.base}/"},
            origin=self.base,
            referer=f"{self.base}/login",
            expected=(200, 302, 303, 307),
        )
        location = res.headers.get("location") or ""
        if res.status_code in (200, 204) and not location:
            # Auth.js 在 XHR 模式下回 JSON
            try:
                payload = res.json()
                location = str((payload or {}).get("url") or "")
            except Exception:  # noqa: BLE001
                location = ""
        if not location:
            raise SessionExpired("signin/google 没有返回跳转地址（Auth.js 配置可能变了）")
        if "accounts.google.com" not in location:
            # 已经是登录态：Auth.js 直接回跳到 callback，把 code 带回来
            return location
        return location

    def exchange_callback(self, callback_url: str) -> dict:
        """GET /api/auth/callback/google?code=&state=，落地 session cookie。"""
        res = self.session.get(
            callback_url,
            headers=self.persona.headers(referer=self.base),
            expected=(200, 302, 303, 307),
        )
        location = res.headers.get("location") or ""
        if "error=" in location:
            raise OAuthDenied(f"回调报错：{location[:160]}")
        if not self.session.has_session_cookie():
            # 有些部署会在 302 之后才 Set-Cookie，再看一眼 session
            info = self.session_info()
            if not info:
                raise SessionExpired("回调走完但没拿到 session cookie")
        return self.session_info() or {}

    def follow_google(self, authorize_url: str, *, identity: GoogleIdentity | None = None, timeout: float = 60.0) -> str:
        """在已登录的 Google 会话里走完授权，拿到回跳 URL（含 code/state）。

        纯 HTTP 只处理"Google 认为你已经登录且已同意"的情况：
        - 302 直接回 callback           → 成功
        - 停在 200 的同意页 / 密码页    → 需要浏览器腿（或者人工给一次授权码）
        """
        url = authorize_url
        for _ in range(8):
            res = self.session.get(url, headers=self.persona.headers(referer=self.base), expected=(200, 302, 303))
            if res.status_code in (302, 303):
                location = res.headers.get("location") or ""
                if not location:
                    break
                if "app.blackbox.ai" in location and "/api/auth/callback/google" in location:
                    return location
                if location.startswith("/"):
                    location = f"{self.base}{location}"
                url = location
                continue
            body = (res.text or "")[:4000]
            if "consent" in body.lower() or "approve" in body.lower():
                raise OAuthDenied("Google 停在同意页：请用 browser 引擎，或人工授权一次后复用 cookies")
            raise OAuthDenied("Google 停在登录/风控页：这个身份需要浏览器腿（或补 TOTP / cookies）")
        raise OAuthDenied(f"Google 授权未完成（后台 {timeout:.0f}s 内没有回跳）")

    def oauth_with_cookies(self, identity: GoogleIdentity) -> dict:
        if identity.cookies:
            applied = apply_cookie_rows(self.session.client, identity.cookies, default_domain=host_of(self.base))
            self.log(f"已注入 Google cookies {applied} 条")
        authorize_url = self.begin_google_oauth()
        self.log(f"授权入口：{authorize_url[:110]}…")
        callback = self.follow_google(authorize_url, identity=identity)
        return self.exchange_callback(callback)

    def oauth_with_code(self, code: str, state: str = "") -> dict:
        if not state:
            raise OAuthDenied("手工授权码必须同时提供 state（从回调 URL 里复制）")
        callback = f"{self.base}{C.CALLBACK_PATH}?code={code}&state={state}"
        return self.exchange_callback(callback)

    # ------------------------------------------------------------------ 会话
    def session_info(self) -> dict:
        res = self.session.get(
            f"{self.base}{C.SESSION_PATH}",
            headers={"Accept": "application/json", "User-Agent": self.persona.user_agent},
            allow_auth_failure=True,
        )
        if res.status_code >= 400:
            return {}
        try:
            payload = res.json()
        except Exception:  # noqa: BLE001
            return {}
        return payload if isinstance(payload, dict) else {}

    def challenge(self) -> dict:
        res = self.session.get(
            f"{self.base}{C.CHALLENGE_PATH}",
            headers={"Accept": "application/json", "User-Agent": self.persona.user_agent},
            allow_auth_failure=True,
        )
        try:
            payload = res.json()
        except Exception:  # noqa: BLE001
            payload = {}
        return payload if isinstance(payload, dict) else {}

    def plan(self) -> str:
        info = self.session_info()
        user = info.get("user") or info.get("account") or {}
        for key in ("plan", "tier", "subscription", "product", "role"):
            value = user.get(key)
            if isinstance(value, str) and value:
                return value
            if isinstance(value, dict):
                for inner in ("name", "slug", "id", "tier"):
                    if value.get(inner):
                        return str(value[inner])
        return ""

    # ------------------------------------------------------------------ Key
    def _load_endpoint_cache(self) -> None:
        if self._endpoint_cache_loaded:
            return
        self._endpoint_cache_loaded = True
        path = self._endpoint_cache_path
        if not path or not os.path.exists(path):
            return
        try:
            with open(path, encoding="utf-8") as handle:
                data = json.load(handle)
            if isinstance(data, dict):
                self._endpoint_cache = data
        except (OSError, json.JSONDecodeError):
            self._endpoint_cache = {}

    def _save_endpoint_cache(self) -> None:
        path = self._endpoint_cache_path
        if not path:
            return
        try:
            os.makedirs(os.path.dirname(os.path.abspath(path)) or ".", exist_ok=True)
            with open(path, "w", encoding="utf-8") as handle:
                json.dump(self._endpoint_cache, handle, ensure_ascii=False, indent=2)
        except OSError:
            pass

    @property
    def _learned_path(self) -> str:
        self._load_endpoint_cache()
        return str(self._endpoint_cache.get("base") or "") == self.base and str(self._endpoint_cache.get("list") or "") or ""

    def _try_list(self, path: str) -> tuple[bool, list[dict], Any]:
        method = (self.config.keys_method or "GET").upper()
        try:
            if method == "POST":
                res = self.session.json_call(
                    "POST",
                    f"{self.base}{path}",
                    payload={},
                    origin=self.base,
                    referer=f"{self.base}/keys",
                    allow_auth_failure=True,
                )
            else:
                res = self.session.get(
                    f"{self.base}{path}",
                    headers={"Accept": "application/json", "User-Agent": self.persona.user_agent},
                    allow_auth_failure=True,
                )
        except UpstreamError as cause:
            # 404/405 = 这个候选路径不存在，继续试下一个
            if (cause.status or 0) in (401, 403, 404, 405, 422):
                return False, [], None
            raise
        if res.status_code in (401, 403, 404, 405):
            return False, [], None
        if res.status_code >= 400:
            return False, [], None
        try:
            payload = res.json()
        except Exception:  # noqa: BLE001
            return False, [], None
        rows = _flatten_keys(payload)
        # 判据：拿到 key 对象，或响应里至少有一个 sk-
        if rows or _find_key_string(payload):
            return True, rows, payload
        return False, [], payload

    def list_keys(self) -> list[dict]:
        """列面板 Key；探测结果缓存到 out/key-endpoints.json。"""
        explicit = str(getattr(self.config, "keys_path", "") or "")
        candidates = [explicit] if explicit else ([self._learned_path] if self._learned_path else []) + C.KEY_PATHS
        seen: set[str] = set()
        for path in candidates:
            if not path or path in seen:
                continue
            seen.add(path)
            ok, rows, _payload = self._try_list(path)
            if not ok:
                continue
            self._endpoint_cache = {"base": self.base, "list": path, "method": self.config.keys_method or "GET"}
            self._save_endpoint_cache()
            self.log(f"面板 Key 接口锁定：{path}（{len(rows)} 把）")
            return rows
        raise KeyNotObtainable(
            "没找到面板 Key 接口。用 --keys-path 指定（浏览器腿可以自动从 DOM 读，不依赖这个接口）"
        )

    def create_key(self, name: str) -> tuple[str, dict]:
        """建一把 Key，返回 (plaintext, row)。"""
        explicit = str(getattr(self.config, "keys_create_path", "") or "")
        list_path = self._learned_path or (explicit or "")
        paths = [explicit] if explicit else ([list_path] if list_path else []) + C.KEY_PATHS
        bodies = []
        if getattr(self.config, "keys_create_body", ""):
            try:
                template = json.loads(self.config.keys_create_body)
                if isinstance(template, dict):
                    bodies.append(template)
            except json.JSONDecodeError:
                pass
        bodies.extend(
            [
                {"name": name},
                {"displayName": name},
                {"label": name},
                {"name": name, "scopes": ["chat"]},
            ]
        )
        seen: set[str] = set()
        for path in paths:
            if not path or path in seen:
                continue
            seen.add(path)
            for body in bodies:
                try:
                    res = self.session.json_call(
                        "POST",
                        f"{self.base}{path}",
                        payload=body,
                        origin=self.base,
                        referer=f"{self.base}/keys",
                        allow_auth_failure=True,
                    )
                except UpstreamError as cause:
                    if (cause.status or 0) in (401, 403, 404, 405, 422):
                        break  # 这个路径不行，换下一个候选
                    continue
                except BlackboxError:
                    continue
                if res.status_code in (401, 403):
                    raise SessionExpired(f"建 Key 被拒（{res.status_code}），会话可能失效")
                if res.status_code >= 400:
                    continue
                try:
                    payload = res.json()
                except Exception:  # noqa: BLE001
                    payload = {}
                key = _find_key_string(payload)
                if key:
                    rows = _flatten_keys(payload)
                    row = rows[0] if rows else {}
                    self._endpoint_cache = {
                        "base": self.base,
                        "list": self._endpoint_cache.get("list") or path,
                        "create": path,
                        "method": self.config.keys_method or "GET",
                    }
                    self._save_endpoint_cache()
                    return key, row
        raise KeyNotObtainable("建 Key 的接口没命中任何候选路径（用 --keys-create-path 指定）")

    # ------------------------------------------------------------------ 验证
    def probe_chat(self, api_key: str, model: str = "") -> dict:
        """用刚拿到的 Key 打一次最小 chat，确认真的能用（反代层才不会白挂）。"""
        model = model or self.config.model
        try:
            res = self.session.json_call(
                "POST",
                f"{self.api_base}/v1/chat/completions",
                payload={
                    "model": model,
                    "messages": [{"role": "user", "content": "ping"}],
                    "max_tokens": 8,
                    "stream": False,
                },
                token=api_key,
                allow_auth_failure=True,
            )
        except BlackboxError as cause:
            return {"ok": False, "kind": cause.kind, "status": cause.status, "reason": str(cause)[:200]}
        body = ""
        try:
            body = res.text[:240]
        except Exception:  # noqa: BLE001
            pass
        return {"ok": res.status_code < 400, "status": res.status_code, "body": body, "model": model}

    # ------------------------------------------------------------------ 复合流程
    def run(
        self,
        identity: GoogleIdentity,
        *,
        keys: int = 1,
        key_name: str = "",
        authorization_code: str = "",
    ) -> Registration:
        self.warm()
        available = self.providers()
        if available and "google" not in available:
            self.log(f"警告：providers 里没有 google（{list(available)}），登录流程可能要调整")

        if identity.cookies and not self.session.has_session_cookie():
            apply_cookie_rows(self.session.client, identity.cookies, default_domain=host_of(self.base))

        info = self.session_info()
        if not info:
            if authorization_code:
                state = str(getattr(self.config, "authorization_code_state", "") or "")
                info = self.oauth_with_code(authorization_code, state)
            elif identity.cookies:
                info = self.oauth_with_cookies(identity)
            else:
                # 没有 app 会话、没有 Google cookies：纯 HTTP 走不了 Google 登录，
                # 直接报 session_expired 让上层切浏览器腿，别在这里空转重试。
                raise SessionExpired(
                    "没有可用的登录态：给 --session-file（app 会话）/ --google-cookies / --google-pool，"
                    "或者用 --engine browser 现场登录"
                )

        user = info.get("user") or info.get("account") or {}
        email = str(user.get("email") or identity.email or "").strip().lower()
        user_id = str(user.get("id") or user.get("userId") or "")
        plan = self.plan()
        self.log(f"登录态 OK：{email or '(no email)'} plan={plan or '-'}")

        keys_rows: list[dict] = []
        api_key = ""
        key_id = ""
        key_source = ""
        try:
            keys_rows = self.list_keys()
        except KeyNotObtainable as cause:
            self.log(f"列 Key 失败：{cause}（继续尝试直接建 Key）")

        for row in keys_rows:
            if row.get("key"):
                api_key = row["key"]
                key_id = row.get("id", "")
                key_source = "listed"
                break

        if not api_key:
            # 面板"只显示一次"，列不出来就建一把新的
            name = key_name or f"{email.split('@')[0] or 'bbx'}-{time.strftime('%Y%m%d')}"
            api_key, row = self.create_key(name)
            key_id = row.get("id") or key_id
            key_source = "created"

        if not api_key:
            raise NoApiKey("登录成功但没拿到 Key（可能是免费档不允许建 Key，或接口变了）")

        prefix = api_key[:8]
        last4 = api_key[-4:]
        for row in keys_rows:
            if row.get("prefix") and api_key.startswith(row["prefix"]):
                key_id = key_id or row.get("id", "")
                last4 = row.get("last4") or last4

        extra: list[dict] = []
        for index in range(max(0, keys - 1)):
            try:
                more, _row = self.create_key(f"{email.split('@')[0] or 'bbx'}-{index + 2}")
            except (BlackboxError, KeyNotObtainable):
                break
            if more and more != api_key:
                extra.append({"key": more, "source": "created"})

        if self.config.probe:
            result = self.probe_chat(api_key)
            if result.get("ok"):
                self.log(f"Key 可用（{result.get('status')}）")
            else:
                self.log(f"Key 自检未通过：{result}")
                if result.get("status") == 403:
                    raise PlanRequired(f"Key 无推理权限：{result.get('body','')[:160]}")

        return Registration(
            email=email,
            api_key=api_key,
            key_id=key_id,
            key_prefix=prefix,
            key_last4=last4,
            key_source=key_source,
            extra_keys=extra,
            plan=plan,
            user_id=user_id,
            engine="protocol",
            cookies=self.session.cookie_rows(),
            model=self.config.model,
            note=identity.note,
        )


def probe(config: Any, *, proxy: str = "", log=None) -> dict[str, Any]:
    """`doctor` 用：只做无害 GET，确认站点与 Auth.js 还在原位。"""
    from .identity import make_persona

    out: dict[str, Any] = {}
    with Session(persona=make_persona(seed="doctor"), proxy=proxy, timeout=20.0, log=log) as session:
        for name, url in (
            ("login", f"{config.base_url}/login"),
            ("providers", f"{config.base_url}{C.PROVIDERS_PATH}"),
            ("csrf", f"{config.base_url}{C.CSRF_PATH}"),
        ):
            try:
                res = session.get(url, headers={"Accept": "application/json,text/html"})
                out[name] = f"HTTP {res.status_code} {len(res.text or '')}b"
            except BlackboxError as cause:
                out[name] = f"FAIL {cause.kind}: {cause}"
    try:
        import httpx

        res = httpx.post(
            f"{config.api_base}/v1/chat/completions",
            headers={"Content-Type": "application/json", "Authorization": "Bearer sk-invalid"},
            json={"model": config.model, "messages": [{"role": "user", "content": "ping"}], "max_tokens": 8},
            timeout=20,
            trust_env=False,
        )
        out["inference"] = f"HTTP {res.status_code} {(res.text or '')[:120]}"
    except Exception as cause:  # noqa: BLE001
        out["inference"] = f"FAIL {cause}"
    return out
