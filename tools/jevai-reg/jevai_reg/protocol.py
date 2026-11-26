"""jev-ai.org 注册 + 建 Key 协议。

从站点行为还原出的三块：

**认证**（Better Auth，Next.js App Router）
    GET  /api/auth/get-session            -> null 或 {session,user}
    GET  /api/auth/ok                     -> {"ok":true}（健康探针）
    POST /api/auth/sign-up/email          {email,password,name}
    POST /api/auth/sign-in/email          {email,password}
    POST /api/auth/send-verification-email{email}
    GET  /api/auth/verify-email?token=…   邮件里的链接

注册对**一次性邮箱域名**是拒绝的（实测）：

    {"code":"EMAIL_DOMAIN_NOT_ALLOWED","message":"Temporary or placeholder email addresses
     cannot be used. Please sign up with a mailbox you can access."}

所以要域名邮箱（catch-all IMAP）。

**业务面**
    GET  /api/user/get-user-info
    GET  /api/user/get-user-credits
    GET  /api/user/get-credit-status
    POST /api/user/checkin                每日签到（顺手加额度）

**模型 API**（OpenAI 兼容形状，独立基址）
    https://jev-ai.org/api/v1              Bearer sk-glm5-…
    GET  /api/v1/models
    POST /api/v1/systemone                决策/评测主端点

Key 在 `https://jev-ai.org/settings/apikeys` 页面签发，前端是懒加载 chunk，
端点名不在首屏 bundle 里 —— 所以这里用**候选路径探测 + 命中缓存**（`key-endpoints.json`）：
逐个试常见的 Better Auth/Next 约定路径，谁的响应里出现 `sk-glm5-…` 就用谁，并记住它。
"""

from __future__ import annotations

import json
import re
import time
from pathlib import Path
from typing import Any, Callable

import httpx

from .errors import (
    Blocked,
    CaptchaRequired,
    EmailRejected,
    EmailTaken,
    JevError,
    NoApiKey,
    PasswordRejected,
    RateLimited,
    Transport,
    VerificationPending,
)
from .net import build_client, chrome_user_agent, request

BASE = "https://jev-ai.org"
API_BASE = f"{BASE}/api/v1"
KEY_PREFIX = "sk-glm5-"

SESSION = "/api/auth/get-session"
HEALTH = "/api/auth/ok"
SIGNUP = "/api/auth/sign-up/email"
SIGNIN = "/api/auth/sign-in/email"
SEND_VERIFY = "/api/auth/send-verification-email"
VERIFY = "/api/auth/verify-email"
USER_INFO = "/api/user/get-user-info"
USER_CREDITS = "/api/user/get-user-credits"
USER_CREDIT_STATUS = "/api/user/get-credit-status"
CHECKIN = "/api/user/checkin"

# 候选端点：Better Auth 生态里建 key 的常见落点（GET 列表 / POST 新建）
KEY_CANDIDATES: tuple[tuple[str, str], ...] = (
    ("/api/user/api-keys", "GET"),
    ("/api/user/api-keys", "POST"),
    ("/api/apikey", "GET"),
    ("/api/apikey", "POST"),
    ("/api/apikeys", "GET"),
    ("/api/apikeys", "POST"),
    ("/api/keys", "GET"),
    ("/api/keys", "POST"),
    ("/api/user/keys", "GET"),
    ("/api/user/keys", "POST"),
    ("/api/settings/apikeys", "GET"),
    ("/api/settings/apikeys", "POST"),
    ("/api/settings/api-keys", "GET"),
    ("/api/settings/api-keys", "POST"),
    ("/api/v1/api-keys", "GET"),
    ("/api/v1/api-keys", "POST"),
    ("/api/user/get-api-keys", "GET"),
    ("/api/user/get-api-keys", "POST"),
)

KEY_RE = re.compile(rf"{re.escape(KEY_PREFIX)}[A-Za-z0-9_\-]{{8,}}")
CF_MARKERS = ("just a moment", "cf-browser-verification", "challenge-platform", "attention required")

_EMAIL_BAD = ("email_domain_not_allowed", "invalid_email", "disposable", "placeholder email")
_EMAIL_TAKEN = ("user_already_exists", "email_already_exists", "already exists")
_PASSWORD_BAD = ("password_too_short", "password_too_long", "weak_password", "invalid_password")


def _looks_blocked(response: httpx.Response) -> bool:
    body = (response.text or "")[:4000].lower()
    if any(marker in body for marker in CF_MARKERS):
        return True
    return response.status_code in (403, 503) and "cloudflare" in response.headers.get("server", "").lower()


