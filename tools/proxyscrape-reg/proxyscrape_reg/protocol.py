"""proxyscrape.com 注册 + 领取 premium 试用 + 读出代理凭据。

从 dashboard 的 Next.js bundle 还原出的端点（都已用 `/v2/api/init` 与 400 报文校验）：

    POST /v2/v4/account/auth/register        {email, password, cf_turnstile_token, …}
    POST /v2/v4/account/auth/login           {service:"email", email, password, cf_turnstile_token}
    GET  /v2/v4/account/auth/me              Bearer
    POST /v2/v4/account/auth/refresh
    GET  /v2/v4/account/accounts-summary     各产品/服务 + 代理凭据
    GET  /v2/v4/account/premium/trial-eligibility   -> {claimed: bool}
    POST /v2/v4/account/premium/claim-trial
    GET  /v2/v4/account/api-keys             面板 API key
    GET  /v2/services/{product}/proxy-setup/{id}   代理接入信息

要点：

* **Turnstile 是硬门**：不带 `cf_turnstile_token` 一律 400 "No Cloudflare Turnstile
  token provided."，sitekey `0x4AAAAAAAFWUVCKyusT9T8r`。
* 试用是**按账号**领的，`trial-eligibility` 的 `claimed` 决定还能不能领；
  同一出口领太多次会被拒（`no_trial`）。
* 代理凭据不在固定字段里：`accounts-summary` 的 `services.*` 下会带
  `provider` / `proxy_credentials`，不同产品形状不一样，所以这里用**深扫描**兜底
  （找 `{host|ip|server}` + `{port|ports}` + 可选 `{username,password}`）。
"""

from __future__ import annotations

import json
import re
import time
from typing import Any, Callable, Iterable

import httpx

from .errors import (
    Blocked,
    EmailRejected,
    EmailTaken,
    NoCredentials,
    NoTrial,
    PasswordRejected,
    ProxyScrapeError,
    RateLimited,
    Transport,
    TurnstileRequired,
    VerificationPending,
)
from .net import build_client, chrome_user_agent, normalize_endpoint, request

BASE = "https://dashboard.proxyscrape.com"

REGISTER = "/v2/v4/account/auth/register"
LOGIN = "/v2/v4/account/auth/login"
REFRESH = "/v2/v4/account/auth/refresh"
ME = "/v2/v4/account/auth/me"
INIT = "/v2/api/init"
SUMMARY = "/v2/v4/account/accounts-summary"
TRIAL_ELIGIBILITY = "/v2/v4/account/premium/trial-eligibility"
TRIAL_CLAIM = "/v2/v4/account/premium/claim-trial"
API_KEYS = "/v2/v4/account/api-keys"
VERIFY_EMAIL = "/v2/v4/account/verify-email"

CF_MARKERS = ("just a moment", "cf-browser-verification", "challenge-platform", "attention required")

_HOST_KEYS = ("host", "ip", "server", "hostname", "proxy_host")
_PORT_KEYS = ("port", "proxy_port", "http_port", "https_port", "ports")
_USER_KEYS = ("username", "user", "login", "proxy_username")
_PASS_KEYS = ("password", "pass", "proxy_password")


def _looks_blocked(response: httpx.Response) -> bool:
    if response.status_code in (403, 503):
        body = (response.text or "")[:2000].lower()
        if any(marker in body for marker in CF_MARKERS):
            return True
    body = (response.text or "")[:4000].lower()
    return any(marker in body for marker in CF_MARKERS)


def _payload(response: httpx.Response) -> dict[str, Any]:
    try:
        data = response.json()
        return data if isinstance(data, dict) else {"data": data}
    except (json.JSONDecodeError, ValueError):
        return {}


def _message(payload: dict[str, Any]) -> str:
    for key in ("message", "error", "detail", "error_description"):
        value = payload.get(key)
        if isinstance(value, str) and value.strip():
            return value
    err = payload.get("errors")
    if isinstance(err, list) and err:
        return json.dumps(err[0])[:300]
    return ""


