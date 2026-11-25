"""Personas: 每个注册单元一套自洽的身份。

比 `allmodels_reg.identity` 多了两件事：

  * `timezone` / `locale` / `accept_language` —— 必须与代理出口的地理位置一致，
    否则 Cloudflare 与 OAuth 提供方都会给可疑分。`geo_for_proxy()` 按代理出口
    国家给出对应组合。
  * `profile_seed` —— 浏览器指纹的确定性种子，同一个 persona 重跑得到同一套
    canvas/WebGL 噪声，方便复现问题。
"""

from __future__ import annotations

import random
import string
from dataclasses import asdict, dataclass

FIRST = (
    "ada", "noor", "kai", "lena", "milo", "iris", "theo", "zara", "omar", "ruth",
    "ivan", "maya", "hugo", "elsa", "finn", "amara", "yuki", "dario", "nina", "sami",
    "leo", "anya", "ravi", "elin", "tomas", "sara", "jonas", "mei", "adam", "vera",
)
LAST = (
    "lovelace", "haddad", "okafor", "marsh", "vance", "keller", "nasser", "bloom",
    "petrov", "salim", "oka", "reyes", "holm", "abbas", "quinn", "silva",
    "moreau", "tanaka", "schulz", "ivanov", "costa", "berg", "lindqvist", "patel",
)
SYMBOLS = "!@#$%&*?"

# 代理出口国家 → (timezone, locale, accept-language)
GEO = {
    "us": ("America/New_York", "en-US", "en-US,en;q=0.9"),
    "ca": ("America/Toronto", "en-CA", "en-CA,en;q=0.9"),
    "gb": ("Europe/London", "en-GB", "en-GB,en;q=0.9"),
    "de": ("Europe/Berlin", "de-DE", "de-DE,de;q=0.9,en;q=0.6"),
    "fr": ("Europe/Paris", "fr-FR", "fr-FR,fr;q=0.9,en;q=0.6"),
    "nl": ("Europe/Amsterdam", "nl-NL", "nl-NL,nl;q=0.9,en;q=0.6"),
    "sg": ("Asia/Singapore", "en-SG", "en-SG,en;q=0.9"),
    "jp": ("Asia/Tokyo", "ja-JP", "ja-JP,ja;q=0.9,en;q=0.5"),
    "hk": ("Asia/Hong_Kong", "zh-HK", "zh-HK,zh;q=0.9,en;q=0.6"),
    "tw": ("Asia/Taipei", "zh-TW", "zh-TW,zh;q=0.9,en;q=0.6"),
    "kr": ("Asia/Seoul", "ko-KR", "ko-KR,ko;q=0.9,en;q=0.5"),
    "au": ("Australia/Sydney", "en-AU", "en-AU,en;q=0.9"),
    "br": ("America/Sao_Paulo", "pt-BR", "pt-BR,pt;q=0.9,en;q=0.5"),
    "in": ("Asia/Kolkata", "en-IN", "en-IN,en;q=0.9"),
}
DEFAULT_GEO = GEO["us"]


@dataclass(slots=True)
class Persona:
    first: str
    last: str
    local_part: str
    name: str
    password: str
    timezone: str = "America/New_York"
    locale: str = "en-US"
    accept_language: str = "en-US,en;q=0.9"
    profile_seed: int = 0

    def as_dict(self) -> dict[str, object]:
        return asdict(self)


def geo_for_proxy(proxy: str) -> tuple[str, str, str]:
    """从代理字符串里猜出口国家（`...-us-...`、`us.example.com`、`country-us`）。

    猜不出来就用美东 —— 它是 WaveSpeed 用户里最不显眼的一组。
    """
    lowered = (proxy or "").lower()
    if not lowered:
        return DEFAULT_GEO
    # 先看有没有显式的国家段：-us- / _us_ / .us. / country=us / geo-us
    for code in GEO:
        for pattern in (f"-{code}-", f"_{code}_", f".{code}.", f"country-{code}", f"geo-{code}", f"region-{code}"):
            if pattern in lowered:
                return GEO[code]
    # 再看 TLD 风格：proxy.example.co.uk:8080
    for code in GEO:
        if f".{code}:" in lowered or lowered.endswith(f".{code}"):
            return GEO[code]
    return DEFAULT_GEO


def make_persona(
    rng: random.Random | None = None,
    *,
    proxy: str = "",
    geo_align: bool = True,
    locale_override: str = "",
) -> Persona:
    rng = rng or random.Random()
    first = rng.choice(FIRST)
    last = rng.choice(LAST)
    digits = "".join(rng.choices(string.digits, k=rng.randint(2, 4)))
    local = f"{first}.{last}{digits}"
    password = (
        rng.choice(string.ascii_uppercase)
        + "".join(rng.choices(string.ascii_lowercase, k=10))
        + "".join(rng.choices(string.digits, k=4))
        + rng.choice(SYMBOLS)
    )
    if geo_align:
        tz, locale, accept = geo_for_proxy(proxy)
    else:
        tz, locale, accept = DEFAULT_GEO
    if locale_override:
        locale = locale_override
        accept = f"{locale_override},{locale_override.split('-')[0]};q=0.9,en;q=0.5"
    return Persona(
        first=first,
        last=last,
        local_part=local,
        name=f"{first.capitalize()} {last.capitalize()}",
        password=password,
        timezone=tz,
        locale=locale,
        accept_language=accept,
        profile_seed=rng.randint(1, 2**31 - 1),
    )