def _payload(response: httpx.Response) -> dict[str, Any]:
    try:
        data = response.json()
    except (json.JSONDecodeError, ValueError):
        return {}
    return data if isinstance(data, dict) else {"data": data}


def _raise_for(path: str, response: httpx.Response) -> None:
    payload = _payload(response)
    code = str(payload.get("code") or payload.get("error") or "")
    message = str(payload.get("message") or payload.get("error_description") or "")
    lowered = f"{code} {message}".lower()
    detail = {"status": response.status_code, "code": code, "message": message[:300]}

    if _looks_blocked(response):
        raise Blocked(f"{path}: edge answered with a bot challenge", detail=detail)
    if response.status_code == 429:
        raise RateLimited(f"{path}: rate limited", detail=detail)
    if "captcha" in lowered or "turnstile" in lowered:
        raise CaptchaRequired(f"{path}: captcha required ({message or code})", detail=detail)
    if any(token in lowered for token in _EMAIL_TAKEN):
        raise EmailTaken(f"{path}: address already registered", detail=detail)
    if any(token in lowered for token in _EMAIL_BAD):
        raise EmailRejected(f"{path}: address rejected ({message or code})", detail=detail)
    if any(token in lowered for token in _PASSWORD_BAD):
        raise PasswordRejected(f"{path}: password rejected ({message or code})", detail=detail)
    if response.status_code in (401, 403):
        raise JevError(f"{path}: not authorised (HTTP {response.status_code})", detail=detail)
    raise JevError(f"{path}: failed (HTTP {response.status_code} {message})", detail=detail)


def find_keys(blob: Any) -> list[str]:
    """在任意响应体里挖出 `sk-glm5-…`（去重保序）。"""
    text = blob if isinstance(blob, str) else json.dumps(blob, ensure_ascii=False)
    seen: list[str] = []
    for match in KEY_RE.findall(text or ""):
        if match not in seen:
            seen.append(match)
    return seen


class KeyEndpoints:
    """候选端点探测结果的缓存：命中一次就记下来，下次直接用。"""

    def __init__(self, path: str | Path | None = None) -> None:
        self.path = Path(path) if path else None
        self.data: dict[str, dict[str, str]] = {}
        if self.path and self.path.exists():
            try:
                self.data = json.loads(self.path.read_text(encoding="utf-8")) or {}
            except (json.JSONDecodeError, OSError):
                self.data = {}

    def get(self, site: str = BASE) -> dict[str, str]:
        return self.data.get(site, {})

    def remember(self, site: str, *, list_path: str = "", create_path: str = "", body_hint: Any = None) -> None:
        entry = dict(self.data.get(site, {}))
        if list_path:
            entry["listPath"] = list_path
        if create_path:
            entry["createPath"] = create_path
        if body_hint is not None:
            entry["bodyHint"] = body_hint
        entry["updatedAt"] = time.strftime("%Y-%m-%dT%H:%M:%S%z")
        self.data[site] = entry
        if self.path:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            self.path.write_text(json.dumps(self.data, ensure_ascii=False, indent=2), encoding="utf-8")