def _raise_for(path: str, response: httpx.Response) -> None:
    payload = _payload(response)
    message = _message(payload)
    lowered = message.lower()
    detail = {"status": response.status_code, "message": message[:400]}

    if _looks_blocked(response):
        raise Blocked(f"{path}: edge answered with a bot challenge", detail=detail)
    if response.status_code == 429:
        raise RateLimited(f"{path}: rate limited", detail=detail)
    if "turnstile" in lowered or "captcha" in lowered:
        raise TurnstileRequired(f"{path}: {message or 'turnstile rejected'}", detail=detail)
    if "already" in lowered and ("exist" in lowered or "account" in lowered or "register" in lowered):
        raise EmailTaken(f"{path}: address already registered", detail=detail)
    if "already claimed" in lowered or "already been claimed" in lowered:
        raise NoTrial(f"{path}: trial already claimed", detail=detail)
    if "trial" in lowered and ("not eligible" in lowered or "unavailable" in lowered or "cannot" in lowered):
        raise NoTrial(f"{path}: {message}", detail=detail)
    if "invalid email" in lowered or "email not" in lowered or "disposable" in lowered:
        raise EmailRejected(f"{path}: address rejected ({message})", detail=detail)
    if "password" in lowered and ("weak" in lowered or "short" in lowered or "invalid" in lowered):
        raise PasswordRejected(f"{path}: password rejected ({message})", detail=detail)
    if response.status_code in (401, 403):
        raise ProxyScrapeError(f"{path}: not authorised (HTTP {response.status_code})", detail=detail)
    raise ProxyScrapeError(f"{path}: failed (HTTP {response.status_code} {message})", detail=detail)


# ------------------------------------------------------------------ 代理深扫描


def _as_endpoints(node: Any, *, inherited_user: str = "", inherited_pass: str = "", out: list[str] | None = None) -> list[str]:
    """在任意 JSON 里找 `{host, port(s), username?, password?}` 并拼成代理串。"""
    out = out if out is not None else []
    if isinstance(node, dict):
        user = ""
        for key in _USER_KEYS:
            value = node.get(key)
            if isinstance(value, str) and value:
                user = value
                break
        password = ""
        for key in _PASS_KEYS:
            value = node.get(key)
            if isinstance(value, str) and value:
                password = value
                break
        user = user or inherited_user
        password = password or inherited_pass

        host = ""
        for key in _HOST_KEYS:
            value = node.get(key)
            if isinstance(value, str) and value and "." in value or (isinstance(value, str) and value.count(":") >= 1):
                host = value
                break
        if host:
            ports: list[Any] = []
            for key in _PORT_KEYS:
                value = node.get(key)
                if isinstance(value, int):
                    ports.append(value)
                elif isinstance(value, str) and value.isdigit():
                    ports.append(value)
                elif isinstance(value, (list, tuple)):
                    ports.extend(value)
            for port in ports:
                endpoint = normalize_endpoint(host, port, user, password)
                if endpoint and endpoint not in out:
                    out.append(endpoint)
        for value in node.values():
            _as_endpoints(value, inherited_user=user, inherited_pass=password, out=out)
    elif isinstance(node, (list, tuple)):
        for item in node:
            _as_endpoints(item, inherited_user=inherited_user, inherited_pass=inherited_pass, out=out)
    return out


def extract_proxies(payload: Any) -> list[str]:
    """把一个响应体里所有像代理的东西挖出来（去重、保序）。"""
    return _as_endpoints(payload)


def parse_proxy_text(text: str) -> list[str]:
    """面板有时给的是 `host:port:user:pass` 或 `user:pass@host:port` 文本。"""
    out: list[str] = []
    for raw in (text or "").replace("\r", "\n").split("\n"):
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        if line.startswith("http://") or line.startswith("https://") or line.startswith("socks"):
            if line not in out:
                out.append(line)
            continue
        parts = line.split(":")
        if len(parts) == 4:
            host, port, user, password = parts
            endpoint = normalize_endpoint(host, port, user, password)
        elif len(parts) == 2:
            endpoint = normalize_endpoint(parts[0], parts[1])
        else:
            endpoint = ""
        if endpoint and endpoint not in out:
            out.append(endpoint)
    return out


# --------------------------------------------------------------------- session


