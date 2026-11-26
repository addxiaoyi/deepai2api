"""会话：Better Auth 注册/登录/验证（六条线里五条都是这套）。

只有 Pollinations 没账号（匿名 + IP 限速），它走 `anon_session()`。
"""

from __future__ import annotations

import json
import re
import time
from typing import Any, Callable

import httpx

from .errors import (
    Blocked,
    CaptchaRequired,
    EmailRejected,
    EmailTaken,
    LineError,
    PasswordRejected,
    RateLimited,
    VerificationPending,
)
from .net import build_client, chrome_user_agent, request
from .site import Site

KEY_RE_TEMPLATE = r"{prefix}[A-Za-z0-9_\-]{{8,}}"
CF_MARKERS = ("just a moment", "cf-browser-verification", "challenge-platform", "attention required")

_EMAIL_BAD = ("email_domain_not_allowed", "invalid_email", "disposable", "placeholder email")
_EMAIL_TAKEN = ("user_already_exists", "email_already_exists", "already exists", "already registered")
_PASSWORD_BAD = ("password_too_short", "password_too_long", "weak_password", "invalid_password")


def find_keys(blob: Any, prefix: str) -> list[str]:
    text = blob if isinstance(blob, str) else json.dumps(blob, ensure_ascii=False)
    pattern = re.compile(re.escape(prefix) + r"[A-Za-z0-9_\-]{8,}")
    seen: list[str] = []
    for match in pattern.findall(text or ""):
        if match not in seen:
            seen.append(match)
    return seen


def _payload(response: httpx.Response) -> dict[str, Any]:
    try:
        data = response.json()
    except (json.JSONDecodeError, ValueError):
        return {}
    return data if isinstance(data, dict) else {"data": data}


def _looks_blocked(response: httpx.Response) -> bool:
    body = (response.text or "")[:4000].lower()
    if any(marker in body for marker in CF_MARKERS):
        return True
    return response.status_code in (403, 503) and "cloudflare" in response.headers.get("server", "").lower()


def norm(payload: Any) -> list[dict[str, Any]]:
    """把各种 "key 列表" 形状统一成 `[{key, ...}]`。"""
    rows: list[dict[str, Any]] = []
    source = payload
    if isinstance(source, dict):
        for field in ("data", "keys", "apiKeys", "api_keys", "items", "list", "tokens"):
            value = source.get(field)
            if isinstance(value, list):
                source = value
                break
        else:
            source = [source]
    if isinstance(source, list):
        for item in source:
            if isinstance(item, str):
                rows.append({"key": item})
            elif isinstance(item, dict):
                for field in ("key", "token", "apiKey", "api_key", "secret", "value"):
                    value = item.get(field)
                    if isinstance(value, str) and value:
                        rows.append({**item, "key": value})
                        break
    return rows


