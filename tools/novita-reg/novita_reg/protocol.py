"""Novita 面板 / API 客户端。

## 面板（注册、登录、取 key）—— 接口全部来自线上 chunk 反查

    基址 I = https://api-server.novita.ai

    POST {I}/v1/user/register      {email,password,confirmPassword,cloudflareToken,
                                    redirectUrl,allowNotification,fromInviteCode,
                                    agreementVersion, ...utm}
    POST {I}/v1/user/login         {email,password,cloudflareToken,redirectUrl,
                                    fromInviteCode,agreementVersion}
    GET  {I}/v1/user/info          当前用户（登录态）
    POST {I}/v1/user/email/verify  激活邮件里的验证
    GET  {I}/v2/user/key           列 key（返回 keys[]）
    POST {I}/v2/user/key           建 key（**明文只在响应里出现一次**）
    PUT  {I}/v2/user/key           改 key（model-access / ip-access 策略）
    DELETE {I}/v2/user/key/{id}    删 key

Turnstile sitekey：`0x4AAAAAAAaG28VfN_OxkED8`（chunk 99745 的 fE），
token 字段名是 `cloudflareToken`。

## 推理

    [OI] 兼容   {API_BASE}/v1/chat/completions     API_BASE = https://api.novita.ai/openai
    Anthropic   https://api.novita.ai/anthropic/v1/messages
    图像异步    POST {API}/v3/async/txt2img → {"task_id":…}
                GET  {API}/v3/async/task-result?task_id=…
    余额        GET  {API}/openapi/v1/billing/balance/detail （单位 1/10000 USD）

## OpenAPI 侧的限制

`/openapi/v2/user/key` 只能**读**（返回 maskedKey、expireTime、lastUsedAt），
不能创建 / 删除 / 改有效期 —— 创建必须走面板的 `/v2/user/key`。
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
    KeyNotObtainable,
    NoApiKey,
    PasswordRejected,
    QuotaExhausted,
    RateLimited,
    SignupError,
    VerifyRequired,
    VerificationPending,
)
from .net import build_client, chrome_user_agent, looks_like_cloudflare, request, safe_request

PANEL = "https://novita.ai"
PANEL_API = "https://api-server.novita.ai"
API_BASE = "https://api.novita.ai"
OPENAI_BASE = "https://api.novita.ai/openai"
ANTHROPIC_BASE = "https://api.novita.ai/anthropic"

TURNSTILE_SITE_KEY = "0x4AAAAAAAaG28VfN_OxkED8"
TURNSTILE_FIELD = "cloudflareToken"

REGISTER = "/v1/user/register"
LOGIN = "/v1/user/login"
INFO = "/v1/user/info"
VERIFY_EMAIL = "/v1/user/email/verify"
KEYS = "/v2/user/key"

# 前端 agreementVersion 的 consentRequestValue，chunk 63709
DEFAULT_AGREEMENT_VERSION = "2025-01-01"

# 面板错误码 → runner 词汇（chunk 15825 的错误表里能读到这些键）
_EMAIL_TAKEN = ("USER_ALREADY_EXIST", "USER_EXISTS", "EMAIL_ALREADY_EXISTS", "user_already_exists")
_EMAIL_BAD = ("INVALID_EMAIL", "EMAIL_NOT_ALLOWED", "EMAIL_FORMAT_ERROR", "DISPOSABLE_EMAIL")
_PASSWORD_BAD = ("PASSWORD_TOO_SHORT", "PASSWORD_TOO_LONG", "PASSWORD_FORMAT_ERROR", "WEAK_PASSWORD")
_LOCKED = ("USER_IS_LOCKED",)
_TURNSTILE = ("TURNSTILE", "CAPTCHA", "CLOUDFLARE")


def _looks_blocked(response: httpx.Response) -> bool:
    if response.status_code in (403, 503) and "cloudflare" in response.headers.get("server", "").lower():
        if looks_like_cloudflare(response.text or ""):
            return True
    return looks_like_cloudflare(response.text or "")


def _panel_error(response: httpx.Response) -> tuple[str, str]:
    """Novita 的错误体通常是 `{errorNo, errorInfo}` 或 `{code,message}`。"""
    try:
        payload = response.json()
    except (json.JSONDecodeError, ValueError):
        return "", (response.text or "")[:300]
    if isinstance(payload, dict):
        code = str(
            payload.get("errorNo")
            or payload.get("code")
            or payload.get("reason")
            or payload.get("error")
            or ""
        )
        message = str(
            payload.get("errorInfo")
            or payload.get("message")
            or payload.get("msg")
            or payload.get("error")
            or ""
        )
        return code, message
    return "", str(payload)[:300]


def _raise_for(path: str, response: httpx.Response) -> None:
    """把一个失败的面板调用翻译成 runner 认识的重试词汇。"""
    code, message = _panel_error(response)
    lowered = f"{code} {message}".lower()
    status = response.status_code
    detail = {"status": status, "code": code, "message": message[:400]}

    if _looks_blocked(response):
        raise Blocked(f"{path}: edge answered with a bot challenge", detail=detail)
    if status == 402:
        raise QuotaExhausted(f"{path}: insufficient balance", detail=detail)
    if status == 429:
        raise RateLimited(f"{path}: rate limited", detail=detail)
    if any(k.lower() in lowered for k in _TURNSTILE):
        raise CaptchaRequired(f"{path}: turnstile rejected the token ({message or code})", detail=detail)
    if any(k.lower() in lowered for k in _LOCKED) or "locked" in lowered:
        raise RateLimited(f"{path}: account locked ({message or code})", detail=detail)
    if any(k.lower() in lowered for k in _EMAIL_TAKEN) or "already" in lowered or "exist" in lowered:
        raise EmailTaken(f"{path}: address already registered", detail=detail)
    if any(k.lower() in lowered for k in _EMAIL_BAD) or "invalid email" in lowered:
        raise EmailRejected(f"{path}: address rejected ({message or code})", detail=detail)
    if any(k.lower() in lowered for k in _PASSWORD_BAD):
        raise PasswordRejected(f"{path}: password rejected ({message or code})", detail=detail)
    if "identity verification" in lowered or "real name" in lowered or "kyc" in lowered:
        raise VerifyRequired(f"{path}: identity verification required", detail=detail)
    if status in (401, 403):
        raise SignupError(f"{path}: not authorised (HTTP {status})", detail=detail)
    raise SignupError(f"{path}: failed (HTTP {status})", detail=detail)


def _body_ok(payload: Any) -> bool:
    """Novita 有些接口 HTTP 200 但 body 里带 errorNo。"""
    if not isinstance(payload, dict):
        return True
    for key in ("errorNo", "code"):
        value = payload.get(key)
        if value in (None, "", 0, "0", "OK", "ok"):
            continue
        try:
            if int(value) == 0:
                continue
        except (TypeError, ValueError):
            pass
        return False
    return True


class Novita:
    """一个会话 = 一份 cookie jar。"""

    def __init__(
        self,
        *,
        panel: str = PANEL,
        panel_api: str = PANEL_API,
        api_base: str = API_BASE,
        openai_base: str = OPENAI_BASE,
        proxy: str = "",
        timeout: float = 45.0,
        language: str = "en-US",
        user_agent: str = "",
        transport: Any = None,
        cookies: list[dict[str, Any]] | None = None,
        token: str = "",
    ) -> None:
        self.panel = (panel or PANEL).rstrip("/")
        self.panel_api = (panel_api or PANEL_API).rstrip("/")
        self.api_base = (api_base or API_BASE).rstrip("/")
        self.openai_base = (openai_base or OPENAI_BASE).rstrip("/")
        self.proxy = proxy
        self.token = token
        self.client = build_client(
            proxy=proxy,
            timeout=timeout,
            user_agent=user_agent or chrome_user_agent(),
            language=language,
            transport=transport,
        )
        self.client.headers.update(
            {
                "Origin": self.panel,
                "Referer": f"{self.panel}/user/register",
                "Accept": "application/json, text/plain, */*",
            }
        )
        for cookie in cookies or []:
            try:
                self.client.cookies.set(
                    str(cookie.get("name", "")),
                    str(cookie.get("value", "")),
                    domain=str(cookie.get("domain") or "") or None,
                    path=str(cookie.get("path") or "/"),
                )
            except Exception:  # noqa: BLE001
                continue

    # ------------------------------------------------------------------ basic
    def __enter__(self) -> "Novita":
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()

    def close(self) -> None:
        self.client.close()

    def cookies(self) -> list[dict[str, str]]:
        out: list[dict[str, str]] = []
        for cookie in self.client.cookies.jar:
            out.append(
                {
                    "name": cookie.name,
                    "value": cookie.value,
                    "domain": cookie.domain or "",
                    "path": cookie.path or "/",
                }
            )
        return out

    def warm(self) -> None:
        """打开注册页拿边缘 cookie；被挑战直接判 Blocked。"""
        response = safe_request(self.client, "GET", f"{self.panel}/user/register", retries=2)
        if response.status_code >= 500:
            raise Blocked(f"register page answered HTTP {response.status_code}")
        if _looks_blocked(response):
            raise Blocked("register page hit a bot challenge")

    # ---------------------------------------------------------------- 注册
    def register(
        self,
        email: str,
        password: str,
        *,
        turnstile: str = "",
        invite_code: str = "",
        redirect_url: str = "",
        agreement_version: str = DEFAULT_AGREEMENT_VERSION,
        utm: dict[str, str] | None = None,
    ) -> dict[str, Any]:
        """POST /v1/user/register。

        请求体字段来自 SignupForm 的实参：email / password / confirmPassword /
        cloudflareToken / redirectUrl / allowNotification / fromInviteCode /
        agreementVersion，外加 utm_* 五件套。
        """
        payload: dict[str, Any] = {
            "email": email,
            "password": password,
            "confirmPassword": password,
            TURNSTILE_FIELD: turnstile,
            "redirectUrl": redirect_url or f"{self.panel}/user/login",
            "allowNotification": True,
            "fromInviteCode": invite_code or "",
            "agreementVersion": agreement_version,
        }
        for key, value in (utm or {}).items():
            if value:
                payload[key] = value

        response = safe_request(
            self.client,
            "POST",
            f"{self.panel_api}{REGISTER}",
            json=payload,
            headers={"Content-Type": "application/json"},
            retries=0,  # 注册不能重放
        )
        if response.status_code >= 400:
            _raise_for(REGISTER, response)
        body = response.json() if response.content else {}
        if not _body_ok(body):
            # HTTP 200 + errorNo：按同一套映射再判一次
            fake = httpx.Response(400, json=body, request=response.request)
            _raise_for(REGISTER, fake)
        return body if isinstance(body, dict) else {}

    def login(self, email: str, password: str, *, turnstile: str = "") -> dict[str, Any]:
        payload: dict[str, Any] = {
            "email": email,
            "password": password,
            TURNSTILE_FIELD: turnstile,
            "redirectUrl": f"{self.panel}/console",
            "fromInviteCode": "",
            "agreementVersion": DEFAULT_AGREEMENT_VERSION,
        }
        response = safe_request(
            self.client,
            "POST",
            f"{self.panel_api}{LOGIN}",
            json=payload,
            headers={"Content-Type": "application/json"},
            retries=0,
        )
        if response.status_code >= 400:
            _raise_for(LOGIN, response)
        body = response.json() if response.content else {}
        if not _body_ok(body):
            fake = httpx.Response(400, json=body, request=response.request)
            _raise_for(LOGIN, fake)
        return body if isinstance(body, dict) else {}

    def info(self) -> dict[str, Any]:
        response = safe_request(self.client, "GET", f"{self.panel_api}{INFO}", retries=1)
        if response.status_code >= 400:
            if response.status_code in (401, 403):
                return {}
            _raise_for(INFO, response)
        payload = response.json() if response.content else {}
        return payload if isinstance(payload, dict) else {}

    def signed_in(self) -> bool:
        try:
            payload = self.info()
        except SignupError:
            return False
        return bool(payload) and _body_ok(payload)

    # ------------------------------------------------------------ 邮箱验证
    def verify_email(self, token: str) -> bool:
        """激活邮件里的 token。链接形态不公开，所以两种都试。"""
        attempts = [
            ("POST", f"{self.panel_api}{VERIFY_EMAIL}", {"token": token}),
            ("GET", f"{self.panel_api}{VERIFY_EMAIL}", None),
        ]
        last: Exception | None = None
        for method, url, body in attempts:
            try:
                response = safe_request(
                    self.client,
                    method,
                    url,
                    json=body,
                    params=None if body else {"token": token},
                    retries=1,
                )
                if response.status_code < 400:
                    payload = response.json() if response.content else {}
                    if _body_ok(payload):
                        return True
                else:
                    _raise_for(VERIFY_EMAIL, response)
            except (SignupError, Exception) as exc:  # noqa: BLE001
                last = exc
                continue
        raise VerificationPending(f"邮箱验证失败：{last}")

    # ------------------------------------------------------------------ key
    def create_key(self, *, name: str, expire_time: str = "permanent", **extra: Any) -> dict[str, Any]:
        """POST /v2/user/key —— 明文只在这次响应里出现。

        前端在发请求前会先用 QP() 把 expireTime 归一（Permanent / 90d / 30d / 24h
        这类可读值 → 具体秒数或枚举），这里接受可读值，同时允许直接给数字。
        """
        payload: dict[str, Any] = {
            "name": name,
            "expireTime": _normalize_expire(expire_time),
            **extra,
        }
        response = safe_request(
            self.client,
            "POST",
            f"{self.panel_api}{KEYS}",
            json=payload,
            headers={"Content-Type": "application/json"},
            retries=0,  # 建 key 不能重放（会多建一把）
        )
        if response.status_code >= 400:
            _raise_for(KEYS, response)
        body = response.json() if response.content else {}
        if not _body_ok(body):
            raise KeyNotObtainable(f"建 key 被拒：{str(body)[:300]}")
        return body if isinstance(body, dict) else {}

    def list_keys(self) -> list[dict[str, Any]]:
        response = safe_request(self.client, "GET", f"{self.panel_api}{KEYS}", retries=2)
        if response.status_code >= 400:
            _raise_for(KEYS, response)
        payload = response.json() if response.content else {}
        if isinstance(payload, dict):
            rows = payload.get("keys") or payload.get("list") or []
            return rows if isinstance(rows, list) else []
        return []

    def openapi_list_keys(self) -> list[dict[str, Any]]:
        """OpenAPI 侧的只读列表：返回 maskedKey / expireTime / lastUsedAt。"""
        response = safe_request(
            self.client,
            "GET",
            f"{self.api_base}/openapi/v2/user/key",
            params={"includePolicySummary": "true"},
            headers=self._auth(self.token) if self.token else {},
            retries=2,
        )
        if response.status_code >= 400:
            _raise_for("/openapi/v2/user/key", response)
        payload = response.json() if response.content else {}
        if isinstance(payload, dict):
            rows = payload.get("keys") or payload.get("data") or []
            return rows if isinstance(rows, list) else []
        return []

    def set_key_policy(self, key_id: str, *, model_ids: list[str] | None = None) -> dict[str, Any]:
        """PUT /v2/user/key —— 放开 model-access（默认策略会挡掉一部分模型）。"""
        payload: dict[str, Any] = {"id": key_id}
        if model_ids is not None:
            payload["modelAccess"] = model_ids
        response = safe_request(
            self.client,
            "PUT",
            f"{self.panel_api}{KEYS}",
            json=payload,
            headers={"Content-Type": "application/json"},
            retries=0,
        )
        if response.status_code >= 400:
            _raise_for(KEYS, response)
        return response.json() if response.content else {}

    # ---------------------------------------------------------------- 推理
    def _auth(self, key: str) -> dict[str, str]:
        return {
            "Authorization": f"Bearer {key}",
            "Content-Type": "application/json",
            "Accept": "application/json",
        }

    def verify_key(self, key: str) -> dict[str, Any]:
        """校验一把 key：GET /openai/v1/models（401 覆盖缺失/格式错/已删除）。"""
        if not key or not key.startswith("sk_"):
            raise NoApiKey("Novita key 应以 sk_ 开头")
        response = safe_request(
            self.client,
            "GET",
            f"{self.openai_base}/v1/models",
            headers=self._auth(key),
            retries=1,
        )
        if response.status_code >= 400:
            _raise_for("/openai/v1/models", response)
        payload = response.json() if response.content else {}
        rows = payload.get("data") if isinstance(payload, dict) else None
        return {"ok": True, "via": "openai/models", "models": len(rows) if isinstance(rows, list) else 0}

    def list_models(self, key: str) -> list[dict[str, Any]]:
        response = safe_request(
            self.client,
            "GET",
            f"{self.openai_base}/v1/models",
            headers=self._auth(key),
            retries=2,
        )
        if response.status_code >= 400:
            _raise_for("/openai/v1/models", response)
        payload = response.json() if response.content else {}
        rows = payload.get("data") if isinstance(payload, dict) else None
        return rows if isinstance(rows, list) else []

    def balance(self) -> dict[str, Any]:
        """余额：单位 1/10000 USD。"""
        headers = self._auth(self.token) if self.token else {}
        response = safe_request(
            self.client,
            "GET",
            f"{self.api_base}/openapi/v1/billing/balance/detail",
            headers=headers,
            retries=2,
        )
        if response.status_code >= 400:
            _raise_for("/openapi/v1/billing/balance/detail", response)
        payload = response.json() if response.content else {}
        out = payload if isinstance(payload, dict) else {}
        raw = out.get("availableBalance") or out.get("balance") or out.get("credit")
        try:
            out["usd"] = round(int(raw) / 10000, 4) if raw is not None else None
        except (TypeError, ValueError):
            out["usd"] = None
        return out

    def chat(self, key: str, body: dict[str, Any]) -> httpx.Response:
        """[OI] 兼容对话。body 原样透传（enable_thinking / top_k / min_p 等私有字段）。"""
        return safe_request(
            self.client,
            "POST",
            f"{self.openai_base}/v1/chat/completions",
            headers=self._auth(key),
            json=body,
            retries=0,
        )

    def async_image(self, key: str, model_name: str, request_body: dict[str, Any]) -> str:
        """POST /v3/async/txt2img —— 参数包在 `request` 对象里（与 WaveSpeed 不同）。"""
        response = safe_request(
            self.client,
            "POST",
            f"{self.api_base}/v3/async/txt2img",
            headers=self._auth(key),
            json={"model_name": model_name, "request": request_body},
            retries=0,
        )
        if response.status_code >= 400:
            _raise_for("/v3/async/txt2img", response)
        payload = response.json() if response.content else {}
        task_id = str((payload or {}).get("task_id") or (payload or {}).get("taskId") or "")
        if not task_id:
            raise SignupError(f"txt2img returned no task_id: {str(payload)[:200]}")
        return task_id

    def poll_task(self, key: str, task_id: str, *, poll: float = 3.0, timeout: float = 300.0) -> dict[str, Any]:
        """GET /v3/async/task-result?task_id=… 状态机见下。"""
        deadline = time.time() + timeout
        delay = max(1.0, poll)
        last: dict[str, Any] = {}
        while time.time() < deadline:
            time.sleep(delay)
            delay = min(8.0, delay * 1.3)
            response = safe_request(
                self.client,
                "GET",
                f"{self.api_base}/v3/async/task-result",
                params={"task_id": task_id},
                headers=self._auth(key),
                retries=3,
            )
            if response.status_code >= 400:
                _raise_for("/v3/async/task-result", response)
            last = response.json() if response.content else {}
            status = str((last or {}).get("task", {}).get("status") or (last or {}).get("status") or "").upper()
            if status == "TASK_STATUS_SUCCEED":
                return last
            if status == "TASK_STATUS_FAILED":
                reason = (last or {}).get("task", {}).get("reason") or (last or {}).get("reason") or ""
                raise SignupError(f"task {task_id} failed: {reason or str(last)[:200]}")
        raise SignupError(f"task {task_id} did not finish in {timeout:.0f}s")

    # ------------------------------------------------------------- 复合流程
    def signup_flow(
        self,
        email: str,
        password: str,
        *,
        turnstile: str = "",
        verify: bool = True,
        provider: Any = None,
        box: Any = None,
        verify_timeout: float = 180.0,
        key_name: str = "",
        keys: int = 1,
        expire_time: str = "permanent",
        invite_code: str = "",
        log: Callable[[str], None] | None = None,
    ) -> dict[str, Any]:
        """完整协议流程：注册 → 激活 → 建 key → 校验。"""
        say = log or (lambda _m: None)
        self.warm()
        body = self.register(email, password, turnstile=turnstile, invite_code=invite_code)
        say(f"注册请求已提交 {email}")

        email_verified: bool | None = None
        if verify and provider is not None and box is not None:
            link = provider.wait_for_link(
                box,
                timeout=verify_timeout,
                must_contain=("verify", "activat", "token", "confirm"),
            )
            if not link:
                raise VerificationPending(f"{email} 在 {verify_timeout:.0f}s 内没收到激活邮件")
            token = _token_from_link(link)
            if token:
                self.verify_email(token)
                email_verified = True
                say(f"邮箱已验证 {email}")
            else:
                # 链接本身就是激活入口：GET 一次即可
                response = safe_request(self.client, "GET", link, retries=1)
                if response.status_code >= 400:
                    raise VerificationPending(f"激活链接不可用（HTTP {response.status_code}）")
                email_verified = True
                say(f"已跟随激活链接 {email}")
        else:
            email_verified = None

        # 注册后 cookie 一般已生效；再登录一次确保会话可用
        if not self.signed_in():
            try:
                self.login(email, password, turnstile=turnstile)
            except SignupError as exc:
                say(f"注册后登录失败（{exc}），继续尝试建 key")

        api_key = ""
        key_record: dict[str, Any] = {}
        try:
            key_record = self.create_key(name=key_name or f"{email.split('@')[0]}-{int(time.time())}", expire_time=expire_time)
            api_key = str(
                key_record.get("key")
                or key_record.get("apiKey")
                or key_record.get("plaintext")
                or (key_record.get("data") or {}).get("key")
                or ""
            )
        except KeyNotObtainable as exc:
            raise NoApiKey(f"注册成功但建 key 失败：{exc}") from exc
        if not api_key:
            raise NoApiKey(f"建 key 的响应里没有明文：{str(key_record)[:300]}")

        extra_keys: list[dict[str, str]] = []
        for index in range(max(0, keys - 1)):
            try:
                more = self.create_key(
                    name=f"{key_name or email.split('@')[0]}-{index + 2}",
                    expire_time=expire_time,
                )
            except SignupError:
                break
            token = str(more.get("key") or more.get("apiKey") or "")
            if token:
                extra_keys.append({"key": token, "source": "created"})

        info = self.info() or {}
        return {
            "email": email,
            "password": password,
            "name": str((info.get("user") or info).get("nickname") or email.split("@")[0]),
            "api_key": api_key,
            "api_key_source": "panel-api",
            "extra_keys": extra_keys,
            "key_id": str(key_record.get("id") or key_record.get("keyId") or ""),
            "key_prefix": api_key[:8],
            "key_last4": api_key[-4:],
            "expire_time": str(key_record.get("expireTime") or expire_time),
            "organization_id": str((info.get("user") or info).get("teamId") or ""),
            "user_id": str((info.get("user") or info).get("userId") or (info.get("user") or info).get("id") or ""),
            "email_verified": email_verified,
            "cookies": self.cookies(),
        }


def _normalize_expire(value: Any) -> Any:
    """前端 QP() 的等价物：可读值 → 面板接受的数字（秒）。"""
    if isinstance(value, (int, float)):
        return int(value)
    text = str(value or "permanent").strip().lower()
    table = {"permanent": -1, "never": -1, "0": -1, "90d": 90 * 86400, "30d": 30 * 86400, "24h": 86400}
    if text in table:
        return table[text]
    if text.isdigit():
        return int(text)
    return -1


def _token_from_link(link: str) -> str:
    match = re.search(r"[?&](?:token|code|verifyToken|activationToken)=([A-Za-z0-9._~-]+)", link)
    return match.group(1) if match else ""


def probe(panel: str = PANEL, *, panel_api: str = PANEL_API, api_base: str = API_BASE, proxy: str = "", timeout: float = 20.0) -> dict[str, Any]:
    """`doctor` 用：一次便宜的存活检查（不注册、不消耗额度）。"""
    client = build_client(proxy=proxy, timeout=timeout)
    try:
        register = safe_request(client, "GET", f"{panel.rstrip('/')}/user/register", retries=1)
        login = safe_request(client, "GET", f"{panel.rstrip('/')}/user/login", retries=1)
        models = safe_request(client, "GET", f"{api_base.rstrip('/')}/openai/v1/models", retries=1)
        text = register.text or ""
        sitekey = TURNSTILE_SITE_KEY if TURNSTILE_SITE_KEY in text else ""
        if not sitekey:
            match = re.search(r'0x4AAAAAA[A-Za-z0-9_-]+', text)
            sitekey = match.group(0) if match else TURNSTILE_SITE_KEY
        return {
            "ok": register.status_code < 500 and not _looks_blocked(register),
            "register_page": f"HTTP {register.status_code} {len(text)} bytes",
            "login_page": f"HTTP {login.status_code} {len(login.text or '')} bytes",
            "turnstile_site_key": sitekey,
            "turnstile_field": TURNSTILE_FIELD,
            "openai_models": f"HTTP {models.status_code} (401 正常，说明端点存在且要鉴权)",
            "panel_api": panel_api,
            "openai_base": f"{api_base.rstrip('/')}/openai",
        }
    finally:
        client.close()
