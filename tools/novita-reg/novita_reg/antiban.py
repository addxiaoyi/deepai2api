"""防封策略 —— 独立成文件，因为它是"能跑多久"的决定因素。

Novita 有完整的邮箱注册表单，可聚类的信号比 WaveSpeed 更多。本模块逐条给出对策：

  1. **出口 IP**       → `IpGate`：同一出口 IP 同时只跑 1 个；换 IP 才换身份。
  2. **邮箱域名**       → `DomainRotator`：轮换 + 单域名速率窗 + 健康分降权。
  3. **请求节奏**       → `jitter()`：全局抖动，避免固定间隔的机器节奏。
  4. **浏览器指纹**     → `fingerprint_for()`：每账号一套自洽指纹，与出口地理对齐。
  5. **会话复用**       → `SessionCache`：OAuth 会话尽量复用，减少重复登录。
  6. **失败语义**       → `backoff_for()`：按失败 kind 决定退避多久、要不要换腿。

设计原则：**宁可慢，也不要被同一批信号聚成一簇**。所有阈值都可配。
"""

from __future__ import annotations

import random
import threading
import time
from dataclasses import dataclass, field
from typing import Any

from .config import AntiBanConfig
from .identity import Persona, geo_for_proxy


# ---------------------------------------------------------------- 出口 IP 闸门


class IpGate:
    """同一出口 IP 的并发闸门。

    并发注册时最容易犯的错是"5 个线程共用 1 个代理"：平台看到的是同一 IP 上
    5 个新账号同时出现，这比任何指纹问题都致命。这里按出口串行化。
    """

    def __init__(self, limit: int = 1) -> None:
        self.limit = max(1, int(limit))
        self._locks: dict[str, threading.Semaphore] = {}
        self._guard = threading.Lock()

    def _sem(self, ip: str) -> threading.Semaphore:
        with self._guard:
            sem = self._locks.get(ip)
            if sem is None:
                sem = threading.Semaphore(self.limit)
                self._locks[ip] = sem
            return sem

    def acquire(self, proxy: str, timeout: float = 300.0) -> bool:
        return self._sem(proxy or "direct").acquire(timeout=timeout)

    def release(self, proxy: str) -> None:
        try:
            self._sem(proxy or "direct").release()
        except ValueError:
            pass

    class _Ctx:
        def __init__(self, gate: "IpGate", proxy: str, timeout: float) -> None:
            self.gate = gate
            self.proxy = proxy
            self.timeout = timeout
            self.ok = False

        def __enter__(self) -> bool:
            self.ok = self.gate.acquire(self.proxy, self.timeout)
            return self.ok

        def __exit__(self, *exc: object) -> None:
            if self.ok:
                self.gate.release(self.proxy)

    def slot(self, proxy: str, timeout: float = 300.0) -> "_Ctx":
        return IpGate._Ctx(self, proxy, timeout)


# ------------------------------------------------------------- 域名邮箱轮换器


@dataclass
class DomainState:
    domain: str
    sent: int = 0
    ok: int = 0
    failed: int = 0
    minute_window: list[float] = field(default_factory=list)
    hour_window: list[float] = field(default_factory=list)

    @property
    def health(self) -> float:
        total = self.ok + self.failed
        if total == 0:
            return 1.0
        return self.ok / total


class DomainRotator:
    """域名轮换 + 速率窗 + 健康分。

    两个窗（分钟 / 小时）各自滑出；健康分低于 floor 的域名暂时不进候选，
    但保留在表里 —— 它可能只是被临时限流，过一段时间会恢复。
    """

    def __init__(self, domains: list[str], anti: AntiBanConfig, rng: random.Random | None = None) -> None:
        self.rng = rng or random.Random()
        self.anti = anti
        # pick() 在持锁期间会调 available()，必须是可重入锁，否则自死锁。
        self._guard = threading.RLock()
        self.states: dict[str, DomainState] = {}
        for domain in domains:
            clean = (domain or "").strip().lower()
            if clean:
                self.states.setdefault(clean, DomainState(domain=clean))
        self._cursor = 0

    def _prune(self, state: DomainState, now: float) -> None:
        state.minute_window = [t for t in state.minute_window if now - t < 60]
        state.hour_window = [t for t in state.hour_window if now - t < 3600]

    def available(self) -> list[str]:
        now = time.time()
        out: list[str] = []
        with self._guard:
            for state in self.states.values():
                self._prune(state, now)
                if state.health < self.anti.domain_health_floor:
                    continue
                if len(state.minute_window) >= self.anti.domain_rpm:
                    continue
                if len(state.hour_window) >= self.anti.domain_rph:
                    continue
                out.append(state.domain)
        return out

    def pick(self, exclude: tuple[str, ...] = ()) -> str:
        """挑一个当前可用的域名；轮询 + 少量随机，避免永远同一个。"""
        with self._guard:
            candidates = [d for d in self.available() if d not in exclude]
            if not candidates:
                return ""
            self._cursor = (self._cursor + 1) % max(1, len(candidates))
            start = self._cursor
            ordered = candidates[start:] + candidates[:start]
            # 前两个里随机取，打散"域名 A 一定第一个"的模式
            head = ordered[:2]
            return self.rng.choice(head) if head else ""

    def note_send(self, domain: str) -> None:
        now = time.time()
        with self._guard:
            state = self.states.get((domain or "").lower())
            if not state:
                return
            self._prune(state, now)
            state.sent += 1
            state.minute_window.append(now)
            state.hour_window.append(now)

    def note_result(self, domain: str, ok: bool) -> None:
        with self._guard:
            state = self.states.get((domain or "").lower())
            if not state:
                return
            if ok:
                state.ok += 1
            else:
                state.failed += 1

    def snapshot(self) -> list[dict[str, Any]]:
        now = time.time()
        rows: list[dict[str, Any]] = []
        with self._guard:
            for state in self.states.values():
                self._prune(state, now)
                rows.append(
                    {
                        "domain": state.domain,
                        "sent": state.sent,
                        "ok": state.ok,
                        "failed": state.failed,
                        "health": round(state.health, 3),
                        "minute": len(state.minute_window),
                        "hour": len(state.hour_window),
                    }
                )
        return sorted(rows, key=lambda r: (-r["health"], r["sent"]))


