"""身份生成：姓名、邮箱 local part、密码、以及"与出口地理对齐"的指纹种子。

一个 persona 就是一次注册的全部自变量。同一个 persona 的 seed 会同时决定
UA / 时区 / 语言 / 屏幕，所以浏览器腿和协议腿看到的是**同一台机器**。
"""

from __future__ import annotations

import hashlib
import random
import string
from dataclasses import dataclass, field

FIRST = (
    "ada", "alan", "amir", "ana", "aria", "axel", "bea", "ben", "cara", "chen",
    "dana", "dario", "eden", "eli", "emma", "erin", "finn", "gala", "hana", "hugo",
    "ines", "ivan", "jade", "jonas", "kaya", "kira", "leo", "lina", "maya", "milo",
    "nina", "noah", "olga", "omar", "pia", "quinn", "ravi", "rosa", "sami", "sara",
    "tara", "theo", "uma", "vera", "wade", "xena", "yuki", "zane", "zoe", "noor",
)
LAST = (
    "adams", "baker", "bloom", "carter", "diaz", "ellis", "fischer", "gomez",
    "hansen", "ibrahim", "jones", "keller", "lopez", "meyer", "novak", "okafor",
    "petrov", "quinn", "rossi", "silva", "tanaka", "ulrich", "vance", "weber",
    "xu", "yamada", "zhang", "kowalski", "lovelace", "moreau", "salim", "takeda",
)

# 出口 → (时区, 语言, Accept-Language)。geo_align 打开时用它覆盖 persona 默认值。
GEO = {
    "us": ("America/New_York", "en-US", "en-US,en;q=0.9"),
    "ca": ("America/Toronto", "en-CA", "en-CA,en;q=0.9"),
    "gb": ("Europe/London", "en-GB", "en-GB,en;q=0.9"),
    "de": ("Europe/Berlin", "de-DE", "de-DE,de;q=0.9,en;q=0.6"),
    "nl": ("Europe/Amsterdam", "nl-NL", "nl-NL,nl;q=0.9,en;q=0.6"),
    "fr": ("Europe/Paris", "fr-FR", "fr-FR,fr;q=0.9,en;q=0.6"),
    "sg": ("Asia/Singapore", "en-SG", "en-SG,en;q=0.9"),
    "jp": ("Asia/Tokyo", "ja-JP", "ja-JP,ja;q=0.9,en;q=0.6"),
    "hk": ("Asia/Hong_Kong", "zh-HK", "zh-HK,zh;q=0.9,en;q=0.6"),
    "au": ("Australia/Sydney", "en-AU", "en-AU,en;q=0.9"),
    "br": ("America/Sao_Paulo", "pt-BR", "pt-BR,pt;q=0.9,en;q=0.6"),
    "in": ("Asia/Kolkata", "en-IN", "en-IN,en;q=0.9"),
    "": ("America/New_York", "en-US", "en-US,en;q=0.9"),
}

# 免费代理 / 常见机房出口的粗判：没有地理信息时退回美国。
_GEO_HINTS = (
    ("jp", ("japan", "tokyo", ".jp")),
    ("sg", ("singapore", ".sg")),
    ("de", ("germany", "frankfurt", ".de")),
    ("gb", ("london", "united kingdom", ".uk")),
    ("hk", ("hong kong", "hongkong", ".hk")),
)


def geo_for_proxy(proxy: str) -> tuple[str, str, str]:
    """从代理字符串里猜地理。猜不到就退回 en-US —— 宁可不改，也不要错配。"""
    lowered = (proxy or "").lower()
    for code, hints in _GEO_HINTS:
        if any(hint in lowered for hint in hints):
            return GEO[code]
    return GEO[""]


def _syllables(rng: random.Random, count: int) -> str:
    cons = "bcdfghjklmnprstvwz"
    vow = "aeiou"
    out = []
    for _ in range(count):
        out.append(rng.choice(cons) + rng.choice(vow))
    return "".join(out)


@dataclass(slots=True)
class Persona:
    email: str = ""
    local: str = ""
    password: str = ""
    first: str = ""
    last: str = ""
    profile_seed: int = 0
    timezone: str = "America/New_York"
    locale: str = "en-US"
    accept_language: str = "en-US,en;q=0.9"
    domain: str = ""
    extra: dict = field(default_factory=dict)

    @property
    def display(self) -> str:
        name = f"{self.first} {self.last}".strip()
        return name or self.local


def make_password(rng: random.Random) -> str:
    """满足 Novita 的强度条：大小写 + 数字 + 符号，长度 14。"""
    upper = rng.choice(string.ascii_uppercase)
    lower = "".join(rng.choice(string.ascii_lowercase) for _ in range(6))
    digits = "".join(rng.choice(string.digits) for _ in range(4))
    symbol = rng.choice("!@#$%^&*")
    tail = rng.choice(string.ascii_lowercase)
    chars = list(upper + lower + digits + symbol + tail)
    rng.shuffle(chars)
    return "".join(chars)


def make_persona(
    *,
    domain: str = "",
    email: str = "",
    proxy: str = "",
    rng: random.Random | None = None,
    geo_align: bool = True,
    local_prefix: str = "",
) -> Persona:
    rng = rng or random.Random()
    first = rng.choice(FIRST)
    last = rng.choice(LAST)
    if email:
        local = email.split("@")[0]
        domain = email.split("@")[-1] if "@" in email else domain
        full = email
    else:
        # 两种 local 形态混着来：first.last 与 first+随机后缀，避免一眼可聚簇。
        style = rng.random()
        if style < 0.45:
            local = f"{first}.{last}"
        elif style < 0.8:
            local = f"{first}.{last}{rng.randint(1, 9999)}"
        else:
            local = f"{_syllables(rng, 2)}{rng.randint(10, 99)}"
        if local_prefix:
            local = f"{local_prefix}{local}"
        full = f"{local}@{domain}" if domain else local

    tz, locale, accept = geo_for_proxy(proxy) if geo_align else GEO[""]
    seed = int(hashlib.sha256(full.encode("utf-8")).hexdigest()[:8], 16)
    return Persona(
        email=full,
        local=local,
        password=make_password(random.Random(seed ^ 0x5F5F)),
        first=first,
        last=last,
        profile_seed=seed,
        timezone=tz,
        locale=locale,
        accept_language=accept,
        domain=domain,
    )
