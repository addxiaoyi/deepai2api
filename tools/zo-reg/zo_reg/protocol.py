"""协议引擎：魔法链登录 → 建工作区 → 签发 access token → 自检。

全部端点在线上实测过（取证表见 docs/ZO-POOL.md，原始抓包在 tools/zo-reg/_re/）：

    1. GET  {panel}/signup                              预热 cookie（Cloudflare __cf_bm）
    2. POST {panel}/api/email-login/request              {"email", "redirect", "desktop"?}
                                                         → 200 {"ok": true}
    3. (邮箱) 收到 https://www.zo.computer/email-login/verify?token=…
    4. POST {panel}/api/email-login/confirm?token=…      （浏览器腿带 turnstileToken）
                                                         → 200 {"redirect": "/signup"} + Set-Cookie
    5. GET  {panel}/api/login-state                      {"claims": {...}, "workspaces": [...]}
    6. GET  {api}/signup/{handle}/available              {"available": true}
       POST {api}/signup/                                {"handle": …}（带 cookie）
    7. GET  {api}/api-keys/                              （列已有 token）
       POST {api}/api-keys/                               {"name": …} → {"key"/"token": "zo_sk_…"}
    8. GET  {api}/models/available                       Bearer 自检
       POST {api}/mcp  initialize/tools/list             顺带验证 MCP

注意第 7 步的凭据是 **access token（zo_sk_…）**，不是 OpenAI Key；
调模型只有 `/zo/ask` 一个入口 —— 这就是反代层 `relay.kind = "zo-ask"` 的原因。
"""

from __future__ import annotations

import json
import re
import time
from dataclasses import dataclass, field

from .config import (
    API_KEYS_PATH,
    ASK_PATH,
    CATALOG_PATH,
    CONFIRM_PATH,
    DEFAULT_MODEL,
    EMAIL_LOGIN_PATH,
    LOGIN_STATE_PATH,
    MCP_PATH,
    MODELS_PATH,
    PERSONAS_PATH,
    SIGNUP_AVAILABLE_PATH,
    SIGNUP_PATH,
    VERIFY_PAGE_PATH,
)
from .errors import (
    Blocked,
    CaptchaRequired,
    EmailRejected,
    EmailTaken,
    HandleTaken,
    NoApiKey,
    NoToken,
    UpstreamError,
)
from .identity import Persona, handle_for
from .net import Session

TOKEN_FIELD_CANDIDATES = ("key", "token", "api_key", "apiKey", "secret", "value")


def _json(res) -> dict:
    try:
        data = res.json()
    except (json.JSONDecodeError, ValueError) as cause:
        raise UpstreamError(f"上游返回非 JSON（HTTP {res.status_code}）：{res.text[:160]}") from cause
    return data if isinstance(data, dict) else {"data": data}


def _redirect_location(res, base: str) -> str:
    location = res.headers.get("location") or ""
    if not location:
        return ""
    if location.startswith("http"):
        return location
    return f"{base.rstrip('/')}/{location.lstrip('/')}"


@dataclass
class Registration:
    """一次注册产出的全部凭据。"""

    email: str
    handle: str
    name: str = ""
    # 对外凭据：access token（zo_sk_…）
    apiKey: str = ""
    keyId: str = ""
    keyName: str = ""
    extraKeys: list[dict] = field(default_factory=list)
    # 会话凭据（续期 / 二次进面板要用）
    accessToken: str = ""
    refreshToken: str = ""
    accessTokenExpiresAt: int = 0
    cookies: list[dict] = field(default_factory=list)
    # 门面信息
    workspaceId: str = ""
    workspaceUrl: str = ""
    emailVerified: bool = False
    claims: dict = field(default_factory=dict)
    models: list[str] = field(default_factory=list)
    mcpTools: int = 0
    engine: str = "protocol"
    domain: str = ""
    proxy: str = ""
    note: str = ""
    probe: dict = field(default_factory=dict)

    @property
    def keyPrefix(self) -> str:
        return self.apiKey[:10] if self.apiKey else ""

    @property
    def keyLast4(self) -> str:
        return self.apiKey[-4:] if self.apiKey else ""

    def to_record(self) -> dict:
        return {
            "email": self.email,
            "name": self.name or self.handle,
            "handle": self.handle,
            "apiKey": self.apiKey,
            "keyId": self.keyId,
            "keyPrefix": self.keyPrefix,
            "keyLast4": self.keyLast4,
            "keySource": "api-keys" if self.apiKey else "",
            "extraKeys": self.extraKeys,
            "accessToken": self.accessToken or self.apiKey,
            "refreshToken": self.refreshToken,
            "accessTokenExpiresAt": self.accessTokenExpiresAt,
            "organizationId": self.workspaceId,
            "workspaceUrl": self.workspaceUrl,
            "emailVerified": self.emailVerified,
            "status": "active" if (self.apiKey or self.accessToken) else "pending",
            "engine": self.engine,
            "domain": self.domain,
            "proxy": self.proxy,
            "note": self.note,
            "cookies": self.cookies,
            "models": self.models[:40],
            "mcpTools": self.mcpTools,
            "claims": self.claims,
            "probe": self.probe,
            "createdAt": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        }


