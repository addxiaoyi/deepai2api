"""身份与指纹。

Zo 是 Next.js 站点 + Cloudflare Turnstile（只在 confirm 那一步、且仅浏览器腿会渲染），
所以 persona 的作用是两件事：

1. **降低批量特征**：不同 UA / 语言 / 时区 / 屏幕，别让同一批号看起来像同一个人。
2. **geo 对齐**：`--proxy` 给的是哪个国家，UA 语言与时区就跟过去（HEADLESS 检查里
   时区与 IP 不一致是最典型的自动化特征）。

桌面端分支（`desktop_ua=True`）额外把 UA 加上 `Zo/<version>`：
面板用 `/Zo\\//.test(navigator.userAgent)` 判断"这是桌面端"，从而走
`zo://email-login?token=…` 深链而不是网页 confirm。
"""

from __future__ import annotations

import hashlib
import random
import string
from dataclasses import dataclass, field, asdict

FIRST_NAMES = [
    "Aiden", "Maya", "Noah", "Lena", "Elliot", "Nora", "Kai", "Iris", "Milo", "Zoe",
    "Hugo", "Clara", "Felix", "Ava", "Owen", "Ruby", "Theo", "Ivy", "Lucas", "Elena",
    "Mark", "Sofia", "Daniel", "Anna", "Pavel", "Olga", "Ryan", "Chloe",
]

LAST_NAMES = [
    "Bennett", "Whitaker", "Ferreira", "Nakamura", "Okafor", "Lindqvist", "Moreau",
    "Castillo", "Hoffmann", "Novak", "Andersen", "Silva", "Kowalski", "Dubois",
    "Petrov", "Ivanova", "Sokolov", "Kuznetsova", "Reed", "Hayes", "Brooks",
]

CHROME_VERSIONS = ["139", "140", "141", "142"]
DESKTOP_UA_VERSIONS = ["1.6.0", "1.7.2", "1.8.0"]

# 代理出口国家 → (时区, 语言, Accept-Language)
GEO_PROFILES = {
    "US": ("America/New_York", "en-US", "en-US,en;q=0.9"),
    "CA": ("America/Toronto", "en-US", "en-US,en;q=0.9,fr;q=0.7"),
    "GB": ("Europe/London", "en-GB", "en-GB,en;q=0.9"),
    "DE": ("Europe/Berlin", "de-DE", "de-DE,de;q=0.9,en-US;q=0.8,en;q=0.7"),
    "NL": ("Europe/Amsterdam", "en-US", "en-US,en;q=0.9,nl;q=0.7"),
    "FR": ("Europe/Paris", "fr-FR", "fr-FR,fr;q=0.9,en-US;q=0.8,en;q=0.7"),
    "SG": ("Asia/Singapore", "en-US", "en-US,en;q=0.9"),
    "JP": ("Asia/Tokyo", "ja-JP", "ja-JP,ja;q=0.9,en-US;q=0.8,en;q=0.7"),
    "TR": ("Europe/Istanbul", "tr-TR", "tr-TR,tr;q=0.9,en;q=0.7"),
    "KZ": ("Asia/Almaty", "ru-RU", "ru-RU,ru;q=0.9,en-US;q=0.8,en;q=0.7"),
    "": ("America/New_York", "en-US", "en-US,en;q=0.9"),
}


