"""WaveSpeed 面板 / API 客户端。

## 面板（注册、取 key）

WaveSpeed 只有 OAuth，**没有** `/api/auth/sign-up/email` 这类自建账号接口，
也**没有**创建 key 的 HTTP 接口。所以面板侧的协议调用只有这些：

    GET  /sign-in                    登录页（探测可达性 / 抓 Turnstile sitekey）
    GET  /accesskey                  需要登录态；返回 SPA 外壳
    GET  /api/v3/balance             余额（用面板 key 鉴权）
    GET  /api/v3/models              模型目录

`/accesskey` 的 key 生成是前端点击 + 后端内部接口，接口名未公开，因此：
  * `browser` 腿：真实浏览器点 Generate，读 DOM 里的明文；
  * `session` 腿：带 cookie 打开 `/accesskey`，从页面内嵌的 __NEXT_DATA__ /
    preloaded state 里找已存在的 key 前缀与 id；拿不到明文就抛 `KeyNotObtainable`
    —— 这是明确信号，不是故障。

## 推理 API（两套，语义完全不同）

### A. LLM —— [OI] 兼容

    POST {llm_base}/chat/completions     [OI] 兼容，stream 时返回 SSE
    POST {llm_base}/responses            [OI] Responses 兼容
    POST {llm_base}/messages             Anthropic 兼容
    GET  {llm_base}/models               模型目录

### B. 生成 —— 自有异步任务

    POST {task_base}/{model_id}                    提交，返回 data.id + data.urls.get
    GET  {task_base}/predictions/{id}/result       轮询
    GET  {task_base}/predictions/{id}              详情

操作上必须注意的三件事：

  1. **POST 不可自动重试**。提交即计费，重试会产生重复任务与重复扣费。
  2. **响应体里的 `code` 可能与 HTTP 200 不一致**。HTTP 200 + body.code != 200
     依然是失败，必须解析 body。
  3. **轮询地址由服务端下发**（`data.urls.get`），不要硬编码拼接。

鉴权一律 `Authorization: Bearer <KEY>`。
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
    RateLimited,
    SignupError,
    Transport,
)
from .net import (
    TASK_FAILED,
    TASK_OK,
    body_code,
    build_client,
    chrome_user_agent,
    looks_like_cloudflare,
    request,
    safe_request,
)

BASE = "https://wavespeed.ai"
API_BASE = "https://llm.wavespeed.ai/v1"
TASK_BASE = "https://api.wavespeed.ai/api/v3"

SIGN_IN = "/sign-in"
ACCESS_KEY = "/accesskey"
DASHBOARD = "/dashboard"

_CF_MARKERS = ("just a moment", "cf-browser-verification", "challenge-platform", "attention required")
_TURNSTILE_SITE_RE = re.compile(r"""sitekey["'\s:]+["'](0x4AAAAAA[A-Za-z0-9_-]+)["']""", re.I)

# 面板错误码 → runner 词汇。WaveSpeed 的码是蛇形小写，也接受"人话"消息。
_EMAIL_TAKEN_CODES = (
    "email_already_exists",
    "email_exists",
    "user_already_exists",
    "user_exists",
    "already_registered",
    "account_exists",
)
_EMAIL_BAD_CODES = (
    "invalid_email",
    "email_not_allowed",
    "disposable_email",
    "email_rejected",
    "email_invalid",
    "domain_not_allowed",
)
_PASSWORD_BAD_CODES = (
    "password_too_short",
    "password_too_long",
    "invalid_password",
    "password_compromised",
    "weak_password",
)


def _looks_blocked(response: httpx.Response) -> bool:
    if response.status_code in (403, 503) and "cloudflare" in response.headers.get("server", "").lower():
        if looks_like_cloudflare(response.text or ""):
            return True
    return looks_like_cloudflare(response.text or "")


def _error_code(response: httpx.Response) -> tuple[str, str]:
    try:
        payload = response.json()
    except (json.JSONDecodeError, ValueError):
        return "", (response.text or "")[:300]
    if isinstance(payload, dict):
        code = str(payload.get("code") or payload.get("error") or payload.get("error_code") or "")
        message = str(payload.get("message") or payload.get("error_message") or payload.get("error") or "")
        return code, message
    return "", str(payload)[:300]


def _raise_for(path: str, response: httpx.Response) -> None:
    """把一个失败的调用翻译成 runner 认识的重试词汇。

    顺序很重要：先判"这道门我们过不去"（挑战 / 限流 / 验证码），再判"这个地址
    有问题"（已被注册 / 被拒 / 密码不合规），最后才是通用的 401/403 与兜底。
    否则 409 的 `email_already_exists` 会被上一层的通用分支吃掉。
    """
    code, message = _error_code(response)
    lowered = f"{code} {message}".lower()
    status = response.status_code
    detail = {"status": status, "code": code, "message": message[:400]}

    if _looks_blocked(response):
        raise Blocked(f"{path}: edge answered with a bot challenge", detail=detail)
    if status == 429:
        raise RateLimited(f"{path}: rate limited", detail=detail)
    if "captcha" in lowered or "turnstile" in lowered:
        raise CaptchaRequired(f"{path}: captcha rejected the token ({message or code})", detail=detail)

    if any(k in lowered for k in _EMAIL_TAKEN_CODES) or "already exists" in lowered or "already registered" in lowered:
        raise EmailTaken(f"{path}: address already registered", detail=detail)
    if any(k in lowered for k in _EMAIL_BAD_CODES) or "invalid email" in lowered or "not allowed" in lowered:
        raise EmailRejected(f"{path}: address rejected ({message or code})", detail=detail)
    if any(k in lowered for k in _PASSWORD_BAD_CODES):
        raise PasswordRejected(f"{path}: password rejected ({message or code})", detail=detail)

    if status in (401, 403):
        raise SignupError(f"{path}: not authorised (HTTP {status})", detail=detail)
    raise SignupError(f"{path}: failed (HTTP {status})", detail=detail)


class WaveSpeed:
    """一个会话 = 一份 cookie jar。OAuth 走完后的面板态就挂在这里。"""

    def __init__(
        self,
        *,
        base_url: str = BASE,
        api_base: str = API_BASE,
        task_base: str = TASK_BASE,
        proxy: str = "",
        timeout: float = 45.0,
        language: str = "en-US",
        user_agent: str = "",
        transport: Any = None,
        cookies: list[dict[str, Any]] | None = None,
    ) -> None:
        self.base = (base_url or BASE).rstrip("/")
        self.api_base = (api_base or API_BASE).rstrip("/")
        self.task_base = (task_base or TASK_BASE).rstrip("/")
        self.proxy = proxy
        self.language = language
        self.user_agent = user_agent or chrome_user_agent()
        self.client = build_client(
            proxy=proxy,
            timeout=timeout,
            user_agent=self.user_agent,
            language=language,
            transport=transport,
        )
        self.client.headers.update({"Origin": self.base, "Referer": f"{self.base}/"})
        for cookie in cookies or []:
            try:
                self.client.cookies.set(
                    str(cookie.get("name", "")),
                    str(cookie.get("value", "")),
                    domain=str(cookie.get("domain") or "") or None,
                    path=str(cookie.get("path") or "/"),
                )
            except Exception:  # noqa: BLE001 - 畸形 cookie 直接跳过
                continue

    # ------------------------------------------------------------------ basic
    def __enter__(self) -> "WaveSpeed":
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
        """先打开首页，拿边缘 cookie；被挑战就直接判 Blocked。"""
        response = safe_request(self.client, "GET", f"{self.base}/", retries=2)
        if response.status_code >= 500:
            raise Blocked(f"home answered HTTP {response.status_code}")
        if _looks_blocked(response):
            raise Blocked("home hit a bot challenge")

    def sign_in_page(self) -> dict[str, Any]:
        """打开 /sign-in：探测可达性，顺手抓 Turnstile sitekey。

        WaveSpeed 的 sitekey 不在文档里，只能从页面里挖；挖不到返回空串，
        由调用方决定用解题器还是直接走浏览器。
        """
        response = safe_request(self.client, "GET", f"{self.base}{SIGN_IN}", retries=2)
        if response.status_code >= 400:
            _raise_for(SIGN_IN, response)
        if _looks_blocked(response):
            raise Blocked("sign-in page hit a bot challenge")
        text = response.text or ""
        match = _TURNSTILE_SITE_RE.search(text)
        return {
            "status": response.status_code,
            "bytes": len(text),
            "turnstile_site_key": match.group(1) if match else "",
            "oauth_buttons": [
                name
                for name, needle in (("google", "google"), ("github", "github"), ("aws", "aws"))
                if needle in text.lower()
            ],
            "title": (re.search(r"<title>([^<]*)</title>", text) or [None, ""])[1],
        }

    # -------------------------------------------------------------- 面板 key
    def accesskey_page(self) -> dict[str, Any]:
        """打开 /accesskey（需要登录态），把页面上能读到的 key 信息挖出来。

        能拿到的只有"已存在 key 的前缀/名称/创建时间"这类元信息；**明文拿不到**
        —— 这就是 `KeyNotObtainable` 的来源，也是为什么浏览器腿不可替代。
        """
        response = safe_request(self.client, "GET", f"{self.base}{ACCESS_KEY}", retries=1)
        if response.status_code in (301, 302, 303, 307, 308):
            location = response.headers.get("location", "")
            raise SignupError(f"/accesskey redirected to {location[:120]} (session not valid)")
        if response.status_code >= 400:
            _raise_for(ACCESS_KEY, response)
        if _looks_blocked(response):
            raise Blocked("/accesskey hit a bot challenge")

        text = response.text or ""
        keys: list[dict[str, Any]] = []
        # 1) Next.js 的 __NEXT_DATA__
        match = re.search(r'<script id="__NEXT_DATA__"[^>]*>(.*?)</script>', text, re.S)
        if match:
            try:
                blob = json.loads(match.group(1))
                keys.extend(_collect_keys(blob))
            except json.JSONDecodeError:
                pass
        # 2) 内联的 preloaded state
        for inline in re.findall(r"self\.__next_f\.push\(\[1,\s*\"(.*?)\"\]\)", text, re.S):
            for candidate in re.findall(r"0x4?[A-Za-z0-9_-]{20,}", inline):
                keys.append({"raw": candidate})
        # 3) 页面里任何像 key 的长串（WaveSpeed key 没有固定前缀，只能看长度与字符集）
        for candidate in set(re.findall(r"\b[A-Za-z0-9]{32,64}\b", text)):
            if any(candidate in json.dumps(k) for k in keys):
                continue
            keys.append({"raw": candidate})

        if not keys:
            raise KeyNotObtainable(
                "/accesskey 可访问，但页面上没有可读的 key 元信息；"
                "WaveSpeed 没有创建 key 的 HTTP 接口，必须走浏览器腿点 Generate"
            )
        return {"status": response.status_code, "candidates": keys[:20]}

    # ------------------------------------------------------------- 推理 / 额度
    def _auth(self, key: str, extra: dict[str, str] | None = None) -> dict[str, str]:
        headers = {
            "Authorization": f"Bearer {key}",
            "Content-Type": "application/json",
            "Accept": "application/json",
        }
        headers.update(extra or {})
        return headers

    def balance(self, key: str) -> dict[str, Any]:
        """余额。用面板 key 打 `{task_base}/balance`。"""
        response = safe_request(
            self.client,
            "GET",
            f"{self.task_base}/balance",
            headers=self._auth(key),
            retries=2,
        )
        if response.status_code >= 400:
            _raise_for("/api/v3/balance", response)
        payload = response.json() if response.content else {}
        code = body_code(payload)
        if code != 200:
            raise SignupError(f"/api/v3/balance returned code {code}: {str(payload)[:200]}")
        return payload if isinstance(payload, dict) else {}

    def verify_key(self, key: str) -> dict[str, Any]:
        """校验一把 key 是否可用：先试 LLM /models，再退到 balance。"""
        if not key or len(key) < 16:
            raise NoApiKey("key 看起来不合法（太短）")
        attempts: list[dict[str, Any]] = []
        try:
            response = safe_request(
                self.client,
                "GET",
                f"{self.api_base}/models",
                headers=self._auth(key),
                retries=1,
            )
            attempts.append({"endpoint": "/v1/models", "status": response.status_code})
            if response.status_code == 200:
                payload = response.json() if response.content else {}
                rows = payload.get("data") if isinstance(payload, dict) else None
                return {
                    "ok": True,
                    "via": "llm/models",
                    "models": len(rows) if isinstance(rows, list) else 0,
                    "attempts": attempts,
                }
            if response.status_code in (401, 403):
                _raise_for("/v1/models", response)
        except (SignupError, Transport) as exc:
            attempts.append({"endpoint": "/v1/models", "error": str(exc)[:160]})

        try:
            payload = self.balance(key)
            return {"ok": True, "via": "balance", "balance": payload.get("data"), "attempts": attempts}
        except (SignupError, Transport) as exc:
            attempts.append({"endpoint": "/api/v3/balance", "error": str(exc)[:160]})
        raise NoApiKey(f"key 校验失败：{attempts}")

    def list_models(self, key: str) -> list[dict[str, Any]]:
        response = safe_request(
            self.client,
            "GET",
            f"{self.api_base}/models",
            headers=self._auth(key),
            retries=2,
        )
        if response.status_code >= 400:
            _raise_for("/v1/models", response)
        payload = response.json() if response.content else {}
        rows = payload.get("data") if isinstance(payload, dict) else None
        return rows if isinstance(rows, list) else []

    def chat(self, key: str, body: dict[str, Any]) -> httpx.Response:
        """[OI] 兼容对话。body 原样透传（保留 provider 私有字段）。"""
        return safe_request(
            self.client,
            "POST",
            f"{self.api_base}/chat/completions",
            headers=self._auth(key),
            json=body,
            retries=0,  # 生成类 POST 不重试
        )

    # ------------------------------------------------------------- 生成任务
    def generate(
        self,
        key: str,
        model_id: str,
        payload: dict[str, Any],
        *,
        poll: float = 2.0,
        timeout: float = 300.0,
        log: Callable[[str], None] | None = None,
    ) -> dict[str, Any]:
        """提交一个生成任务并轮询到终态。

        提交体直接平铺模型参数（WaveSpeed 的约定），不是包在 `input` 里。
        """
        say = log or (lambda _m: None)
        response = safe_request(
            self.client,
            "POST",
            f"{self.task_base}/{model_id.lstrip('/')}",
            headers=self._auth(key),
            json=payload,
            retries=0,  # 关键：提交即计费
        )
        if response.status_code >= 400:
            _raise_for(f"/api/v3/{model_id}", response)
        body = response.json() if response.content else {}
        code = body_code(body)
        if code != 200:
            raise SignupError(f"submit returned code {code}: {str(body)[:200]}")

        data = (body or {}).get("data") or {}
        task_id = str(data.get("id") or "")
        if not task_id:
            raise SignupError(f"submit returned no task id: {str(body)[:200]}")

        # 轮询地址由服务端下发，别自己拼
        poll_url = ((data.get("urls") or {}).get("get")) or ""
        if not poll_url:
            poll_url = f"{self.task_base}/predictions/{task_id}/result"
        say(f"task {task_id} submitted, polling {poll_url}")

        deadline = time.time() + timeout
        delay = max(1.0, poll)
        last: dict[str, Any] = {}
        while time.time() < deadline:
            time.sleep(delay)
            delay = min(10.0, delay * 1.35)  # 逐步放宽，官方建议 2s → 5–10s
            response = safe_request(self.client, "GET", poll_url, headers=self._auth(key), retries=3)
            if response.status_code >= 400:
                _raise_for("/predictions/result", response)
            last = response.json() if response.content else {}
            status = str(((last or {}).get("data") or {}).get("status") or "").lower()
            if status in TASK_OK:
                return last
            if status in TASK_FAILED:
                raise SignupError(f"task {task_id} ended as {status}: {str(last)[:300]}")
        raise SignupError(f"task {task_id} did not finish in {timeout:.0f}s (last={str(last)[:200]})")


def _collect_keys(node: Any, depth: int = 0) -> list[dict[str, Any]]:
    """在任意 JSON 结构里找形似 api key 的字段。"""
    out: list[dict[str, Any]] = []
    if depth > 8:
        return out
    if isinstance(node, dict):
        for key, value in node.items():
            lowered = str(key).lower()
            if isinstance(value, str) and any(
                token in lowered for token in ("apikey", "api_key", "key", "token", "secret")
            ):
                if len(value) >= 16:
                    out.append({"field": str(key), "value": value})
            else:
                out.extend(_collect_keys(value, depth + 1))
    elif isinstance(node, list):
        for item in node[:50]:
            out.extend(_collect_keys(item, depth + 1))
    return out


def probe(base_url: str = BASE, *, api_base: str = API_BASE, proxy: str = "", timeout: float = 20.0) -> dict[str, Any]:
    """`doctor` 用：一次便宜的存活检查（不注册、不消耗额度）。"""
    client = build_client(proxy=proxy, timeout=timeout)
    try:
        home = safe_request(client, "GET", f"{base_url.rstrip('/')}/", retries=1)
        sign_in = safe_request(client, "GET", f"{base_url.rstrip('/')}{SIGN_IN}", retries=1)
        models = safe_request(client, "GET", f"{api_base.rstrip('/')}/models", retries=1)
        text = sign_in.text or ""
        match = _TURNSTILE_SITE_RE.search(text)
        return {
            "ok": home.status_code < 500 and not _looks_blocked(home),
            "home": f"HTTP {home.status_code} {len(home.text or '')} bytes",
            "sign_in": f"HTTP {sign_in.status_code} {len(text)} bytes",
            "sign_in_title": (re.search(r"<title>([^<]*)</title>", text) or [None, ""])[1],
            "turnstile_site_key": match.group(1) if match else "",
            "oauth": [n for n in ("google", "github", "aws") if n in text.lower()],
            "llm_models": f"HTTP {models.status_code} (401 正常，说明端点存在且要鉴权)",
            "api_base": api_base,
            "task_base": TASK_BASE,
        }
    finally:
        client.close()