# ------------------------------------------------------------------ 会话缓存


@dataclass
class SessionEntry:
    """一份可复用的浏览器登录态。"""

    proxy: str
    cookies: list[dict[str, str]]
    saved_at: float
    engine: str = "browser"
    note: str = ""

    def fresh(self, ttl_hours: int) -> bool:
        return (time.time() - self.saved_at) < ttl_hours * 3600


class SessionCache:
    """按出口 IP 缓存 cookie jar。

    Novita 的 voucher 风控会看"同设备多账号"：复用一份已激活的会话（而不是
    每次都从头登录）能显著降低"同一 IP 反复出现新账号"的观感。
    """

    def __init__(self, ttl_hours: int = 72) -> None:
        self.ttl_hours = max(1, int(ttl_hours))
        self._guard = threading.Lock()
        self._by_key: dict[str, SessionEntry] = {}

    @staticmethod
    def key_for(proxy: str, email: str = "") -> str:
        return f"{proxy or 'direct'}|{email or '*'}"

    def get(self, proxy: str, email: str = "") -> SessionEntry | None:
        with self._guard:
            entry = self._by_key.get(self.key_for(proxy, email))
            if entry and entry.fresh(self.ttl_hours):
                return entry
            return None

    def put(self, proxy: str, cookies: list[dict[str, str]], *, email: str = "", engine: str = "browser") -> None:
        with self._guard:
            self._by_key[self.key_for(proxy, email)] = SessionEntry(
                proxy=proxy,
                cookies=list(cookies or []),
                saved_at=time.time(),
                engine=engine,
            )

    def drop(self, proxy: str, email: str = "") -> None:
        with self._guard:
            self._by_key.pop(self.key_for(proxy, email), None)

    def stats(self) -> dict[str, Any]:
        with self._guard:
            live = [e for e in self._by_key.values() if e.fresh(self.ttl_hours)]
            return {"entries": len(self._by_key), "fresh": len(live), "ttl_hours": self.ttl_hours}


# ------------------------------------------------------------------- 指纹生成