@dataclass
class Persona:
    first_name: str
    last_name: str
    username: str
    handle: str
    email: str = ""
    # 指纹
    chrome_major: str = "140"
    platform: str = "Win32"
    timezone: str = "America/New_York"
    locale: str = "en-US"
    accept_language: str = "en-US,en;q=0.9"
    viewport: tuple[int, int] = (1440, 900)
    screen: tuple[int, int] = (1920, 1080)
    device_scale: float = 1.0
    # 桌面端壳（可选）
    desktop: bool = False
    desktop_version: str = ""
    # 元信息
    seed: str = ""
    note: str = ""

    # ------------------------------------------------------------- 派生属性
    @property
    def base_user_agent(self) -> str:
        return (
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
            f"(KHTML, like Gecko) Chrome/{self.chrome_major}.0.0.0 Safari/537.36"
        )

    @property
    def user_agent(self) -> str:
        """桌面端分支：Electron 壳的 UA 会带 `Zo/<version>`（面板据此判定桌面端）。"""
        if not self.desktop:
            return self.base_user_agent
        version = self.desktop_version or "1.7.2"
        return f"{self.base_user_agent} Zo/{version}"

    @property
    def sec_ch_ua(self) -> str:
        v = self.chrome_major
        return f'"Chromium";v="{v}", "Google Chrome";v="{v}", "Not?A_Brand";v="99"'

    def headers(self, *, referer: str = "", origin: str = "") -> dict[str, str]:
        out = {
            "User-Agent": self.user_agent,
            "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,image/webp,*/*;q=0.8",
            "Accept-Language": self.accept_language,
            "Upgrade-Insecure-Requests": "1",
            "Sec-CH-UA": self.sec_ch_ua,
            "Sec-CH-UA-Mobile": "?0",
            "Sec-CH-UA-Platform": f'"{self.platform}"',
            "Sec-Fetch-Dest": "document",
            "Sec-Fetch-Mode": "navigate",
            "Sec-Fetch-Site": "same-origin",
            "Sec-Fetch-User": "?1",
        }
        if referer:
            out["Referer"] = referer
        if origin:
            out["Origin"] = origin
        return out

    def xhr_headers(self, *, origin: str, referer: str) -> dict[str, str]:
        return {
            "User-Agent": self.user_agent,
            "Accept": "*/*",
            "Accept-Language": self.accept_language,
            "Content-Type": "application/json",
            "Origin": origin,
            "Referer": referer,
            "Sec-CH-UA": self.sec_ch_ua,
            "Sec-CH-UA-Mobile": "?0",
            "Sec-CH-UA-Platform": f'"{self.platform}"',
            "Sec-Fetch-Dest": "empty",
            "Sec-Fetch-Mode": "cors",
            "Sec-Fetch-Site": "same-origin",
        }

    def fingerprint(self) -> dict:
        return {
            "userAgent": self.user_agent,
            "platform": self.platform,
            "locale": self.locale,
            "timezoneId": self.timezone,
            "viewport": {"width": self.viewport[0], "height": self.viewport[1]},
            "screen": {"width": self.screen[0], "height": self.screen[1]},
            "deviceScaleFactor": self.device_scale,
            "hardwareConcurrency": random.choice([4, 6, 8, 12, 16]),
            "colorScheme": random.choice(["light", "light", "dark"]),
        }

    def to_dict(self) -> dict:
        return asdict(self)


def geo_profile(country: str | None) -> tuple[str, str, str]:
    return GEO_PROFILES.get((country or "").upper(), GEO_PROFILES[""])


def handle_for(persona: Persona, seed: str, *, prefix: str = "") -> str:
    """工作区 handle：小写字母数字与短横，3-30 位；先探占用再落。

    用 `名+姓+数字`，可复现（同 seed 同 handle），方便重跑时撞上同一个号。
    """
    rng = random.Random(hashlib.sha256(("h:" + seed).encode()).hexdigest())
    first = "".join(ch for ch in persona.first_name.lower() if ch.isalnum())
    last = "".join(ch for ch in persona.last_name.lower() if ch.isalnum())
    stem = f"{prefix}{first}{last}" if prefix else f"{first}{last}"
    stem = stem[:20] or "zo"
    return f"{stem}{rng.randint(100, 9999)}"


def local_part_for(persona: Persona, seed: str) -> str:
    """域名邮箱的 local part：名.姓+数字，冲突概率极低且可读。"""
    rng = random.Random(hashlib.sha256(("m:" + seed).encode()).hexdigest())
    first = "".join(ch for ch in persona.first_name.lower() if ch.isalnum())
    last = "".join(ch for ch in persona.last_name.lower() if ch.isalnum())
    return f"{first}.{last}{rng.randint(100, 9999)}"


def make_persona(
    *,
    seed: str | None = None,
    country: str | None = None,
    desktop: bool = False,
    handle_prefix: str = "",
) -> Persona:
    """造一个人。`seed` 给了就可复现（重跑同一单元得到同一套身份）。"""
    seed = seed or "".join(random.choices(string.ascii_lowercase + string.digits, k=16))
    rng = random.Random(hashlib.sha256(seed.encode()).hexdigest())
    first = rng.choice(FIRST_NAMES)
    last = rng.choice(LAST_NAMES)
    tz, locale, accept = geo_profile(country)
    major = rng.choice(CHROME_VERSIONS)
    width = rng.choice([1366, 1440, 1512, 1536, 1600, 1920])
    height = rng.choice([768, 864, 900, 960, 1080])
    persona = Persona(
        first_name=first,
        last_name=last,
        username="",
        handle="",
        chrome_major=major,
        timezone=tz,
        locale=locale,
        accept_language=accept,
        viewport=(width, max(720, height - 120)),
        screen=(width, height),
        device_scale=1.0 if width <= 1600 else 1.25,
        desktop=desktop,
        desktop_version=rng.choice(DESKTOP_UA_VERSIONS) if desktop else "",
        seed=seed,
        note=f"seed={seed[:10]}",
    )
    persona.username = f"{persona.first_name.lower()}.{persona.last_name.lower()}"
    persona.handle = handle_for(persona, seed, prefix=handle_prefix)
    return persona