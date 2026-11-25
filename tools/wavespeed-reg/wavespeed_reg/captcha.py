"""人机校验：Turnstile / hCaptcha token。

WaveSpeed 的 sitekey 不在文档里，只能在运行时从页面挖；挖不到就交回浏览器腿。
解题优先级（便宜 → 贵）：

  none      : 直接不带 token 试一次，让上游告诉我们"要不要"
  auto      : 先 none，被拦再按 api_key 是否存在选 capsolver / 2captcha / browser
  capsolver : AntiTurnstileTask(Proxyless)，尊重页面 action
  2captcha  : TurnstileTaskProxyless
  browser   : 在真实页面里渲染 widget，读 callback token
  manual    : 别人产出的 token（config / env 里给）
"""

from __future__ import annotations

import time
from typing import Any

from .errors import CaptchaRequired
from .net import build_client, request

CAPSOLVER = "https://api.capsolver.com"
TWOCAPTCHA = "https://2captcha.com"


class CaptchaError(CaptchaRequired):
    pass


def solve_turnstile(
    *,
    provider: str,
    api_key: str,
    site_url: str,
    site_key: str,
    action: str = "login",
    proxy: str = "",
    timeout: float = 120.0,
    poll: float = 3.0,
    client: Any = None,
) -> str:
    """给 `site_url` / `site_key` 拿一个 Turnstile token。

    `auto` 的语义：没有 api_key 就返回空串（交给调用方按"无 token 先试"处理），
    有 api_key 就按 capsolver 处理 —— 真正的"先试再解题"由 runner 决定。
    """
    name = (provider or "none").strip().lower()
    if name in ("", "none"):
        return ""
    if name == "auto":
        return _capsolver(
            api_key=api_key,
            site_url=site_url,
            site_key=site_key,
            action=action,
            proxy=proxy,
            timeout=timeout,
            poll=poll,
            client=client,
        ) if api_key else ""
    if name == "manual":
        if not api_key:
            raise CaptchaError("manual captcha provider needs a token in `captcha.api_key`")
        return api_key
    if not site_key:
        raise CaptchaError("captcha provider needs a site_key (从 /sign-in 页面挖不到)")
    if name == "capsolver":
        return _capsolver(
            api_key=api_key,
            site_url=site_url,
            site_key=site_key,
            action=action,
            proxy=proxy,
            timeout=timeout,
            poll=poll,
            client=client,
        )
    if name == "2captcha":
        return _twocaptcha(
            api_key=api_key,
            site_url=site_url,
            site_key=site_key,
            action=action,
            proxy=proxy,
            timeout=timeout,
            poll=poll,
            client=client,
        )
    if name == "browser":
        return mint_with_browser(site_url=site_url, site_key=site_key, action=action, proxy=proxy, timeout=timeout)
    raise CaptchaError(f"unknown captcha provider: {provider}")


def _capsolver(
    *,
    api_key: str,
    site_url: str,
    site_key: str,
    action: str,
    proxy: str,
    timeout: float,
    poll: float,
    client: Any,
) -> str:
    if not api_key:
        raise CaptchaError("capsolver needs an api key")
    http = client or build_client(proxy=proxy, timeout=40)
    task: dict[str, Any] = {
        "type": "AntiTurnstileTaskProxyLess",
        "websiteURL": site_url,
        "websiteKey": site_key,
        "pageAction": action,
    }
    if proxy:
        # 与注册请求同一出口，Cloudflare 的 IP 评分才一致。
        task["type"] = "AntiTurnstileTask"
        task["proxy"] = proxy
    created = request(http, "POST", f"{CAPSOLVER}/createTask", json={"clientKey": api_key, "task": task}).json()
    if created.get("errorId"):
        raise CaptchaError(f"capsolver createTask failed: {created.get('errorDescription')}")
    task_id = created.get("taskId")
    if not task_id:
        raise CaptchaError(f"capsolver createTask returned no taskId: {created}")
    deadline = time.time() + timeout
    while time.time() < deadline:
        time.sleep(poll)
        result = request(
            http, "POST", f"{CAPSOLVER}/getTaskResult", json={"clientKey": api_key, "taskId": task_id}
        ).json()
        if result.get("errorId"):
            raise CaptchaError(f"capsolver getTaskResult failed: {result.get('errorDescription')}")
        if result.get("status") == "ready":
            token = (result.get("solution") or {}).get("token", "")
            if not token:
                raise CaptchaError("capsolver solution carried no token")
            return token
    raise CaptchaError("capsolver solve timed out")


