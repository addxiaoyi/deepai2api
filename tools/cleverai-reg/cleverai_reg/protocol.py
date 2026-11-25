"""Clever AI Hub signup + chat protocol, reconstructed from the shipped SPA.

Two backends sit behind the app:

  Firebase Identity Toolkit   `identitytoolkit.googleapis.com`
      POST /v1/accounts:signUp           {email,password,returnSecureToken}
      POST /v1/accounts:signInWithPassword
      POST /v1/accounts:sendOobCode      {requestType:"VERIFY_EMAIL",idToken}
      POST /v1/accounts:update           {oobCode}   -> consumes the mail link
      POST /v1/accounts:lookup           {idToken}
      POST https://securetoken.googleapis.com/v1/token   grant_type=refresh_token

  appserver (Azure Functions)  `api.cleverai.app/appserver/api/v1`
      POST   /users                      multipart `json={uid,appId,platform,isAnonymousUser}`
                                         -> 201 + wallet (30 renewable + welcome credits)
      GET    /users/{uid}?type=0|1|2     profile + wallet
      GET    /configs?type=engines       94 engine descriptors (id, module, cost)
      POST   /user-settings              per-user settings blob
      POST   /omni/response              SSE chat stream

Every appserver call needs three things at once:

  * `Authorization: Bearer <firebase idToken>`
  * `x-functions-key: <the key the bundle ships>` (Azure Functions host key)
  * `X-Firebase-AppCheck: <app check JWT>`  (see `appcheck.py`)

and the version headers `Version`/`X-Version`; without them the backend answers
with a canned "please update the app" assistant turn instead of the model reply.

The chat body is the SPA's `omni` envelope — `messages[]` of dialog messages with
`type: human|ai` and `contents[]` of `{type: text|tool_call|tool_result, …}`.
"""

from __future__ import annotations

import json
import os
import time
import uuid
from typing import Any, Callable, Iterator

import httpx

from .errors import (
    AppCheckRequired,
    Blocked,
    EmailRejected,
    EmailTaken,
    NoCredit,
    PasswordRejected,
    RateLimited,
    SignupError,
    Transport,
    VerificationPending,
)
from .net import build_client, chrome_user_agent, device_blob, multipart_json, request

IDENTITY = "https://identitytoolkit.googleapis.com/v1"
SECURETOKEN = "https://securetoken.googleapis.com/v1/token"
BASE = "https://api.cleverai.app"
APP_PATH = "/appserver/api/v1"

APP_ID = "1:387451343950:web:7098a29d2a2aa91c751f1f"
WEB_API_KEY = "AIzaSyAdbIs_1isauoMViqf4rmpGUH5UFb3z_JQ"
# Azure Functions 主机密钥：web bundle 里带着它，appserver 才应答。它不是用户级
# 凭据，但确实能直接打上游 —— 所以不进仓库，走环境变量 CLEVERAI_FUNCTIONS_KEY。
FUNCTIONS_KEY = (os.environ.get("CLEVERAI_FUNCTIONS_KEY") or "").strip()
APP_VERSION = "12.2.0"

# Firebase auth error codes that map onto the runner's policy.
_EMAIL_TAKEN = ("EMAIL_EXISTS", "email-already-in-use")
_EMAIL_BAD = ("INVALID_EMAIL", "EMAIL_NOT_FOUND", "MISSING_EMAIL", "DISABLED_USER")
_PASSWORD_BAD = ("WEAK_PASSWORD", "INVALID_PASSWORD", "PASSWORD_DOES_NOT_MEET")
_RATE = ("TOO_MANY_ATTEMPTS_TRY_LATER", "QUOTA_EXCEEDED", "RESOURCE_EXHAUSTED")
_BLOCKED = ("OPERATION_NOT_ALLOWED", "ADMIN_ONLY_OPERATION", "CAPTCHA_CHECK_FAILED")


def _functions_key() -> str:
    """缺密钥时给清楚的理由，而不是让上游回一个含糊的 401。

    每次都读环境变量：测试/网关可以在 import 之后再注入，不必改模块常量。
    """
    current = (os.environ.get("CLEVERAI_FUNCTIONS_KEY") or FUNCTIONS_KEY or "").strip()
    if not current:
        raise SignupError(
            "缺少 CLEVERAI_FUNCTIONS_KEY：把 CleverAI web bundle 里的 x-functions-key 写进环境变量"
        )
    return current