class ZoProtocol:
    """纯 HTTP 引擎。一个实例负责一个账号的全流程。"""

    def __init__(self, *, config, persona: Persona, proxy: str = "", log=None):
        self.config = config
        self.persona = persona
        self.proxy = proxy
        self.log = log or (lambda *a, **k: None)
        self.panel = (config.base_url or "https://www.zo.computer").rstrip("/")
        self.api = (config.api_base or "https://api.zo.computer").rstrip("/")
        self.session = Session(
            persona=persona,
            proxy=proxy,
            timeout=config.timeout,
            retries=config.retries,
            log=self.log,
        )

    # ------------------------------------------------------------- 0. 预热
    def warmup(self) -> None:
        """先访问首页/注册页，拿到 Cloudflare 与框架 cookie，别让第一个 POST 像脚本。"""
        for path in ("/", "/signup"):
            try:
                res = self.session.get(f"{self.panel}{path}", headers=self.persona.headers(referer=f"{self.panel}/"))
                self.log(f"预热 {path} → HTTP {res.status_code}")
            except Exception as cause:  # noqa: BLE001 - 预热失败不算致命
                self.log(f"预热 {path} 失败：{cause}", "warn")

    # ------------------------------------------------------ 1. 请求魔法链
    def request_magic_link(self, email: str) -> None:
        payload = {
            "email": email,
            "redirect": "/signup",
        }
        if self.persona.desktop:
            payload["desktop"] = True
        res = self.session.request(
            "POST",
            f"{self.panel}{EMAIL_LOGIN_PATH}",
            headers=self.persona.xhr_headers(origin=self.panel, referer=f"{self.panel}/signup"),
            json=payload,
        )
        if res.status_code in (401, 403):
            raise Blocked(f"魔法链请求被拒（{res.status_code}）", status=res.status_code, detail=res.text[:160])
        if res.status_code == 429:
            from .errors import RateLimited

            raise RateLimited("魔法链请求被限流（429）", status=429, detail=res.text[:160])
        if res.status_code >= 400:
            detail = res.text[:200]
            if "email" in detail.lower():
                raise EmailRejected(f"站点拒收这个邮箱：{detail}")
            raise UpstreamError(f"魔法链请求失败（{res.status_code}）：{detail}", status=res.status_code)
        try:
            data = res.json()
        except (json.JSONDecodeError, ValueError):
            data = {}
        if isinstance(data, dict) and data.get("ok") is False:
            raise EmailRejected(f"魔法链请求被拒：{json.dumps(data)[:160]}")
        self.log("魔法链已发出，等邮件…")

    # ------------------------------------------------------ 2. 确认魔法链
    def confirm_magic_link(self, token: str, *, turnstile_token: str = "") -> dict:
        """POST /api/email-login/confirm?token=…；成功即写入 cookie 会话。"""
        if not token:
            raise NoToken("没有魔法链 token。")
        body: dict = {}
        if turnstile_token:
            body["turnstileToken"] = turnstile_token
        res = self.session.request(
            "POST",
            f"{self.panel}{CONFIRM_PATH}",
            params={"token": token, **({"redirect": "/signup"} if self.persona.desktop else {})},
            headers=self.persona.xhr_headers(
                origin=self.panel, referer=f"{self.panel}{VERIFY_PAGE_PATH}?token={token}"
            ),
            json=body,
        )
        if res.status_code == 403:
            detail = res.text[:200]
            if "turnstile" in detail.lower() or "captcha" in detail.lower():
                raise CaptchaRequired(f"魔法链确认要人机校验：{detail}")
            raise NoToken(f"魔法链无效或已过期：{detail}")
        if res.status_code >= 400:
            raise UpstreamError(f"魔法链确认失败（{res.status_code}）：{res.text[:200]}", status=res.status_code)
        data = _json(res)
        # 会话可能在响应头 Set-Cookie 里；httpx 已经把 Domain 匹配的种进 jar
        self._absorb_cookies()
        if not self.session.cookies_dict().get("access_token") and not data.get("redirect"):
            self.log("确认成功但暂时看不到 access_token cookie（可能在 login-state 之后）", "warn")
        return data

    def _absorb_cookies(self) -> None:
        for cookie in self.session.client.cookies.jar:
            if cookie.name == "access_token":
                self._cookie_token = cookie.value

    # -------------------------------------------------------- 3. 会话状态
    def login_state(self) -> dict:
        res = self.session.request(
            "GET",
            f"{self.panel}{LOGIN_STATE_PATH}",
            headers={**self.persona.headers(referer=f"{self.panel}/signup"), "Accept": "application/json"},
        )
        if res.status_code >= 400:
            raise UpstreamError(f"login-state 失败（{res.status_code}）", status=res.status_code, detail=res.text[:160])
        data = _json(res)
        claims = data.get("claims") or {}
        workspaces = data.get("workspaces") or []
        if not claims and not workspaces:
            raise NoToken("会话没生效：login-state 里既没有 claims 也没有 workspaces。")
        return data

    # ---------------------------------------------------------- 4. 工作区
    def handle_available(self, handle: str) -> bool:
        res = self.session.request(
            "GET",
            f"{self.api}{SIGNUP_AVAILABLE_PATH.format(handle=handle)}",
            headers={**self.persona.headers(referer=f"{self.panel}/signup"), "Accept": "application/json"},
        )
        if res.status_code >= 500:
            raise UpstreamError(f"handle 探测失败（{res.status_code}）", status=res.status_code)
        data = _json(res) if res.status_code < 400 else {}
        return bool(data.get("available"))

    def create_workspace(self, *, handle: str) -> dict:
        payload = {"handle": handle}
        promo = self.config.key_policy.get("promoCode") if isinstance(self.config.key_policy, dict) else None
        if promo:
            payload["promo_code"] = str(promo)
        res = self.session.request(
            "POST",
            f"{self.api}{SIGNUP_PATH}",
            headers=self.persona.xhr_headers(origin=f"{self.panel}", referer=f"{self.panel}/signup"),
            json=payload,
        )
        if res.status_code in (401, 403):
            raise Blocked(f"建工作区被拒（{res.status_code}）", status=res.status_code, detail=res.text[:200])
        if res.status_code >= 400:
            detail = res.text[:220]
            lowered = detail.lower()
            if "taken" in lowered or "exist" in lowered or "unavailable" in lowered:
                raise HandleTaken(f"handle 已占用：{detail}")
            if "email" in lowered:
                raise EmailRejected(f"建工作区时邮箱被拒：{detail}")
            raise UpstreamError(f"建工作区失败（{res.status_code}）：{detail}", status=res.status_code)
        return _json(res)

    # ------------------------------------------------------- 5. access token
    def _extract_token(self, data: dict) -> str:
        for field_name in TOKEN_FIELD_CANDIDATES:
            value = data.get(field_name)
            if isinstance(value, str) and value.strip():
                return value.strip()
            if isinstance(value, dict):
                for nested in TOKEN_FIELD_CANDIDATES:
                    inner = value.get(nested)
                    if isinstance(inner, str) and inner.strip():
                        return inner.strip()
        return ""

    def list_api_keys(self) -> list[dict]:
        res = self.session.request(
            "GET",
            f"{self.api}{API_KEYS_PATH}",
            headers={**self.persona.headers(referer=f"{self.panel}/settings/advanced"), "Accept": "application/json"},
        )
        if res.status_code >= 400:
            return []
        data = _json(res)
        for key in ("api_keys", "keys", "tokens", "data", "items"):
            rows = data.get(key)
            if isinstance(rows, list):
                return [row for row in rows if isinstance(row, dict)]
        return data.get("results") if isinstance(data.get("results"), list) else []

    def create_api_key(self, *, name: str) -> dict:
        """POST /api-keys/ → 返回带明文的那个响应（只在创建时给一次）。"""
        res = self.session.request(
            "POST",
            f"{self.api}{API_KEYS_PATH}",
            headers=self.persona.xhr_headers(origin=f"{self.panel}", referer=f"{self.panel}/settings/advanced"),
            json={"name": name},
        )
        if res.status_code in (401, 403):
            raise NoToken(f"建 token 被拒（{res.status_code}，会话 cookie 可能没带上）：{res.text[:160]}")
        if res.status_code >= 400:
            raise NoApiKey(f"建 token 失败（{res.status_code}）：{res.text[:200]}")
        data = _json(res)
        token = self._extract_token(data)
        if not token:
            raise NoApiKey(f"api-keys 没返回 token：{json.dumps(data)[:220]}")
        return {"key": token, "id": str(data.get("id") or data.get("key_id") or ""), "raw": data}

    # ---------------------------------------------------------- 6. 自检
    def list_models(self) -> list[str]:
        res = self.session.request(
            "GET",
            f"{self.api}{MODELS_PATH}",
            headers={
                "User-Agent": self.persona.user_agent,
                "Accept": "application/json",
                "Accept-Language": self.persona.accept_language,
            },
        )
        if res.status_code >= 400:
            return []
        data = _json(res)
        return [str(row.get("model_name")) for row in (data.get("models") or []) if row.get("model_name")]

    def probe_ask(self, *, model: str = "") -> dict:
        started = time.time()
        try:
            res = self.session.request(
                "POST",
                f"{self.api}{ASK_PATH}",
                headers={
                    **self.persona.xhr_headers(origin=f"{self.panel}", referer=f"{self.panel}/"),
                },
                json={"input": "ping", "model_name": model or self.config.model or DEFAULT_MODEL, "stream": False},
            )
            body = res.text[:220]
            return {
                "ok": res.status_code == 200 and '"output"' in body,
                "status": res.status_code,
                "body": body,
                "ms": int((time.time() - started) * 1000),
            }
        except Exception as cause:  # noqa: BLE001
            return {"ok": False, "status": 0, "body": str(cause)[:220], "ms": int((time.time() - started) * 1000)}

    def probe_mcp(self) -> dict:
        started = time.time()
        try:
            res = self.session.request(
                "POST",
                f"{self.api}{MCP_PATH}",
                headers={
                    "User-Agent": self.persona.user_agent,
                    "Accept": "application/json, text/event-stream",
                    "Content-Type": "application/json",
                },
                json={
                    "jsonrpc": "2.0",
                    "id": 1,
                    "method": "initialize",
                    "params": {
                        "protocolVersion": "2025-06-18",
                        "capabilities": {},
                        "clientInfo": {"name": "zo-reg", "version": "0.1.0"},
                    },
                },
            )
            ok = res.status_code == 200 and "result" in res.text
            tools = 0
            if ok:
                listed = self.session.request(
                    "POST",
                    f"{self.api}{MCP_PATH}",
                    headers={
                        "User-Agent": self.persona.user_agent,
                        "Accept": "application/json, text/event-stream",
                        "Content-Type": "application/json",
                    },
                    json={"jsonrpc": "2.0", "id": 2, "method": "tools/list", "params": {}},
                )
                try:
                    tools = len((listed.json().get("result") or {}).get("tools") or [])
                except (json.JSONDecodeError, ValueError):
                    tools = 0
            return {"ok": ok, "status": res.status_code, "tools": tools, "ms": int((time.time() - started) * 1000)}
        except Exception as cause:  # noqa: BLE001
            return {"ok": False, "status": 0, "tools": 0, "reason": str(cause)[:200], "ms": int((time.time() - started) * 1000)}

    def catalog_size(self) -> int:
        try:
            res = self.session.request("GET", f"{self.api}{CATALOG_PATH}", headers={"Accept": "application/json"})
            if res.status_code >= 400:
                return 0
            return len((_json(res).get("models") or []))
        except Exception:  # noqa: BLE001
            return 0

    # ------------------------------------------------------------- 主流程
    def reserve_handle(self, *, preferred: str, attempts: int = 6) -> str:
        """先探占用；占用就换后缀。返回最终 handle。"""
        candidate = preferred
        for index in range(attempts):
            if self.handle_available(candidate):
                return candidate
            self.log(f"handle {candidate} 已占用，换一个", "warn")
            candidate = f"{preferred[:22]}{index + 2}{''.join(ch for ch in 'abcdefghijkm' if ch == 'abcdefghijkm'[index % 11])}"
        raise HandleTaken(f"连续 {attempts} 个 handle 都被占用：{preferred}")

    def run(
        self,
        *,
        email: str,
        token: str = "",
        handle: str = "",
        keys_per_account: int = 1,
        token_name: str = "gateway",
        probe: bool = True,
        mcp_probe: bool = False,
    ) -> Registration:
        registration = Registration(
            email=email,
            handle=handle or self.persona.handle,
            name=self.persona.username,
            domain=email.split("@", 1)[-1] if "@" in email else "",
            proxy=self.proxy,
            note=self.persona.note,
        )

        self.log("step warmup")
        self.warmup()

        if not token:
            self.log("step magic-link-request")
            self.request_magic_link(email)
            raise NoToken("魔法链已发出，等 runner 取到 token 再继续。")

        self.log("step magic-link-confirm")
        self.confirm_magic_link(token)

        self.log("step login-state")
        state = self.login_state()
        claims = state.get("claims") or {}
        workspaces = state.get("workspaces") or []
        registration.claims = {k: claims.get(k) for k in ("email", "sub", "name") if claims.get(k)}
        registration.emailVerified = bool(claims.get("email_verified", True))
        if workspaces:
            first = workspaces[0] if isinstance(workspaces[0], dict) else {}
            registration.workspaceId = str(first.get("id") or first.get("slug") or "")
            registration.workspaceUrl = str(first.get("url") or "")
            handle_from_state = str(first.get("handle") or first.get("slug") or "")
            if handle_from_state:
                registration.handle = handle_from_state

        if not workspaces:
            self.log("step signup")
            chosen = self.reserve_handle(preferred=registration.handle)
            created = self.create_workspace(handle=chosen)
            registration.handle = chosen
            registration.workspaceId = str(created.get("id") or created.get("workspace_id") or "")
            registration.workspaceUrl = str(created.get("url") or created.get("handle") or "")
            # 建完工作区再确认一次状态，顺便让 cookie 与 claims 落定
            try:
                state = self.login_state()
                registration.claims = {
                    **(registration.claims or {}),
                    **{k: (state.get("claims") or {}).get(k) for k in ("email", "sub") if (state.get("claims") or {}).get(k)},
                }
            except Exception as cause:  # noqa: BLE001
                self.log(f"建工作区后 login-state 复核失败（不致命）：{cause}", "warn")

        self.log("step api-key")
        existing = self.list_api_keys()
        first = self.create_api_key(name=token_name)
        registration.apiKey = first["key"]
        registration.keyId = first["id"]
        registration.keyName = token_name
        registration.extraKeys = [
            {"key": row.get("key") or "", "id": str(row.get("id") or ""), "source": "existing"}
            for row in existing
            if isinstance(row, dict)
        ][:5]
        for index in range(1, max(1, keys_per_account)):
            extra = self.create_api_key(name=f"{token_name}-{index + 1}")
            registration.extraKeys.append({"key": extra["key"], "id": extra["id"], "source": "created"})

        # 会话里的 token 与 api-key 分开记：反代层优先用 accessToken
        jar_token = self.session.cookies_dict().get("access_token") or ""
        registration.accessToken = jar_token or registration.apiKey
        registration.refreshToken = self.session.cookies_dict().get("refresh_token") or ""
        registration.accessTokenExpiresAt = int((time.time() + 86400) * 1000)
        registration.cookies = self.session.cookie_records()
        registration.status = "active"

        if probe:
            self.log("step probe")
            registration.models = self.list_models()[:40]
            registration.probe = self.probe_ask(model=self.config.model)
            if not registration.probe.get("ok"):
                self.log(f"自检失败：{registration.probe.get('body', '')[:120]}", "warn")
        if mcp_probe:
            self.log("step mcp")
            mcp = self.probe_mcp()
            registration.mcpTools = int(mcp.get("tools") or 0)
            registration.probe = {**(registration.probe or {}), "mcp": mcp}

        return registration

    def close(self) -> None:
        self.session.close()