def _twocaptcha(
    *,
    api_key: str,
    site_url: str,
    site_key: str,
    action: str,
    proxy: str,
    timeout: float,
    poll: float,
    client: Any,
) -> str:
    if not api_key:
        raise CaptchaError("2captcha needs an api key")
    http = client or build_client(proxy=proxy, timeout=40)
    params: dict[str, Any] = {
        "key": api_key,
        "method": "turnstile",
        "sitekey": site_key,
        "pageurl": site_url,
        "action": action,
        "json": 1,
    }
    if proxy:
        host, _, auth = proxy.split("://", 2)[-1].partition("@")
        if auth:
            user, _, secret = auth.partition(":")
            params.update({"proxy": host, "proxytype": "HTTP", "proxyuser": user, "proxypass": secret})
        else:
            params.update({"proxy": host, "proxytype": "HTTP"})
    created = request(http, "POST", f"{TWOCAPTCHA}/in.php", data=params).json()
    if created.get("status") != 1:
        raise CaptchaError(f"2captcha in.php failed: {created.get('request')}")
    task_id = created.get("request")
    deadline = time.time() + timeout
    while time.time() < deadline:
        time.sleep(poll)
        result = request(
            http,
            "GET",
            f"{TWOCAPTCHA}/res.php",
            params={"key": api_key, "action": "get", "id": task_id, "json": 1},
        ).json()
        if result.get("status") == 1 and isinstance(result.get("request"), str):
            token = result["request"]
            if token and not token.startswith("CAPCHA_NOT_READY"):
                return token
        elif result.get("request") not in ("CAPCHA_NOT_READY", None):
            raise CaptchaError(f"2captcha res.php failed: {result.get('request')}")
    raise CaptchaError("2captcha solve timed out")


_MINT_JS = """
async (args) => {
  const { siteKey, action } = args;
  if (!window.turnstile) {
    await new Promise((resolve, reject) => {
      const s = document.createElement('script');
      s.src = 'https://challenges.cloudflare.com/turnstile/v0/api.js?render=explicit';
      s.async = true;
      s.onload = resolve;
      s.onerror = () => reject(new Error('turnstile script failed'));
      document.head.appendChild(s);
    });
  }
  const host = document.createElement('div');
  host.id = '__ws_captcha';
  host.style.position = 'fixed';
  host.style.inset = '0';
  host.style.zIndex = '2147483647';
  document.body.appendChild(host);
  return await new Promise((resolve, reject) => {
    const timer = setTimeout(() => reject(new Error('turnstile mint timed out')), 120000);
    try {
      window.turnstile.render(host, {
        sitekey: siteKey,
        action,
        theme: 'light',
        callback: (token) => { clearTimeout(timer); resolve(token); },
        'error-callback': () => { clearTimeout(timer); reject(new Error('turnstile error callback')); },
      });
    } catch (error) {
      clearTimeout(timer);
      reject(error);
    }
  });
}
"""


def mint_with_browser(
    *,
    site_url: str,
    site_key: str,
    action: str = "login",
    proxy: str = "",
    timeout: float = 180.0,
    headless: bool = True,
) -> str:
    """在真实 origin 上渲染 widget，读 callback token。

    Playwright 是可选依赖：协议腿永远不需要它，但干净的住宅 IP + 真浏览器
    通常能直接过，不必付解题费。
    """
    try:
        from playwright.sync_api import sync_playwright
    except Exception as exc:  # noqa: BLE001 - 可选依赖
        raise CaptchaError(
            "browser captcha provider needs playwright (pip install playwright && playwright install chromium)"
        ) from exc

    launch: dict[str, Any] = {"headless": headless}
    if proxy:
        launch["proxy"] = {"server": proxy}
    with sync_playwright() as pw:
        browser = pw.chromium.launch(**launch)
        try:
            context = browser.new_context(locale="en-US")
            page = context.new_page()
            page.goto(site_url, wait_until="domcontentloaded", timeout=int(timeout * 1000))
            token = page.evaluate(_MINT_JS, {"siteKey": site_key, "action": action})
            if not token:
                raise CaptchaError("browser produced an empty turnstile token")
            return str(token)
        finally:
            browser.close()