def _auth_error(status: int, body: str) -> SignupError:
    lowered = (body or "")[:600].lower()
    try:
        code = str((json.loads(body or "{}").get("error") or {}).get("message") or "")
    except (json.JSONDecodeError, ValueError, AttributeError):
        code = ""
    detail = {"status": status, "code": code, "body": (body or "")[:300]}
    if status == 429 or any(k.lower() in lowered for k in _RATE):
        return RateLimited(f"identity toolkit rate limited ({code or status})", detail=detail)
    if any(k.lower() in lowered for k in _EMAIL_TAKEN):
        return EmailTaken(f"address already registered ({code})", detail=detail)
    if any(k.lower() in lowered for k in _EMAIL_BAD):
        return EmailRejected(f"address rejected ({code})", detail=detail)
    if any(k.lower() in lowered for k in _PASSWORD_BAD):
        return PasswordRejected(f"password rejected ({code})", detail=detail)
    if any(k.lower() in lowered for k in _BLOCKED):
        return Blocked(f"sign-up disabled for this project ({code})", detail=detail)
    return SignupError(f"identity toolkit failed (HTTP {status} {code})", detail=detail)


class CleverAI:
    """One signup session = one Firebase user = one pool entry."""

    def __init__(
        self,
        *,
        proxy: str = "",
        timeout: float = 60.0,
        language: str = "en-US",
        user_agent: str = "",
        device: dict[str, Any] | None = None,
        app_check: str = "",
        appcheck_getter: Callable[[], str] | None = None,
        transport: Any = None,
    ) -> None:
        self.proxy = proxy
        self.language = language
        self.user_agent = user_agent or chrome_user_agent()
        self.device = device or device_blob(self.user_agent)
        self.base = BASE
        self.client = build_client(
            proxy=proxy, timeout=timeout, user_agent=self.user_agent, language=language, transport=transport
        )
        self.app_check = app_check
        self._appcheck_getter = appcheck_getter
        self._transport = transport
        self.id_token = ""
        self.refresh_token = ""
        self.uid = ""
        self.email = ""

    # ------------------------------------------------------------------ basics
    def __enter__(self) -> "CleverAI":
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()

    def close(self) -> None:
        self.client.close()

    def _appcheck(self, *, force: bool = False) -> str:
        if force and self._appcheck_getter is not None:
            self.app_check = self._appcheck_getter()
        elif not self.app_check and self._appcheck_getter is not None:
            self.app_check = self._appcheck_getter()
        return self.app_check

    def _headers(self, *, extra: dict[str, str] | None = None, json_body: bool = True) -> dict[str, str]:
        headers = {
            "Authorization": f"Bearer {self.id_token}",
            "x-functions-key": _functions_key(),
            "Accept-Language": self.language.split(",")[0].split("-")[0] or "en",
            "Version": APP_VERSION,
            "X-Version": APP_VERSION,
            "X-App-ID": APP_ID,
            "X-UID": self.uid or "UNKNOWN",
            "X-Platform": "web",
            "X-DEVICE": json.dumps(self.device, separators=(",", ":")),
            "X-Firebase-AppCheck": self._appcheck(),
        }
        if json_body:
            headers["Content-Type"] = "application/json"
        if extra:
            headers.update(extra)
        return headers

    # ------------------------------------------------------------------- auth
    def signup(self, email: str, password: str) -> dict[str, Any]:
        response = request(
            self.client,
            "POST",
            f"{IDENTITY}/accounts:signUp?key={WEB_API_KEY}",
            json={"email": email, "password": password, "returnSecureToken": True},
        )
        if response.status_code >= 400:
            raise _auth_error(response.status_code, response.text)
        payload = response.json() or {}
        self._adopt(payload)
        self.email = email
        return payload

    def login(self, email: str, password: str) -> dict[str, Any]:
        response = request(
            self.client,
            "POST",
            f"{IDENTITY}/accounts:signInWithPassword?key={WEB_API_KEY}",
            json={"email": email, "password": password, "returnSecureToken": True},
        )
        if response.status_code >= 400:
            raise _auth_error(response.status_code, response.text)
        payload = response.json() or {}
        self._adopt(payload)
        self.email = email
        return payload

    def _adopt(self, payload: dict[str, Any]) -> None:
        self.id_token = str(payload.get("idToken") or "")
        self.refresh_token = str(payload.get("refreshToken") or "")
        self.uid = str(payload.get("localId") or payload.get("user_id") or self.uid)

    def refresh(self) -> str:
        if not self.refresh_token:
            raise SignupError("no refresh token: sign in first")
        response = request(
            self.client,
            "POST",
            f"{SECURETOKEN}?key={WEB_API_KEY}",
            data={"grant_type": "refresh_token", "refresh_token": self.refresh_token},
            headers={"Content-Type": "application/x-www-form-urlencoded"},
        )
        if response.status_code >= 400:
            raise _auth_error(response.status_code, response.text)
        payload = response.json() or {}
        self.id_token = str(payload.get("id_token") or self.id_token)
        self.refresh_token = str(payload.get("refresh_token") or self.refresh_token)
        return self.id_token

    def send_verification(self) -> None:
        response = request(
            self.client,
            "POST",
            f"{IDENTITY}/accounts:sendOobCode?key={WEB_API_KEY}",
            json={"requestType": "VERIFY_EMAIL", "idToken": self.id_token},
        )
        if response.status_code >= 400:
            raise _auth_error(response.status_code, response.text)

    def apply_verification_link(self, link: str) -> bool:
        """Consume the mail link. Firebase accepts the raw `oobCode`."""
        code = ""
        if "oobCode=" in link:
            code = link.split("oobCode=", 1)[1].split("&", 1)[0]
        if not code:
            raise VerificationPending(f"verification link carries no oobCode: {link[:120]}")
        response = request(
            self.client,
            "POST",
            f"{IDENTITY}/accounts:update?key={WEB_API_KEY}",
            json={"oobCode": code},
        )
        if response.status_code >= 400:
            raise VerificationPending(
                f"verification code rejected (HTTP {response.status_code})", detail=response.text[:200]
            )
        return True

    def account_info(self) -> dict[str, Any]:
        response = request(
            self.client, "POST", f"{IDENTITY}/accounts:lookup?key={WEB_API_KEY}", json={"idToken": self.id_token}
        )
        if response.status_code >= 400:
            raise _auth_error(response.status_code, response.text)
        users = (response.json() or {}).get("users") or []
        return users[0] if users else {}

    # -------------------------------------------------------------- appserver
    def _call(self, method: str, path: str, **kwargs: Any) -> httpx.Response:
        headers = kwargs.pop("headers", None) or self._headers()
        response = request(self.client, method, f"{self.base}{APP_PATH}{path}", headers=headers, **kwargs)
        if response.status_code >= 400:
            self._raise_appserver(path, response)
        return response

    def _raise_appserver(self, path: str, response: httpx.Response) -> None:
        lowered = (response.text or "")[:800].lower()
        status = response.status_code
        detail = {"status": status, "body": (response.text or "")[:400]}
        if status == 401 and "app_check" in lowered:
            # Token went stale mid-batch: force one fresh mint and let the runner retry.
            self._appcheck(force=True)
            raise AppCheckRequired(f"{path}: app check token rejected", detail=detail)
        if status == 429:
            raise RateLimited(f"{path}: rate limited", detail=detail)
        if "no credit" in lowered or "insufficient" in lowered or "paywall" in lowered:
            raise NoCredit(f"{path}: wallet cannot pay for this turn", detail=detail)
        if status in (403, 503):
            raise Blocked(f"{path}: refused (HTTP {status})", detail=detail)
        raise SignupError(f"{path}: failed (HTTP {status})", detail=detail)

    def create_profile(self, *, anonymous: bool = False) -> dict[str, Any]:
        """Provision the appserver-side user. Returns the profile incl. wallet."""
        payload = {"uid": self.uid, "appId": APP_ID, "platform": "web", "isAnonymousUser": anonymous}
        headers = self._headers()
        headers.pop("Content-Type", None)
        response = request(
            self.client,
            "POST",
            f"{self.base}{APP_PATH}/users",
            files=multipart_json(payload),
            headers=headers,
        )
        if response.status_code >= 400:
            self._raise_appserver("/users", response)
        body = response.json() if response.content else {}
        return body if isinstance(body, dict) else {}

    def profile(self, user_type: int = 2) -> dict[str, Any]:
        response = self._call("GET", f"/users/{self.uid}", params={"type": user_type})
        body = response.json() if response.content else {}
        return body if isinstance(body, dict) else {}

    def wallet(self) -> dict[str, Any]:
        return (self.profile() or {}).get("wallet") or {}

    def engines(self) -> list[dict[str, Any]]:
        response = self._call("GET", "/configs", params={"type": "engines"})
        payload = response.json() or {}
        data = payload.get("data")
        rows: Any = data if isinstance(data, list) else (data or {}).get("engines") if isinstance(data, dict) else None
        return [row for row in (rows or []) if isinstance(row, dict)]

    def create_settings(self, settings: dict[str, Any]) -> dict[str, Any]:
        response = self._call("POST", "/user-settings", json=settings)
        body = response.json() if response.content else {}
        return body if isinstance(body, dict) else {}

    # ------------------------------------------------------------------- chat
    def chat(
        self,
        messages: list[dict[str, Any]],
        *,
        engine: str,
        module: str = "CHAT",
        tools: list[dict[str, Any]] | None = None,
        wallet: dict[str, Any] | None = None,
        cost: int = 0,
        thread_id: str = "",
        user_type: str = "FREE",
        timeout: float | None = None,
        stream: bool = True,
    ) -> Iterator[dict[str, Any]]:
        """POST `omni/response` and yield parsed SSE envelopes.

        `messages` uses the app's dialog shape:
            {"id": ..., "type": "human"|"ai", "module": "CHAT",
             "contents": [{"type": "text", "text": ...}, …]}
        """
        thread_id = thread_id or str(uuid.uuid4())
        request_id = str(uuid.uuid4())
        body: dict[str, Any] = {
            "messages": messages,
            "thread": {"id": thread_id},
            "appId": APP_ID,
            "platform": "web",
            "engine": engine,
            "module": module,
            "filters": [],
            "options": {
                "type": user_type,
                "language": self.language.split("-")[0] or "en",
                "tools": tools or [],
            },
            "model": engine,
            "deviceType": "unknown",
            "sequenceNumber": 0,
            "device": self.device,
            "isDemo": False,
            "metadata": {
                "outputProtocol": "mixed-v1",
                "requestId": request_id,
                "capabilities": ["mcp-app-v1"],
            },
        }
        if wallet:
            body["wallet"] = {**wallet, "cost": cost}
        headers = self._headers(
            extra={
                "Accept": "text/event-stream",
                "X-Requested-With": "XMLHttpRequest",
                "X-Experience-Type": "DEFAULT",
                "Cache-Control": "no-cache",
            }
        )
        read_timeout = timeout if timeout is not None else 300.0
        # 流式读取要单独的客户端（超时语义不同），但传输层沿用构造时的那个，
        # 这样离线测试的 MockTransport 也能覆盖到这里。
        stream_kwargs: dict[str, Any] = {
            "timeout": httpx.Timeout(connect=30.0, read=read_timeout, write=60.0, pool=30.0),
            "trust_env": False,
            "headers": {"User-Agent": self.user_agent},
        }
        if self._transport is not None:
            stream_kwargs["transport"] = self._transport
        elif self.proxy:
            stream_kwargs["proxy"] = self.proxy
        http = httpx.Client(**stream_kwargs)
        try:
            if not stream:
                response = http.post(f"{self.base}{APP_PATH}/omni/response", json=body, headers=headers)
                if response.status_code >= 400:
                    self._raise_appserver("/omni/response", response)
                yield from _parse_sse(response.text)
                return
            with http.stream("POST", f"{self.base}{APP_PATH}/omni/response", json=body, headers=headers) as response:
                if response.status_code >= 400:
                    response.read()
                    self._raise_appserver("/omni/response", response)
                for line in response.iter_lines():
                    if not line:
                        continue
                    if line.startswith("data:"):
                        chunk = line[5:].strip()
                        if not chunk:
                            continue
                        try:
                            event = json.loads(chunk)
                        except json.JSONDecodeError:
                            continue
                        if isinstance(event, dict):
                            yield event
        finally:
            http.close()