class Jev:
    """一个账号一个会话：cookie jar 就是 Better Auth 的 session。"""

    def __init__(
        self,
        *,
        base_url: str = BASE,
        proxy: str = "",
        timeout: float = 45.0,
        language: str = "en-US",
        user_agent: str = "",
        transport: Any = None,
        key_endpoints: KeyEndpoints | None = None,
    ) -> None:
        self.base = (base_url or BASE).rstrip("/")
        self.proxy = proxy
        self.user_agent = user_agent or chrome_user_agent()
        self.client = build_client(
            proxy=proxy, timeout=timeout, user_agent=self.user_agent, language=language, transport=transport
        )
        self.client.headers.update({"Origin": self.base, "Referer": f"{self.base}/sign-up"})
        self.key_endpoints = key_endpoints or KeyEndpoints()
        self.email = ""
        self.user: dict[str, Any] = {}

    def __enter__(self) -> "Jev":
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()

    def close(self) -> None:
        self.client.close()

    # ------------------------------------------------------------------- auth
    def health(self) -> bool:
        response = request(self.client, "GET", f"{self.base}{HEALTH}")
        return response.status_code == 200 and "ok" in (response.text or "")

    def signup(self, email: str, password: str, name: str = "") -> dict[str, Any]:
        body = {"email": email, "password": password, "name": name or email.split("@")[0]}
        response = request(
            self.client, "POST", f"{self.base}{SIGNUP}", json=body,
            headers={"Content-Type": "application/json", "Accept": "application/json"},
        )
        if _looks_blocked(response):
            raise Blocked(f"{SIGNUP}: edge answered with a bot challenge", detail=(response.text or "")[:300])
        if response.status_code >= 400:
            _raise_for(SIGNUP, response)
        payload = _payload(response)
        self.email = email
        user = payload.get("user") if isinstance(payload.get("user"), dict) else payload
        if isinstance(user, dict):
            self.user = user
        return payload

    def login(self, email: str, password: str) -> dict[str, Any]:
        response = request(
            self.client, "POST", f"{self.base}{SIGNIN}", json={"email": email, "password": password},
            headers={"Content-Type": "application/json", "Accept": "application/json"},
        )
        if response.status_code >= 400:
            _raise_for(SIGNIN, response)
        payload = _payload(response)
        self.email = email
        return payload

    def session(self) -> dict[str, Any] | None:
        response = request(self.client, "GET", f"{self.base}{SESSION}")
        if response.status_code >= 400:
            if response.status_code in (401, 403):
                return None
            _raise_for(SESSION, response)
        payload = _payload(response)
        if not payload:
            return None
        data = payload["data"] if "data" in payload else payload
        if not isinstance(data, dict) or not data:
            return None
        return data

    def verified(self) -> bool | None:
        payload = self.session() or {}
        user = payload.get("user") if isinstance(payload.get("user"), dict) else {}
        return user.get("emailVerified")

    def send_verification(self) -> None:
        response = request(
            self.client, "POST", f"{self.base}{SEND_VERIFY}", json={"email": self.email},
            headers={"Content-Type": "application/json"},
        )
        if response.status_code >= 400:
            _raise_for(SEND_VERIFY, response)

    def verify_link(self, link: str) -> bool:
        response = request(self.client, "GET", link, follow_redirects=False)
        if 300 <= response.status_code < 400:
            location = response.headers.get("location", "")
            if "error=" in location:
                raise VerificationPending(f"verification link rejected: {location[:160]}")
            return True
        if response.status_code >= 400:
            _raise_for(VERIFY, response)
        return True

    # ---------------------------------------------------------------- account
    def user_info(self) -> dict[str, Any]:
        response = request(self.client, "GET", f"{self.base}{USER_INFO}")
        if response.status_code >= 400:
            return {}
        payload = _payload(response)
        return payload.get("data") if isinstance(payload.get("data"), dict) else payload

    def credits(self) -> dict[str, Any]:
        response = request(self.client, "GET", f"{self.base}{USER_CREDITS}")
        if response.status_code >= 400:
            return {}
        payload = _payload(response)
        return payload.get("data") if isinstance(payload.get("data"), dict) else payload

    def credit_status(self) -> dict[str, Any]:
        """`POST /api/user/get-credit-status`（前端就是 POST）。

        回的形状固定为 `{code, data}`，`data.chatTier` 是 "free" / 付费档，
        额度耗尽的 reason 会是 `free_daily_limit` 或 `premium_model`。
        """
        response = request(self.client, "POST", f"{self.base}{USER_CREDIT_STATUS}", json={})
        if response.status_code >= 400:
            return {}
        payload = _payload(response)
        if "code" in payload and payload.get("code") not in (0, "0", None):
            return {}
        data = payload.get("data", payload)
        return data if isinstance(data, dict) else {}

    def checkin(self) -> dict[str, Any]:
        response = request(self.client, "POST", f"{self.base}{CHECKIN}", json={})
        if response.status_code >= 400:
            return {}
        return _payload(response)

    # ------------------------------------------------------------------- keys
    def list_keys(self) -> list[dict[str, Any]]:
        """按缓存优先、候选兜底的方式列出已有 Key。"""
        cached = self.key_endpoints.get(self.base).get("listPath")
        order = ([cached] if cached else []) + [p for (p, m) in KEY_CANDIDATES if m == "GET" and p != cached]
        for path in order:
            if not path:
                continue
            try:
                response = request(self.client, "GET", f"{self.base}{path}")
            except Transport:
                continue
            if response.status_code >= 400:
                continue
            text = response.text or ""
            if KEY_PREFIX not in text and "apiKey" not in text and "api_key" not in text:
                continue
            self.key_endpoints.remember(self.base, list_path=path)
            return _normalize_keys(_payload(response), text)
        return []

    def create_key(self, name: str = "") -> dict[str, Any]:
        """建一把 Key；端点未知时按候选逐个试，命中即缓存。"""
        cached = self.key_endpoints.get(self.base).get("createPath")
        order = ([cached] if cached else []) + [p for (p, m) in KEY_CANDIDATES if m == "POST" and p != cached]
        last: httpx.Response | None = None
        for path in order:
            if not path:
                continue
            for body in ({"name": name} if name else {}, {}, {"label": name or "dcv"}):
                try:
                    response = request(
                        self.client, "POST", f"{self.base}{path}", json=body,
                        headers={"Content-Type": "application/json", "Accept": "application/json"},
                    )
                except Transport:
                    continue
                last = response
                if response.status_code in (404, 405):
                    break
                if response.status_code >= 400:
                    continue
                found = find_keys(response.text or "")
                if found:
                    self.key_endpoints.remember(self.base, create_path=path, body_hint=body)
                    return {"key": found[0], "raw": _payload(response), "path": path}
        if last is not None and last.status_code >= 400 and last.status_code not in (404, 405):
            _raise_for("key create", last)
        raise NoApiKey("没找到建 Key 的端点（候选全失败）")

    def ensure_key(self, name: str = "") -> str:
        """先看已有 Key，没有再建一把；都没有就报 `NoApiKey`。"""
        for row in self.list_keys():
            key = str(row.get("key") or row.get("token") or "")
            if key.startswith(KEY_PREFIX):
                return key
        created = self.create_key(name)
        key = str(created.get("key") or "")
        if not key.startswith(KEY_PREFIX):
            raise NoApiKey("建 Key 接口没有返回 sk-glm5-… 形状的 Key", detail=str(created)[:200])
        return key

    # --------------------------------------------------------------- composite
    def run_flow(
        self,
        email: str,
        password: str,
        *,
        name: str = "",
        verify: bool = True,
        provider: Any = None,
        box: Any = None,
        verify_timeout: float = 180.0,
        key_name: str = "",
        log: Callable[[str], None] | None = None,
    ) -> dict[str, Any]:
        say = log or (lambda _m: None)
        self.signup(email, password, name=name or email.split("@")[0])
        say("注册成功")

        email_verified: bool | None = None
        if verify and provider is not None and box is not None:
            try:
                link = provider.wait_for_link(box, timeout=verify_timeout)
                if link:
                    self.verify_link(link)
                    email_verified = self.verified()
                    say(f"邮箱已验证（emailVerified={email_verified}）")
                else:
                    say("没收到验证信，继续建 Key")
            except VerificationPending as exc:
                say(f"验证失败（继续）：{exc}")
        else:
            email_verified = self.verified()

        api_key = self.ensure_key(key_name or f"dcg-{time.strftime('%Y-%m-%d')}")
        say(f"拿到 Key {api_key[:12]}…")

        info = self.user_info()
        credits = self.credits()
        try:
            self.checkin()
        except JevError:
            pass

        return {
            "email": email,
            "password": password,
            "name": name or email.split("@")[0],
            "apiKey": api_key,
            "keyPrefix": KEY_PREFIX,
            "userId": str((self.session() or {}).get("user", {}).get("id") or ""),
            "emailVerified": email_verified,
            "credits": credits or info.get("credits") or {},
            "userInfo": info,
        }