class Session:
    """一个账号一个会话（cookie jar 就是 Better Auth 的 session）。"""

    def __init__(
        self,
        site: Site,
        *,
        proxy: str = "",
        timeout: float = 45.0,
        language: str = "en-US",
        user_agent: str = "",
        transport: Any = None,
    ) -> None:
        self.site = site
        self.client = build_client(
            proxy=proxy, timeout=timeout, user_agent=user_agent or chrome_user_agent(), language=language,
            transport=transport,
        )
        self.client.headers.update({"Origin": site.base_url, "Accept": "application/json"})
        self.client.headers.update(site.extra_headers or {})
        self.email = ""
        self.user: dict[str, Any] = {}

    def __enter__(self) -> "Session":
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()

    def close(self) -> None:
        self.client.close()

    # ------------------------------------------------------------------ 判定
    def _raise(self, path: str, response: httpx.Response) -> None:
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
        raise LineError(f"{path}: failed (HTTP {response.status_code} {message})", detail=detail)

    # ------------------------------------------------------------------ 注册
    def health(self) -> bool:
        path = self.site.health_path
        if not path:
            return True
        response = request(self.client, "GET", self.site.url(path))
        return response.status_code < 400

    def signup(self, email: str, password: str, name: str = "") -> dict[str, Any]:
        body = {"email": email, "password": password, "name": name or email.split("@")[0]}
        response = request(self.client, "POST", self.site.url(self.site.signup_path), json=body)
        if _looks_blocked(response):
            raise Blocked(f"{self.site.signup_path}: bot challenge", detail=(response.text or "")[:300])
        if response.status_code >= 400:
            self._raise(self.site.signup_path, response)
        payload = _payload(response)
        self.email = email
        user = payload.get("user") if isinstance(payload.get("user"), dict) else payload
        if isinstance(user, dict):
            self.user = user
        return payload

    def login(self, email: str, password: str) -> dict[str, Any]:
        response = request(
            self.client, "POST", self.site.url(self.site.signin_path), json={"email": email, "password": password}
        )
        if response.status_code >= 400:
            self._raise(self.site.signin_path, response)
        self.email = email
        return _payload(response)

    def session(self) -> dict[str, Any] | None:
        response = request(self.client, "GET", self.site.url(self.site.session_path))
        if response.status_code in (401, 403):
            return None
        if response.status_code >= 400:
            return None
        payload = _payload(response)
        data = payload.get("data", payload) if payload else None
        if not isinstance(data, dict) or not data:
            return None
        return data

    def verified(self) -> bool | None:
        payload = self.session() or {}
        user = payload.get("user") if isinstance(payload.get("user"), dict) else {}
        return user.get("emailVerified")

    def send_verification(self) -> None:
        if not self.site.send_verify_path:
            return
        response = request(
            self.client, "POST", self.site.url(self.site.send_verify_path), json={"email": self.email}
        )
        if response.status_code >= 400:
            self._raise(self.site.send_verify_path, response)

    def verify_link(self, link: str) -> bool:
        response = request(self.client, "GET", link, follow_redirects=False)
        if 300 <= response.status_code < 400:
            location = response.headers.get("location", "")
            if "error=" in location:
                raise VerificationPending(f"verification link rejected: {location[:160]}")
            return True
        if response.status_code >= 400:
            self._raise(self.site.verify_path or "verify", response)
        return True

    def credits(self) -> dict[str, Any]:
        if not self.site.credits_path:
            return {}
        response = request(self.client, "GET", self.site.url(self.site.credits_path))
        if response.status_code >= 400:
            return {}
        payload = _payload(response)
        data = payload.get("data", payload)
        return data if isinstance(data, dict) else {}

    # -------------------------------------------------------------------- Key
    def list_keys(self) -> list[dict[str, Any]]:
        for path, method in self.site.key_candidates:
            if method != "GET":
                continue
            try:
                response = request(self.client, "GET", self.site.url(path))
            except LineError:
                continue
            if response.status_code >= 400:
                continue
            text = response.text or ""
            if self.site.key_prefix not in text and "apiKey" not in text and "api_key" not in text:
                continue
            rows = norm(_payload(response))
            if not rows:
                rows = [{"key": key} for key in find_keys(text, self.site.key_prefix)]
            if rows:
                return [{**row, "_path": path} for row in rows if str(row.get("key", "")).startswith(self.site.key_prefix)]
        return []

    def create_key(self, name: str = "") -> dict[str, Any]:
        if not self.site.key_candidates:
            raise LineError("这条线没有登记发 Key 端点（site.key_candidates 为空）")
        last: httpx.Response | None = None
        for path, method in self.site.key_candidates:
            if method != "POST":
                continue
            for body in self.site.create_key_body:
                payload = dict(body)
                if name and "name" in payload:
                    payload["name"] = name
                try:
                    response = request(self.client, "POST", self.site.url(path), json=payload)
                except LineError:
                    continue
                last = response
                if response.status_code in (404, 405):
                    break
                if response.status_code >= 400:
                    continue
                found = find_keys(response.text or "", self.site.key_prefix)
                if not found:
                    rows = norm(_payload(response))
                    found = [str(row["key"]) for row in rows if str(row.get("key", "")).startswith(self.site.key_prefix)]
                if found:
                    return {"key": found[0], "path": path, "raw": _payload(response)}
        if last is not None and last.status_code >= 400 and last.status_code not in (404, 405):
            self._raise("key create", last)
        raise LineError("没找到发 Key 端点（候选全失败）")

    def ensure_key(self, name: str = "") -> str:
        for row in self.list_keys():
            key = str(row.get("key") or "")
            if key.startswith(self.site.key_prefix):
                return key
        created = self.create_key(name)
        key = str(created.get("key") or "")
        if not key.startswith(self.site.key_prefix):
            raise LineError(f"建 Key 没返回 {self.site.key_prefix}… 形状")
        return key

    # ------------------------------------------------------------------ 组合
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
        if verify and provider is not None and box is not None and self.site.send_verify_path:
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

        return {
            "email": email,
            "password": password,
            "name": name or email.split("@")[0],
            "apiKey": api_key,
            "keyPrefix": self.site.key_prefix,
            "userId": str((self.session() or {}).get("user", {}).get("id") or ""),
            "emailVerified": email_verified,
            "credits": self.credits(),
        }


def anon_session(site: Site, *, proxy: str = "", timeout: float = 45.0, transport: Any = None) -> dict[str, Any]:
    """无账号线（Pollinations）：直接当"匿名 Key"用，不做注册。"""
    client = build_client(proxy=proxy, timeout=timeout, transport=transport)
    try:
        response = request(client, "GET", f"{site.api_base.rstrip('/')}/models")
        ok = response.status_code < 400
        return {
            "email": f"anon@{site.id}",
            "password": "",
            "name": "anonymous",
            "apiKey": "",           # 匿名线不需要 Key
            "keyPrefix": site.key_prefix,
            "emailVerified": None,
            "credits": {},
            "anonymous": True,
            "modelsOk": ok,
            "status": response.status_code,
        }
    finally:
        client.close()


def _dig(payload: Any, path: str) -> Any:
    """`data.access_token` 这种点号路径取值。"""
    node: Any = payload
    for part in str(path or "").split("."):
        if not part:
            continue
        if isinstance(node, dict):
            node = node.get(part)
        else:
            return None
    return node