def _parse_sse(text: str) -> Iterator[dict[str, Any]]:
    for block in (text or "").split("\n\n"):
        for line in block.split("\n"):
            if not line.startswith("data:"):
                continue
            try:
                event = json.loads(line[5:].strip())
            except json.JSONDecodeError:
                continue
            if isinstance(event, dict):
                yield event


# --------------------------------------------------------------------- helpers
def dialog_message(
    text: str,
    *,
    role: str = "human",
    module: str = "CHAT",
    extra_contents: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    """Build one `messages[]` entry in the shape the appserver expects."""
    contents: list[dict[str, Any]] = []
    if text:
        contents.append({"type": "text", "text": text, "item_id": str(uuid.uuid4())})
    contents.extend(extra_contents or [])
    return {
        "id": str(uuid.uuid4()),
        "type": "human" if role in ("human", "user") else "ai",
        "module": module,
        "contents": contents,
        "createdAt": int(time.time() * 1000),
        "platform": {"type": "web", "appVersion": APP_VERSION, "appBuildNumber": 1},
    }


def text_of(event: dict[str, Any], *, include_thinking: bool = False) -> str:
    """Concatenate the text payload of one SSE envelope."""
    out: list[str] = []
    for item in (event.get("delta") or {}).get("content") or []:
        if not isinstance(item, dict):
            continue
        kind = item.get("type")
        if kind == "text" or (include_thinking and kind == "thinking_text"):
            out.append(str(item.get("text") or ""))
    return "".join(out)


def probe(*, proxy: str = "", timeout: float = 20.0) -> dict[str, Any]:
    """`doctor`: is the backend reachable and does it still want an app key?"""
    client = build_client(proxy=proxy, timeout=timeout)
    try:
        root = request(client, "GET", "https://api.cleverai.app/")
        unauth = request(client, "GET", f"https://api.cleverai.app{APP_PATH}/configs")
        return {
            "backend": f"HTTP {root.status_code}",
            "configs_without_auth": f"HTTP {unauth.status_code}",
            "app_id": APP_ID,
            "app_version": APP_VERSION,
            "functions_key": FUNCTIONS_KEY[:12] + "…",
        }
    except Transport as exc:
        return {"backend": f"unreachable: {exc}"}
    finally:
        client.close()