def _normalize_keys(payload: Any, text: str = "") -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    source = payload
    if isinstance(source, dict):
        for key in ("data", "keys", "apiKeys", "api_keys", "items", "list"):
            value = source.get(key)
            if isinstance(value, list):
                source = value
                break
        else:
            source = [source]
    if isinstance(source, list):
        for item in source:
            if isinstance(item, str) and item.startswith(KEY_PREFIX):
                rows.append({"key": item})
            elif isinstance(item, dict):
                key = ""
                for field in ("key", "token", "apiKey", "api_key", "secret", "value"):
                    value = item.get(field)
                    if isinstance(value, str) and value.startswith(KEY_PREFIX):
                        key = value
                        break
                if key:
                    rows.append({**item, "key": key})
    if not rows:
        rows = [{"key": key} for key in find_keys(text or payload)]
    return rows


def probe(*, proxy: str = "") -> dict[str, Any]:
    """`doctor`：站点可达性 + 认证面 + 模型 API 的错误形状。"""
    client = build_client(proxy=proxy, timeout=20.0)
    try:
        health = request(client, "GET", f"{BASE}{HEALTH}")
        session = request(client, "GET", f"{BASE}{SESSION}")
        models = request(client, "GET", f"{API_BASE}/models")
        return {
            "auth_ok": f"HTTP {health.status_code} {health.text.strip()[:40]}",
            "session_anonymous": f"HTTP {session.status_code} {session.text[:40]}",
            "models_without_key": f"HTTP {models.status_code} {(models.text or '')[:160]}",
            "api_base": API_BASE,
            "key_prefix": KEY_PREFIX,
        }
    except Transport as exc:
        return {"health": f"unreachable: {exc}"}
    finally:
        client.close()
