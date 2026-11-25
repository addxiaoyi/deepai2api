"""Optional captcha solving for the browser engine (Turnstile / reCAPTCHA v2-v3).

Kept provider-agnostic: point `capsolver` at your key, or supply your own token
source. The browser engine only calls this when a challenge widget is actually
on the page and did not clear itself.
"""

from __future__ import annotations

import time
from typing import Any

from .net import build_client, request

CAPSOLVER = "https://api.capsolver.com"


class CaptchaError(RuntimeError):
    pass


def solve_turnstile(
    *,
    api_key: str,
    site_url: str,
    site_key: str,
    proxy: str = "",
    timeout: float = 120.0,
    poll: float = 3.0,
    client: Any = None,
) -> str:
    """AntiTurnstileTaskProxyLess (or proxied) -> token string."""
    if not api_key:
        raise CaptchaError("no captcha api key configured")
    http = client or build_client(proxy=proxy, timeout=40)
    task: dict[str, Any] = {
        "type": "AntiTurnstileTaskProxyLess",
        "websiteURL": site_url,
        "websiteKey": site_key,
    }
    if proxy:
        # proxied variant keeps the solve on the same egress as the browser
        task["type"] = "AntiTurnstileTask"
        task["proxy"] = proxy
    created = request(
        http, "POST", f"{CAPSOLVER}/createTask", json={"clientKey": api_key, "task": task}
    ).json()
    if created.get("errorId"):
        raise CaptchaError(f"createTask failed: {created.get('errorDescription')}")
    task_id = created.get("taskId")
    if not task_id:
        raise CaptchaError(f"createTask returned no taskId: {created}")
    deadline = time.time() + timeout
    while time.time() < deadline:
        time.sleep(poll)
        result = request(
            http, "POST", f"{CAPSOLVER}/getTaskResult", json={"clientKey": api_key, "taskId": task_id}
        ).json()
        if result.get("errorId"):
            raise CaptchaError(f"getTaskResult failed: {result.get('errorDescription')}")
        if result.get("status") == "ready":
            token = (result.get("solution") or {}).get("token", "")
            if not token:
                raise CaptchaError("solution carried no token")
            return token
    raise CaptchaError("captcha solve timed out")


INJECT_JS = """
(args) => {
  const { token, field } = args;
  const inputs = Array.from(document.querySelectorAll('input[name="' + field + '"]'));
  for (const input of inputs) {
    input.value = token;
    input.dispatchEvent(new Event('input', {bubbles: true}));
    input.dispatchEvent(new Event('change', {bubbles: true}));
  }
  if (window.turnstile && typeof window.turnstile.getResponse === 'function') {
    try { window.__cfTurnstileCallback && window.__cfTurnstileCallback(token); } catch (e) {}
  }
  return inputs.length;
}
"""
