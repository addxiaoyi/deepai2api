"""身份与指纹。

Blackbox 只认 Google 登录，所以"注册机"真正池化的是 **Google 身份**：

    * 一个身份 = 一个 Google 账号（邮箱 + 密码 + 可选 TOTP / 已登录 cookies）
    * 一个身份只对应一个 Blackbox 账号（同一 Google 反复授权只会回到同一个账号）
    * 一个身份全程绑定一个出口 IP + 一套自洽指纹（Google 风控最忌中途换环境）

`--google-pool` 读 JSONL，一行一个身份：

    {"email":"a@own.example.com","password":"…","totp_secret":"BASE32","note":"batch-1"}
    {"email":"b@own.example.com","cookies":[{"name":"SID","value":"…","domain":".google.com"}]}

没有 pool 时退回 `--mail-provider` 造地址 + `fixed` 复用（只适合"身份由人工建好、
注册机只负责过 OAuth + 领 Key"的场景）。
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import os
import random
import string
import struct
import time
from dataclasses import asdict, dataclass, field

from .config import GoogleIdentity

FIRST_NAMES = [
    "Ada", "Grace", "Linus", "Ken", "Barbara", "Alan", "Margaret", "Dennis",
    "Radia", "Vint", "Katherine", "Edsger", "Frances", "Tim", "Anita", "Bjarne",
    "Yuki", "Haruto", "Sora", "Mei", "Ravi", "Priya", "Omar", "Layla",
    "Noah", "Emma", "Liam", "Olivia", "Mateo", "Sofia", "Lucas", "Chloe",
]

LAST_NAMES = [
    "Lovelace", "Hopper", "Torvalds", "Thompson", "Liskov", "Turing", "Hamilton",
    "Ritchie", "Perlman", "Cerf", "Johnson", "Dijkstra", "Allen", "Berners",
    "Borg", "Stroustrup", "Tanaka", "Sato", "Kim", "Chen", "Patel", "Sharma",
    "Haddad", "Nasser", "Miller", "Brown", "Davis", "Wilson", "Garcia", "Martinez",
]

CHROME_VERSIONS = ["139", "140", "141", "142"]

# 出口国家 → (时区, 语言, Accept-Language)；Google 会拿这三项和 IP 对账
GEO_PROFILES = {
    "US": ("America/New_York", "en-US", "en-US,en;q=0.9"),
    "GB": ("Europe/London", "en-GB", "en-GB,en;q=0.9"),
    "DE": ("Europe/Berlin", "de-DE", "de-DE,de;q=0.9,en-US;q=0.8"),
    "NL": ("Europe/Amsterdam", "en-US", "en-US,en;q=0.9,nl;q=0.7"),
    "SG": ("Asia/Singapore", "en-US", "en-US,en;q=0.9"),
    "JP": ("Asia/Tokyo", "ja-JP", "ja-JP,ja;q=0.9,en-US;q=0.8"),
    "IN": ("Asia/Kolkata", "en-IN", "en-IN,en;q=0.9"),
    "BR": ("America/Sao_Paulo", "pt-BR", "pt-BR,pt;q=0.9,en-US;q=0.8"),
    "": ("America/New_York", "en-US", "en-US,en;q=0.9"),
}


@dataclass
class Persona:
    first_name: str
    last_name: str
    display_name: str
    chrome_major: str = "140"
    platform: str = "Win32"
    timezone: str = "America/New_York"
    locale: str = "en-US"
    accept_language: str = "en-US,en;q=0.9"
    viewport: tuple[int, int] = (1440, 900)
    screen: tuple[int, int] = (1920, 1080)
    device_scale: float = 1.0
    note: str = ""

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
            "Sec-CH-UA-Platform": f'"{self.platform}"',
            "Sec-Fetch-Dest": "document",
            "Sec-Fetch-Mode": "navigate",
            "Sec-Fetch-Site": "none",
            "Sec-Fetch-User": "?1",
        }
        if referer:
            out["Referer"] = referer
        return out

    def xhr_headers(self, *, origin: str, referer: str, json_body: bool = False) -> dict[str, str]:
        return {
            "User-Agent": self.user_agent,
            "Accept": "application/json",
            "Accept-Language": self.accept_language,
            "Content-Type": "application/json" if json_body else "application/x-www-form-urlencoded",
            "Origin": origin,
            "Referer": referer,
            "Sec-CH-UA": self.sec_ch_ua,
            "Sec-CH-UA-Mobile": "?0",
            "Sec-CH-UA-Platform": f'"{self.platform}"',
            "Sec-Fetch-Dest": "empty",
            "Sec-Fetch-Mode": "cors",
            "Sec-Fetch-Site": "same-origin",
            "X-Requested-With": "XMLHttpRequest",
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


def _seed_rng(seed: str) -> random.Random:
    return random.Random(hashlib.sha256(seed.encode()).hexdigest())


def make_persona(*, seed: str | None = None, country: str | None = None) -> Persona:
    """造一个"人"；给了 seed 就完全可复现（重跑同一单元得到同一套指纹）。"""
    seed = seed or "".join(random.choices(string.ascii_lowercase + string.digits, k=16))
    rng = _seed_rng(seed)
    first = rng.choice(FIRST_NAMES)
    last = rng.choice(LAST_NAMES)
    tz, locale, accept = geo_profile(country)
    major = rng.choice(CHROME_VERSIONS)
    width = rng.choice([1366, 1440, 1512, 1536, 1600, 1920])
    height = rng.choice([768, 864, 900, 960, 1080])
    return Persona(
        first_name=first,
        last_name=last,
        display_name=f"{first} {last}",
        chrome_major=major,
        timezone=tz,
        locale=locale,
        accept_language=accept,
        viewport=(width, max(720, height - 120)),
        screen=(width, height),
        device_scale=1.0 if width <= 1600 else 1.25,
        note=f"seed={seed[:10]}",
    )


def local_part_for(persona: Persona, seed: str) -> str:
    """域名邮箱 local part：名.姓+数字，可读、冲突概率低。"""
    rng = _seed_rng("m:" + seed)
    first = "".join(ch for ch in persona.first_name.lower() if ch.isalnum())
    last = "".join(ch for ch in persona.last_name.lower() if ch.isalnum())
    return f"{first}.{last}{rng.randint(100, 9999)}"


# ------------------------------------------------------------------ TOTP
def totp_now(secret: str, *, digits: int = 6, period: int = 30, at: float | None = None) -> str:
    """标准 TOTP（RFC 6238，SHA1）。Google Authenticator 用的就是这套。"""
    if not secret:
        return ""
    normalized = secret.strip().replace(" ", "").upper()
    padding = "=" * (-len(normalized) % 8)
    try:
        key = base64.b32decode(normalized + padding, casefold=True)
    except Exception:  # noqa: BLE001 - 非法 secret 当作没有
        return ""
    counter = int((at if at is not None else time.time()) // period)
    digest = hmac.new(key, struct.pack(">Q", counter), hashlib.sha1).digest()
    offset = digest[-1] & 0x0F
    code = (struct.unpack(">I", digest[offset : offset + 4])[0] & 0x7FFFFFFF) % (10**digits)
    return str(code).zfill(digits)


# ------------------------------------------------------- Google 身份池
def load_identities(path: str) -> list[GoogleIdentity]:
    """读 JSONL / JSON 数组；坏行跳过，不让一行脏数据毁掉整批。"""
    if not path or not os.path.exists(path):
        return []
    identities: list[GoogleIdentity] = []
    with open(path, encoding="utf-8") as handle:
        text = handle.read().strip()
    if not text:
        return []
    rows: list[dict] = []
    if text.startswith("["):
        try:
            parsed = json.loads(text)
            rows = parsed if isinstance(parsed, list) else []
        except json.JSONDecodeError:
            rows = []
    else:
        for line in text.splitlines():
            line = line.strip()
            if not line or line.startswith("#"):
                continue
            try:
                item = json.loads(line)
            except json.JSONDecodeError:
                continue
            if isinstance(item, dict):
                rows.append(item)
    for row in rows:
        email = str(row.get("email") or row.get("address") or "").strip().lower()
        if not email:
            continue
        identities.append(
            GoogleIdentity(
                email=email,
                password=str(row.get("password") or ""),
                totp_secret=str(row.get("totp_secret") or row.get("totp") or ""),
                cookies=row.get("cookies") if isinstance(row.get("cookies"), list) else [],
                note=str(row.get("note") or ""),
            )
        )
    return identities


def load_cookies(path: str) -> list[dict]:
    """读浏览器导出的 cookies JSON（数组，或 {"cookies":[…]}）。"""
    if not path or not os.path.exists(path):
        return []
    try:
        with open(path, encoding="utf-8") as handle:
            raw = json.load(handle)
    except (OSError, json.JSONDecodeError):
        return []
    rows = raw.get("cookies") if isinstance(raw, dict) else raw
    if not isinstance(rows, list):
        return []
    out = []
    for row in rows:
        if not isinstance(row, dict) or not row.get("name"):
            continue
        out.append(
            {
                "name": str(row.get("name")),
                "value": str(row.get("value") or ""),
                "domain": str(row.get("domain") or ""),
                "path": str(row.get("path") or "/"),
            }
        )
    return out


def identity_from_cookies(email: str, cookies: list[dict]) -> GoogleIdentity:
    return GoogleIdentity(email=email, cookies=cookies, note="cookies-only")
