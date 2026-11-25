"""身份与指纹。

注册机最容易被识别的地方不是密码，而是"这个账号和上一个像不像同一个人"：
同名拼音、同 UA、同语言、同秒级时序都会把号串起来。所以这里是每号独立 persona：

    * 名字 / 用户名 / 密码（满足 moonai 的 8 位最小长度）
    * UA + sec-ch-ua 一套（Chrome 版本自洽）
    * 时区 / 语言（`geo_align` 时跟随代理出口国家）
    * 浏览器窗口 / 屏幕尺寸（浏览器腿用）

moonai 的注册表单只要 username / email / password1，**没有人机校验**，
所以 persona 的作用主要是降低"批量特征"而不是过验证码。
"""

from __future__ import annotations

import hashlib
import random
import string
from dataclasses import dataclass, field, asdict

FIRST_NAMES = [
    "Aisha", "Dias", "Aruzhan", "Nurlan", "Madina", "Timur", "Aigerim", "Sanzhar",
    "Dana", "Alikhan", "Kamila", "Yerlan", "Zhanel", "Askar", "Dinara", "Bekzat",
    "Tomiris", "Arman", "Saltanat", "Daulet", "Ainur", "Ruslan", "Meirzhan", "Aizhan",
    "Daniel", "Sofia", "Mark", "Elena", "Igor", "Anna", "Pavel", "Olga",
]

LAST_NAMES = [
    "Nurmaganbetov", "Suleimenova", "Zhaksybek", "Abdrashitov", "Kairatuly",
    "Tolegenova", "Beketov", "Amanzhol", "Serikbay", "Ospanova", "Yerzhanov",
    "Mukhtarova", "Iskakov", "Baitursyn", "Zhumabek", "Sadykova",
    "Petrov", "Ivanova", "Sokolov", "Kuznetsova", "Smirnov", "Popova",
]

# Chrome 主线版本 → sec-ch-ua 自洽的整套头
CHROME_VERSIONS = ["139", "140", "141", "142"]

# 代理出口国家 → (时区, 语言, Accept-Language)
GEO_PROFILES = {
    "KZ": ("Asia/Almaty", "ru-RU", "ru-RU,ru;q=0.9,en-US;q=0.8,en;q=0.7"),
    "RU": ("Europe/Moscow", "ru-RU", "ru-RU,ru;q=0.9,en-US;q=0.8,en;q=0.7"),
    "DE": ("Europe/Berlin", "de-DE", "de-DE,de;q=0.9,en-US;q=0.8,en;q=0.7"),
    "US": ("America/New_York", "en-US", "en-US,en;q=0.9"),
    "NL": ("Europe/Amsterdam", "en-US", "en-US,en;q=0.9,nl;q=0.7"),
    "SG": ("Asia/Singapore", "en-US", "en-US,en;q=0.9"),
    "TR": ("Europe/Istanbul", "tr-TR", "tr-TR,tr;q=0.9,en;q=0.7"),
    "": ("Asia/Almaty", "ru-RU", "ru-RU,ru;q=0.9,en-US;q=0.8,en;q=0.7"),
}


@dataclass
class Persona:
    first_name: str
    last_name: str
    username: str
    password: str
    email: str = ""
    # 指纹
    chrome_major: str = "140"
    platform: str = "Win32"
    timezone: str = "Asia/Almaty"
    locale: str = "ru-RU"
    accept_language: str = "ru-RU,ru;q=0.9,en-US;q=0.8,en;q=0.7"
    viewport: tuple[int, int] = (1440, 900)
    screen: tuple[int, int] = (1920, 1080)
    device_scale: float = 1.0
    note: str = ""

    # ------------------------------------------------------------- 派生属性
    @property
    def user_agent(self) -> str:
        return (
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
            f"(KHTML, like Gecko) Chrome/{self.chrome_major}.0.0.0 Safari/537.36"
        )

    @property
    def sec_ch_ua(self) -> str:
        v = self.chrome_major
        return f'"Chromium";v="{v}", "Google Chrome";v="{v}", "Not?A_Brand";v="99"'

    def headers(self, *, referer: str = "") -> dict[str, str]:
        out = {
            "User-Agent": self.user_agent,
            "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,image/webp,*/*;q=0.8",
            "Accept-Language": self.accept_language,
            "Upgrade-Insecure-Requests": "1",
            "Sec-CH-UA": self.sec_ch_ua,
            "Sec-CH-UA-Mobile": "?0",
            'Sec-CH-UA-Platform': f'"{self.platform}"',
            "Sec-Fetch-Dest": "document",
            "Sec-Fetch-Mode": "navigate",
            "Sec-Fetch-Site": "same-origin",
            "Sec-Fetch-User": "?1",
        }
        if referer:
            out["Referer"] = referer
        return out

    def xhr_headers(self, *, origin: str, referer: str) -> dict[str, str]:
        return {
            "User-Agent": self.user_agent,
            "Accept": "application/json",
            "Accept-Language": self.accept_language,
            "Content-Type": "application/x-www-form-urlencoded",
            "Origin": origin,
            "Referer": referer,
            "Sec-CH-UA": self.sec_ch_ua,
            "Sec-CH-UA-Mobile": "?0",
            'Sec-CH-UA-Platform': f'"{self.platform}"',
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


def password_for(seed: str) -> str:
    """可复现的强密码（同一 seed 永远得到同一个，便于重跑与排障）。"""
    rng = random.Random(hashlib.sha256(seed.encode()).hexdigest())
    symbols = "!@#$%^&*"
    body = "".join(rng.choice(string.ascii_letters + string.digits) for _ in range(10))
    return f"{body[:4].capitalize()}{rng.choice(symbols)}{body[4:]}{rng.randint(10, 99)}"


def username_for(persona: Persona, seed: str) -> str:
    """moonai 的 username 只要求非空；用 名.姓 + 数字，像真人但可复现。"""
    rng = random.Random(hashlib.sha256(("u:" + seed).encode()).hexdigest())
    base = f"{persona.first_name.lower()}.{persona.last_name.lower()}"
    base = "".join(ch for ch in base if ch.isalnum() or ch == ".")
    return f"{base}{rng.randint(10, 9999)}"


def geo_profile(country: str | None) -> tuple[str, str, str]:
    return GEO_PROFILES.get((country or "").upper(), GEO_PROFILES[""])


def make_persona(*, seed: str | None = None, country: str | None = None, domain: str = "") -> Persona:
    """造一个人。`seed` 给了就可复现（重跑同一单元得到同一套凭据）。"""
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
        password=password_for(seed),
        chrome_major=major,
        timezone=tz,
        locale=locale,
        accept_language=accept,
        viewport=(width, max(720, height - 120)),
        screen=(width, height),
        device_scale=1.0 if width <= 1600 else 1.25,
        note=f"seed={seed[:10]}",
    )
    persona.username = username_for(persona, seed)
    return persona


def local_part_for(persona: Persona, seed: str) -> str:
    """域名邮箱的 local part：名.姓+数字，冲突概率极低且可读。"""
    rng = random.Random(hashlib.sha256(("m:" + seed).encode()).hexdigest())
    first = "".join(ch for ch in persona.first_name.lower() if ch.isalnum())
    last = "".join(ch for ch in persona.last_name.lower() if ch.isalnum())
    return f"{first}.{last}{rng.randint(100, 9999)}"