def fingerprint_for(persona: Persona, proxy: str, *, geo_align: bool = True) -> dict[str, Any]:
    """给一个 persona 生成一套**自洽**的浏览器指纹。

    自洽的意思是：时区、语言、UA 平台、屏幕、硬件并发数互相不矛盾。
    矛盾的组合（比如 Linux UA + Windows 字体 + 东京时区 + 美国 IP）是最容易被
    指纹库直接判定为自动化的特征。
    """
    rng = random.Random(persona.profile_seed or 1)
    if geo_align:
        tz, locale, accept = geo_for_proxy(proxy)
    else:
        tz, locale, accept = persona.timezone, persona.locale, persona.accept_language

    platform = rng.choice(["Windows", "Windows", "macOS"])  # 桌面端占多数
    if platform == "Windows":
        ua = (
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
            f"(KHTML, like Gecko) Chrome/{rng.choice([131, 132, 133, 134, 135])}.0.0.0 Safari/537.36"
        )
        screens = [(1920, 1080), (1536, 864), (1366, 768), (2560, 1440)]
        cores = [8, 12, 16]
        gpu_vendor = "Google Inc. (NVIDIA)"
        gpu_renderer = rng.choice(
            [
                "ANGLE (NVIDIA, NVIDIA GeForce RTX 3060 Direct3D11 vs_5_0 ps_5_0, D3D11)",
                "ANGLE (NVIDIA, NVIDIA GeForce GTX 1660 SUPER Direct3D11 vs_5_0 ps_5_0, D3D11)",
                "ANGLE (Intel, Intel(R) UHD Graphics 630 Direct3D11 vs_5_0 ps_5_0, D3D11)",
            ]
        )
    else:
        ua = (
            "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
            f"(KHTML, like Gecko) Chrome/{rng.choice([131, 132, 133, 134, 135])}.0.0.0 Safari/537.36"
        )
        screens = [(1728, 1117), (1512, 982), (1440, 900)]
        cores = [8, 10]
        gpu_vendor = "Google Inc. (Apple)"
        gpu_renderer = "ANGLE (Apple, ANGLE Metal Renderer: Apple M1, Unspecified Version)"

    width, height = rng.choice(screens)
    return {
        "user_agent": ua,
        "platform": platform,
        "locale": locale,
        "accept_language": accept,
        "timezone": tz,
        "viewport": {"width": width, "height": height - rng.randint(60, 120)},
        "screen": {"width": width, "height": height},
        "device_scale_factor": rng.choice([1, 1, 2]) if platform == "macOS" else rng.choice([1, 1, 1.25]),
        "hardware_concurrency": rng.choice(cores),
        "device_memory": rng.choice([8, 8, 16]),
        "webgl_vendor": gpu_vendor,
        "webgl_renderer": gpu_renderer,
        "color_depth": 24,
        "seed": persona.profile_seed,
    }


# ------------------------------------------------------------------- 节奏控制


def jitter(anti: AntiBanConfig, rng: random.Random | None = None) -> float:
    """睡一段随机时间，返回实际睡了多久（秒）。

    固定间隔是最廉价的机器人特征之一，所以这里只保证"区间"，不保证"均匀"。
    """
    rng = rng or random.Random()
    low, high = anti.jitter_ms[0], anti.jitter_ms[1]
    if high <= 0:
        return 0.0
    seconds = rng.uniform(max(0, low), max(low, high)) / 1000.0
    time.sleep(seconds)
    return seconds


def backoff_for(anti: AntiBanConfig, kind: str) -> float:
    """按失败类型退避；返回实际等待秒数。"""
    low, high = anti.backoff_for(kind)
    if high <= 0:
        return 0.0
    seconds = random.uniform(low, max(low, high))
    if seconds > 0:
        time.sleep(seconds)
    return seconds


def should_switch_engine(kind: str) -> bool:
    """这个失败是不是"该换引擎"的信号。

    协议腿遇到 Cloudflare / Turnstile / 拿不到 key，继续在协议上重试只是浪费；
    OAuth 本身走不通则应该换账号而不是换引擎。
    """
    return kind in ("blocked", "captcha_required", "key_not_obtainable", "no_api_key")


def should_rotate_identity(kind: str) -> bool:
    """这个失败是不是"该换邮箱/域名/代理"的信号。"""
    return kind in (
        "email_taken",
        "email_rejected",
        "rate_limited",
        "blocked",
        "captcha_required",
        "oauth_unavailable",
    )




# ------------------------------------------------------- Novita 专属：多账号隔离


def profile_isolation_plan(
    email: str,
    *,
    root: str = "",
    isolate: bool = True,
    clear_storage: bool = True,
    seed: int = 0,
) -> dict[str, Any]:
    """规划一个账号的隔离方案。

    Novita 官方 FAQ 明确：检测到"同设备多账号"会自动撤销 voucher。所以在同一台
    机器上批量跑时，每个账号必须看起来像一台独立设备：

      * 独立 profile 目录（cookie / cache / IndexedDB 各自一份）
      * 独立 canvas / WebGL 噪声种子（由 persona.profile_seed 派生）
      * 进入页面前清 localStorage / sessionStorage
    """
    if not isolate or not root:
        return {
            "profile_dir": root,
            "isolated": False,
            "clear_storage": clear_storage,
            "noise_seed": seed,
        }
    tag = "".join(ch if ch.isalnum() or ch in "._-" else "_" for ch in (email or "anon"))[:48]
    return {
        "profile_dir": f"{root.rstrip('/')}/{tag}",
        "isolated": True,
        "clear_storage": clear_storage,
        "noise_seed": seed or (hash(email) & 0x7FFFFFFF),
    }


def key_policy_payload(
    *,
    open_model_access: bool = False,
    model_ids: list[str] | None = None,
) -> dict[str, Any]:
    """建完 key 后要 PUT 的 model-access 策略体。

    Novita 默认会挡掉一部分模型（`model_access_denied`），把 model-access 打开
    就能让这把 key 用全量模型。只在 `open_model_access=true` 时发送。
    """
    if not open_model_access:
        return {}
    payload: dict[str, Any] = {"modelAccess": "all"}
    if model_ids:
        payload["modelAccess"] = list(model_ids)
    return payload