class CodeSignupSession(Session):
    """邮箱验证码注册的站点（Agnes / 一堆国内站都是这个形状）。

    四步：发码 → 收件箱取 6 位码 → POST 注册拿 access_token → Bearer 建 Key。
    """

    def request_code(self, email: str, purpose: str = "register", captcha: dict[str, str] | None = None) -> bool:
        path = self.site.verify_code_path
        if not path:
            raise LineError("这条线没有登记发码端点（site.verify_code_path）")
        params: dict[str, str] = {"email": email, "purpose": purpose}
        # 有验证码门槛的站（Agnes = 腾讯云验证码）要把 ticket/randstr 一起带上
        for name in self.site.captcha_params:
            value = str((captcha or {}).get(name) or "").strip()
            if value:
                params[name] = value
        response = request(self.client, "GET", self.site.url(path), params=params)
        if response.status_code >= 400:
            self._raise(path, response)
        payload = _payload(response)
        code = payload.get("code")
        if code not in (None, 200, "200", 0, "0"):
            message = str(payload.get("message") or "")
            lowered = message.lower()
            if "too many" in lowered and "domain" in lowered:
                raise RejectedDomain(f"{path}: 该邮箱域名被限流（{message[:120]}）", detail=payload)
            self._raise(path, httpx.Response(400, request=response.request, json=payload))
        self.email = email
        return True

    def signup_with_code(self, email: str, password: str, code: str) -> dict[str, Any]:
        path = self.site.register_path
        if not path:
            raise LineError("这条线没有登记注册端点（site.register_path）")
        body = {"email": email, "password": password, "password_confirm": password, "code": code}
        response = request(self.client, "POST", self.site.url(path), json=body)
        if response.status_code >= 400:
            self._raise(path, response)
        payload = _payload(response)
        if payload.get("code") not in (None, 200, "200", 0, "0"):
            self._raise(path, httpx.Response(400, request=response.request, json=payload))
        self.access_token = str(_dig(payload, self.site.token_json_path) or "")
        if not self.access_token:
            raise LineError(f"{path}: 注册成功但没拿到 access_token（token_json_path={self.site.token_json_path}）")
        self.user = (_dig(payload, "data.user") or {}) if isinstance(_dig(payload, "data.user"), dict) else {}
        return payload

    def create_key(self, name: str = "") -> dict[str, Any]:
        path = self.site.token_path
        if not path:
            return super().create_key(name)
        headers = {"Authorization": f"Bearer {self.access_token}", "Content-Type": "application/json"}
        payloads = ({"name": name or "dcg", "api_key_profile": "default"}, {"name": name or "dcg"}, {})
        last: httpx.Response | None = None
        for body in payloads:
            response = request(self.client, "POST", self.site.url(path), json=body, headers=headers)
            last = response
            if response.status_code >= 400:
                continue
            data = _payload(response)
            if data.get("code") not in (None, 200, "200", 0, "0"):
                continue
            key = str(_dig(data, self.site.key_json_path) or "")
            if not key:
                found = find_keys(data, self.site.key_prefix)
                key = found[0] if found else ""
            if key:
                return {"key": key, "path": path, "raw": data}
        if last is not None and last.status_code >= 400:
            self._raise(path, last)
        raise LineError(f"{path}: 建 Key 没返回 {self.site.key_prefix or 'Key'}")

    def list_keys(self) -> list[dict[str, Any]]:
        """这类站点通常没有"列 Key"接口；返回空，让 ensure_key 去建。"""
        return []

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
        captcha: dict[str, str] | None = None,
        log: Callable[[str], None] | None = None,
    ) -> dict[str, Any]:
        say = log or (lambda _m: None)
        if provider is None or box is None:
            raise LineError("验证码注册需要邮箱 provider 与 mailbox")
        if self.site.captcha_params and not any((captcha or {}).values()):
            raise CaptchaRequired(
                f"{self.site.id}: 发码需要验证码参数 {'/'.join(self.site.captcha_params)}"
                "（用 --captcha manual + --ticket/--randstr，或 --captcha browser）"
            )
        self.request_code(email, captcha=captcha)
        say("验证码已发送")
        code = provider.wait_for_code(box, timeout=verify_timeout)
        if not code:
            raise VerificationPending("没收到验证码邮件")
        say(f"收到验证码 {code}")
        self.signup_with_code(email, password, code)
        say("注册成功")
        api_key = self.create_key(key_name or f"dcg-{time.strftime('%Y-%m-%d')}")["key"]
        say(f"拿到 Key {api_key[:12]}…")
        return {
            "email": email,
            "password": password,
            "name": name or email.split("@")[0],
            "apiKey": api_key,
            "keyPrefix": self.site.key_prefix,
            "userId": str((self.user or {}).get("display_id") or (self.user or {}).get("id") or ""),
            "emailVerified": True,
            "credits": self.credits(),
        }


def make_session(site: Site, **kwargs: Any) -> Session:
    """按 `site.flow` 选会话实现。"""
    if site.flow == "code-signup":
        return CodeSignupSession(site, **kwargs)
    return Session(site, **kwargs)
