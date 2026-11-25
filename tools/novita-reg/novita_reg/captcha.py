"""Turnstile 解题：capsolver / 2captcha / 真机渲染 / 手工 token。

Novita 的注册与登录**都**强制 Turnstile：没有有效 token 时服务端直接回
`500 CLOUDFLARE_CHECK_ERROR`（注册）或 `400 VERIFY_TOKEN_FAILED`（登录）。
sitekey 是 `0x4AAAAAAAaG28VfN_OxkED8`，从 webpack 常量里抠出来的。

四种方式的分工：

    none      不发 token —— 只在出口信誉极好时才有意义，默认会失败
    manual    直接把 --captcha-key 当 token 用（人工在浏览器里过完复制）
    capsolver / 2captcha   打码平台，返回 token 字符串
    browser   起真浏览器，让 Turnstile 自己过，然后从 DOM 里读 token
"""

from __future__ import annotations

import time
from dataclasses import dataclass
from typing import Any

from .errors import CaptchaRequired
from .net import TURNSTILE_SITE_KEY, build_client, request


@dataclass
class CaptchaConfig:
    provider: str = "none"      # none | manual | capsolver | 2captcha | browser | auto
    api_key: str = ""
    site_key: str = TURNSTILE_SITE_KEY
    page_url: str = "https://novita.ai/user/register"
    action: str = ""
    timeout: float = 150.0
    poll: float = 4.0


class Solver:
    provider = "none"

    def __init__(self, cfg: CaptchaConfig, *, proxy: str = "", browser: Any = None) -> None:
        self.cfg = cfg
        self.proxy = proxy
        self.browser = browser

    def solve(self, *, page_url: str = "", action: str = "") -> str:
        return ""

    def report_bad(self, token: str) -> None:
        return None


class NoSolver(Solver):
    provider = "none"


class ManualSolver(Solver):
    """`--captcha-key` 就是 token 本身。多账号时需要每号一个，所以用 `;` 分隔排队。"""

    provider = "manual"

    def __init__(self, cfg: CaptchaConfig, **kwargs: Any) -> None:
        super().__init__(cfg, **kwargs)
        self._tokens = [t.strip() for t in (cfg.api_key or "").split(";") if t.strip()]

    def solve(self, *, page_url: str = "", action: str = "") -> str:
        if not self._tokens:
            raise CaptchaRequired("manual 模式需要 --captcha-key <turnstile token>")
        return self._tokens.pop(0)


class _ApiSolver(Solver):
    """打码平台的公共骨架：createTask → poll → 拿 token。"""

    create_url = ""
    result_url = ""
    task_type = ""

    def __init__(self, cfg: CaptchaConfig, **kwargs: Any) -> None:
        super().__init__(cfg, **kwargs)
        if not cfg.api_key:
            raise CaptchaRequired(f"{self.provider} 需要 --captcha-key")
        self.client = build_client(proxy=self.proxy, timeout=30.0)

    def _create_payload(self, page_url: str, action: str) -> dict[str, Any]:
        raise NotImplementedError

    def _result_payload(self, task_id: str) -> dict[str, Any]:
        raise NotImplementedError

    def _extract(self, payload: dict[str, Any]) -> str:
        raise NotImplementedError

    def solve(self, *, page_url: str = "", action: str = "") -> str:
        page = page_url or self.cfg.page_url
        created = request(self.client, "POST", self.create_url, json=self._create_payload(page, action))
        data = created.json() if created.status_code == 200 else {}
        task_id = str(data.get("taskId") or data.get("task_id") or "")
        if not task_id:
            raise CaptchaRequired(f"{self.provider} 建任务失败：{created.text[:200]}")
        deadline = time.time() + self.cfg.timeout
        while time.time() < deadline:
            time.sleep(self.cfg.poll)
            polled = request(self.client, "POST", self.result_url, json=self._result_payload(task_id))
            if polled.status_code != 200:
                continue
            token = self._extract(polled.json())
            if token:
                return token
        raise CaptchaRequired(f"{self.provider} 超时未出 token（{self.cfg.timeout:.0f}s）")


class CapSolver(_ApiSolver):
    provider = "capsolver"
    create_url = "https://api.capsolver.com/createTask"
    result_url = "https://api.capsolver.com/getTaskResult"
    task_type = "AntiTurnstileTaskProxyLess"

    def _create_payload(self, page_url: str, action: str) -> dict[str, Any]:
        task: dict[str, Any] = {
            "type": self.task_type,
            "websiteURL": page_url,
            "websiteKey": self.cfg.site_key,
        }
        if action:
            task["metadata"] = {"action": action}
        return {"clientKey": self.cfg.api_key, "task": task}

    def _result_payload(self, task_id: str) -> dict[str, Any]:
        return {"clientKey": self.cfg.api_key, "taskId": task_id}

    def _extract(self, payload: dict[str, Any]) -> str:
        if str(payload.get("status", "")).lower() != "ready":
            return ""
        solution = payload.get("solution") or {}
        return str(solution.get("token") or solution.get("gRecaptchaResponse") or "")


class TwoCaptcha(_ApiSolver):
    provider = "2captcha"
    create_url = "https://api.2captcha.com/createTask"
    result_url = "https://api.2captcha.com/getTaskResult"
    task_type = "TurnstileTaskProxyless"

    def _create_payload(self, page_url: str, action: str) -> dict[str, Any]:
        task: dict[str, Any] = {
            "type": self.task_type,
            "websiteURL": page_url,
            "websiteKey": self.cfg.site_key,
        }
        if action:
            task["action"] = action
        return {"clientKey": self.cfg.api_key, "task": task}

    def _result_payload(self, task_id: str) -> dict[str, Any]:
        return {"clientKey": self.cfg.api_key, "taskId": task_id}

    def _extract(self, payload: dict[str, Any]) -> str:
        if str(payload.get("status", "")).lower() != "ready":
            return ""
        return str((payload.get("solution") or {}).get("token") or "")


class BrowserSolver(Solver):
    """真机过 Turnstile：`browser` 必须是 browser.BrowserEngine 实例。"""

    provider = "browser"

    def solve(self, *, page_url: str = "", action: str = "") -> str:
        if self.browser is None:
            raise CaptchaRequired("browser 解题需要浏览器引擎")
        token = self.browser.solve_turnstile(page_url or self.cfg.page_url, timeout=self.cfg.timeout)
        if not token:
            raise CaptchaRequired("浏览器里 Turnstile 没出 token")
        return token


def build_solver(cfg: CaptchaConfig, *, proxy: str = "", browser: Any = None) -> Solver:
    provider = (cfg.provider or "none").lower()
    if provider in ("", "none"):
        return NoSolver(cfg, proxy=proxy)
    if provider == "manual":
        return ManualSolver(cfg, proxy=proxy)
    if provider == "capsolver":
        return CapSolver(cfg, proxy=proxy)
    if provider == "2captcha":
        return TwoCaptcha(cfg, proxy=proxy)
    if provider == "browser":
        return BrowserSolver(cfg, proxy=proxy, browser=browser)
    if provider == "auto":
        # auto：有 key 就上打码平台，没 key 且允许浏览器就浏览器过，否则不发 token。
        if cfg.api_key:
            return CapSolver(cfg, proxy=proxy)
        if browser is not None:
            return BrowserSolver(cfg, proxy=proxy, browser=browser)
        return NoSolver(cfg, proxy=proxy)
    raise CaptchaRequired(f"未知 captcha provider：{provider}")
