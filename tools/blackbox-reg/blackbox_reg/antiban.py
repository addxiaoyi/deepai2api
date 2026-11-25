"""防封策略实现。

Blackbox 的风控面比前几个站点更宽：Google 那半程认"人"，app 那半程认"会话"，
面板建 Key 认"频次"。所以这里是五层护栏，字段与网关 `DEFAULT_ANTI_BAN` 对齐：

1. **身份层**：一个 Google 身份只出一个账号，身份与出口 IP 绑定
   （`proxy_sticky_per_identity`），避免"同一账号一小时横跨三个国家"。
2. **域名层**：滑动窗口限速（RPM/RPH）+ 健康分（成功 +1 / 失败 -2），
   低于 `domain_health_floor` 的域自动停用。
3. **出口层**：同一代理串行（`ip_concurrency`），失败计数决定轮换顺序。
4. **节奏层**：注册之间抖动、OAuth 页停留、批次后冷却。
5. **接口层**：面板建 Key 单账号 RPM（`key_create_rpm`），401/403 立即冷却该账号。
"""

from __future__ import annotations

import random
import threading
import time
from collections import deque
from dataclasses import dataclass, field

from .config import AntiBanConfig


class SlidingWindow:
    """最近 `window` 秒内的计数。"""

    def __init__(self, window: float):
        self.window = window
        self._hits: deque[float] = deque()
        self._lock = threading.Lock()

    def count(self, now: float | None = None) -> int:
        now = now or time.time()
        with self._lock:
            while self._hits and now - self._hits[0] > self.window:
                self._hits.popleft()
            return len(self._hits)

    def add(self, now: float | None = None) -> None:
        with self._lock:
            self._hits.append(now or time.time())

    def wait_seconds(self, limit: int) -> float:
        if limit <= 0:
            return 0.0
        now = time.time()
        with self._lock:
            while self._hits and now - self._hits[0] > self.window:
                self._hits.popleft()
            if len(self._hits) < limit:
                return 0.0
            oldest = self._hits[0]
        return max(0.0, self.window - (now - oldest))


@dataclass
class DomainState:
    domain: str
    ok: int = 0
    bad: int = 0
    minute: SlidingWindow = field(default_factory=lambda: SlidingWindow(60))
    hour: SlidingWindow = field(default_factory=lambda: SlidingWindow(3600))

    @property
    def health(self) -> float:
        total = self.ok + self.bad
        return 1.0 if total == 0 else max(0.0, self.ok / total)


class AntiBan:
    """线程安全的防封决策器；runner 的每个工作线程都从这里要"能不能开始"。"""

    def __init__(self, config: AntiBanConfig, *, proxies: list[str] | None = None, log=None):
        self.config = config
        self.log = log or (lambda *a, **k: None)
        self.domains: dict[str, DomainState] = {}
        self._domain_lock = threading.Lock()
        self._proxy_locks: dict[str, threading.Semaphore] = {}
        self._proxies = list(proxies or [])
        self._proxy_failures: dict[str, int] = {}
        self._identity_bind: dict[str, str] = {}
        self._lock = threading.Lock()
        self.started_at = time.time()

    # ------------------------------------------------------------------ 身份
    def proxy_for_identity(self, identity: str, index: int = 0) -> str:
        """同一身份永远拿同一个出口（Google 最忌中途换 IP）。"""
        if not self._proxies:
            return ""
        if not self.config.proxy_sticky_per_identity:
            return self.proxy_for(index)
        with self._lock:
            bound = self._identity_bind.get(identity)
            if bound is not None:
                return bound
            # 新身份：优先挑失败次数最少的出口，避免都挤在坏代理上
            ranked = sorted(self._proxies, key=lambda p: (self._proxy_failures.get(p, 0), self._proxies.index(p)))
            chosen = ranked[0] if ranked else ""
            self._identity_bind[identity] = chosen
            return chosen

    def release_identity(self, identity: str) -> None:
        with self._lock:
            self._identity_bind.pop(identity, None)

    # ------------------------------------------------------------------ 域名
    def domain_state(self, domain: str) -> DomainState:
        with self._domain_lock:
            if domain not in self.domains:
                self.domains[domain] = DomainState(domain=domain)
            return self.domains[domain]

    def pick_domain(self, candidates: list[str]) -> str:
        if not candidates:
            raise ValueError("没有可用的邮箱域名。")
        eligible = [d for d in candidates if self.domain_state(d).health >= self.config.domain_health_floor] or candidates
        weights = []
        for domain in eligible:
            state = self.domain_state(domain)
            idle = 1.0 + (self.config.domain_rpm - state.minute.count()) / max(1, self.config.domain_rpm)
            weights.append(max(0.05, state.health) * idle)
        return random.choices(eligible, weights=weights, k=1)[0]

    def reserve_domain(self, domain: str) -> float:
        """占用一个域名配额，返回需要等待的秒数（0 = 立刻可用）。"""
        state = self.domain_state(domain)
        wait = max(
            state.minute.wait_seconds(self.config.domain_rpm),
            state.hour.wait_seconds(self.config.domain_rph),
            0.0,
        )
        if wait > 0:
            return min(wait, 90.0)
        state.minute.add()
        state.hour.add()
        return 0.0

    def record_domain(self, domain: str, *, ok: bool) -> None:
        if not domain:
            return
        state = self.domain_state(domain)
        if ok:
            state.ok += 1
        else:
            state.bad += 2

    def domain_report(self) -> list[dict]:
        return [
            {"domain": d.domain, "ok": d.ok, "bad": d.bad, "health": round(d.health, 3)}
            for d in sorted(self.domains.values(), key=lambda x: (-x.ok, x.domain))
        ]

    # ------------------------------------------------------------------ 出口
    def proxy_for(self, index: int) -> str:
        if not self._proxies:
            return ""
        if self.config.proxy_rotate:
            return self._proxies[index % len(self._proxies)]
        return self._proxies[0]

    def proxy_lock(self, proxy: str) -> threading.Semaphore:
        key = proxy or "direct"
        with self._lock:
            if key not in self._proxy_locks:
                self._proxy_locks[key] = threading.Semaphore(max(1, self.config.ip_concurrency))
            return self._proxy_locks[key]

    def record_proxy(self, proxy: str, *, ok: bool) -> None:
        if not proxy:
            return
        with self._lock:
            self._proxy_failures[proxy] = 0 if ok else self._proxy_failures.get(proxy, 0) + 1

    # ------------------------------------------------------------------ 节奏
    def jitter(self) -> None:
        low, high = (self.config.jitter_ms + [1200, 4200])[:2]
        time.sleep(random.uniform(min(low, high), max(low, high)) / 1000.0)

    def oauth_pause(self) -> None:
        low, high = (self.config.oauth_pause_ms + [800, 2400])[:2]
        time.sleep(random.uniform(min(low, high), max(low, high)) / 1000.0)

    def backoff_seconds(self, kind: str) -> float:
        band = (self.config.backoff or {}).get(kind) or [0, 0]
        low, high = (list(band) + [0, 0])[:2]
        if not high:
            return 0.0
        return random.uniform(min(low, high), max(low, high))

    def cool_down_batch(self) -> None:
        if self.config.cooldown_after_batch > 0:
            time.sleep(self.config.cooldown_after_batch)