class ProxyScrape:
    """一个账号一个会话。cookie jar + Bearer 都留着，领试用和读凭据都要登录态。"""

    def __init__(
        self,
        *,
        base_url: str = BASE,
        proxy: str = "",
        timeout: float = 60.0,
        language: str = "en-US",
        user_agent: str = "",
        transport: Any = None,
    ) -> None:
        self.base = (base_url or BASE).rstrip("/")
        self.proxy = proxy
        self.user_agent = user_agent or chrome_user_agent()
        self.client = build_client(
            proxy=proxy, timeout=timeout, user_agent=self.user_agent, language=language, transport=transport
        )
        self.client.headers.update({"Origin": self.base, "Referer": f"{self.base}/v2/sign-up"})
        self.access_token = ""
        self.refresh_token = ""
        self.email = ""

    # ------------------------------------------------------------------ basics
    def __enter__(self) -> "ProxyScrape":
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()

    def close(self) -> None:
        self.client.close()

    def _headers(self, extra: dict[str, str] | None = None) -> dict[str, str]:
        headers = {"Accept": "application/json", "Content-Type": "application/json"}
        if self.access_token:
            headers["Authorization"] = f"Bearer {self.access_token}"
        if extra:
            headers.update(extra)
        return headers

    def _adopt(self, payload: dict[str, Any]) -> None:
        data = payload.get("data") if isinstance(payload.get("data"), dict) else payload
        tokens = data.get("tokens") if isinstance(data.get("tokens"), dict) else {}
        self.access_token = str(
            tokens.get("access") or data.get("accessToken") or data.get("access_token") or self.access_token or ""
        )
        self.refresh_token = str(
            tokens.get("refresh") or data.get("refreshToken") or data.get("refresh_token") or self.refresh_token or ""
        )

    # -------------------------------------------------------------------- auth
    def register(self, email: str, password: str, *, turnstile: str = "", first_name: str = "", last_name: str = "") -> dict[str, Any]:
        body: dict[str, Any] = {
            "email": email,
            "password": password,
            "cf_turnstile_token": turnstile,
        }
        if first_name:
            body["firstName"] = first_name
        if last_name:
            body["lastName"] = last_name
        response = request(self.client, "POST", f"{self.base}{REGISTER}", json=body, headers=self._headers())
        if _looks_blocked(response):
            raise Blocked(f"{REGISTER}: edge answered with a bot challenge", detail=(response.text or "")[:300])
        if response.status_code >= 400:
            _raise_for(REGISTER, response)
        payload = _payload(response)
        self._adopt(payload)
        self.email = email
        return payload

    def login(self, email: str, password: str, *, turnstile: str = "") -> dict[str, Any]:
        body = {"service": "email", "email": email, "password": password, "cf_turnstile_token": turnstile}
        response = request(self.client, "POST", f"{self.base}{LOGIN}", json=body, headers=self._headers())
        if _looks_blocked(response):
            raise Blocked(f"{LOGIN}: edge answered with a bot challenge", detail=(response.text or "")[:300])
        if response.status_code >= 400:
            _raise_for(LOGIN, response)
        payload = _payload(response)
        self._adopt(payload)
        self.email = email
        return payload

    def refresh(self) -> dict[str, Any]:
        if not self.refresh_token:
            raise ProxyScrapeError("no refresh token")
        response = request(
            self.client,
            "POST",
            f"{self.base}{REFRESH}",
            json={"refreshToken": self.refresh_token},
            headers=self._headers(),
        )
        if response.status_code >= 400:
            _raise_for(REFRESH, response)
        payload = _payload(response)
        self._adopt(payload)
        return payload

    def me(self) -> dict[str, Any]:
        response = request(self.client, "GET", f"{self.base}{ME}", headers=self._headers())
        if response.status_code >= 400:
            _raise_for(ME, response)
        return _payload(response)

    # ------------------------------------------------------------------- trial
    def trial_claimed(self) -> bool:
        response = request(self.client, "GET", f"{self.base}{TRIAL_ELIGIBILITY}", headers=self._headers())
        if response.status_code >= 400:
            _raise_for(TRIAL_ELIGIBILITY, response)
        payload = _payload(response)
        data = payload.get("data") if isinstance(payload.get("data"), dict) else payload
        return bool(data.get("claimed"))

    def claim_trial(self) -> dict[str, Any]:
        response = request(
            self.client, "POST", f"{self.base}{TRIAL_CLAIM}", json={}, headers=self._headers()
        )
        if response.status_code >= 400:
            _raise_for(TRIAL_CLAIM, response)
        return _payload(response)

    # --------------------------------------------------------------- 凭据读取
    def summary(self) -> dict[str, Any]:
        response = request(self.client, "GET", f"{self.base}{SUMMARY}", headers=self._headers())
        if response.status_code >= 400:
            _raise_for(SUMMARY, response)
        return _payload(response)

    def api_keys(self) -> list[dict[str, Any]]:
        response = request(self.client, "GET", f"{self.base}{API_KEYS}", headers=self._headers())
        if response.status_code >= 400:
            _raise_for(API_KEYS, response)
        payload = _payload(response)
        data = payload.get("data") if isinstance(payload.get("data"), list) else payload.get("apiKeys")
        return data if isinstance(data, list) else []

    def verify_email_link(self, link: str) -> bool:
        response = request(self.client, "GET", link, follow_redirects=True)
        if _looks_blocked(response):
            raise Blocked("verify link hit a bot challenge")
        if response.status_code >= 400:
            raise VerificationPending(
                f"verification link rejected (HTTP {response.status_code})", detail=(response.text or "")[:200]
            )
        return True

    def proxies(self) -> list[str]:
        """把能拿到的凭据汇总成代理串列表（summary + api-keys + 各产品 proxy-setup）。"""
        found: list[str] = []
        try:
            found.extend(extract_proxies(self.summary()))
        except ProxyScrapeError:
            pass
        # 产品级 proxy-setup：从 summary 里挑出 service id 再逐个问
        try:
            payload = self.summary()
            for product, service in (payload.get("data", {}).get("services") or {}).items():
                sid = ""
                if isinstance(service, dict):
                    sid = str(service.get("id") or service.get("subscriptionId") or "")
                if not sid:
                    continue
                for path in (f"/v2/services/{product}/proxy-setup/{sid}", f"/v2/services/{product}/proxy-list/{sid}"):
                    response = request(self.client, "GET", f"{self.base}{path}", headers=self._headers())
                    if response.status_code < 400:
                        found.extend(parse_proxy_text(response.text))
                        found.extend(extract_proxies(_payload(response)))
        except ProxyScrapeError:
            pass
        if not found:
            raise NoCredentials("登录成功但没读到代理凭据（试用可能没开通，或面板换了字段）")
        # 去重保序
        seen: set[str] = set()
        unique: list[str] = []
        for item in found:
            if item not in seen:
                seen.add(item)
                unique.append(item)
        return unique

    # --------------------------------------------------------------- composite
    def run_flow(
        self,
        email: str,
        password: str,
        *,
        turnstile_factory: Callable[[], str] | None = None,
        claim: bool = True,
        log: Callable[[str], None] | None = None,
    ) -> dict[str, Any]:
        say = log or (lambda _m: None)
        token = turnstile_factory() if turnstile_factory else ""
        self.register(email, password, turnstile=token)
        say("注册成功")

        if not self.access_token:
            # 有的流程注册只给 cookie，登录一次拿 Bearer
            self.login(email, password, turnstile=turnstile_factory() if turnstile_factory else "")

        claimed = False
        if claim:
            try:
                if self.trial_claimed():
                    say("该账号已经领过试用")
                    claimed = True
                else:
                    self.claim_trial()
                    claimed = True
                    say("试用已领取")
            except NoTrial as exc:
                say(f"领试用失败：{exc}")
                raise

        proxies = self.proxies()
        say(f"读到 {len(proxies)} 条代理")
        return {
            "email": email,
            "password": password,
            "access_token": self.access_token,
            "refresh_token": self.refresh_token,
            "trial_claimed": claimed,
            "proxies": proxies,
            "api_keys": self.api_keys(),
        }


def probe(*, proxy: str = "") -> dict[str, Any]:
    """`doctor`：站点可达性 + 注册端点的 Turnstile 门是否还在。"""
    client = build_client(proxy=proxy, timeout=20.0)
    try:
        page = request(client, "GET", f"{BASE}/v2/sign-up")
        init = request(client, "POST", f"{BASE}{INIT}", json={})
        register = request(
            client,
            "POST",
            f"{BASE}{REGISTER}",
            json={"email": "probe@example.invalid", "password": "Xk9#mQ2vLp7!aB"},
        )
        return {
            "signup_page": f"HTTP {page.status_code} {len(page.text or '')} bytes",
            "init": f"HTTP {init.status_code} {init.text[:60]}",
            "register_without_turnstile": f"HTTP {register.status_code} {(register.text or '')[:120]}",
            "turnstile_site_key": "0x4AAAAAAAFWUVCKyusT9T8r",
        }
    except Transport as exc:
        return {"signup_page": f"unreachable: {exc}"}
    finally:
        client.close()
