"""Persona generation: name, username, password, email local part, browser hints.

Everything is derived from `random.Random`, so a seeded run is reproducible and
the account row stays self-describing (we store the persona it was created from).
"""

from __future__ import annotations

import random
import string
from dataclasses import dataclass, asdict
from typing import Any

FIRST_NAMES = (
    "marcus", "elena", "tobias", "nadia", "julian", "priya", "owen", "sasha",
    "liam", "maya", "noah", "ivy", "ethan", "clara", "devon", "ruby",
    "felix", "hazel", "jonas", "lena", "mateo", "nora", "oscar", "paula",
    "quentin", "rosa", "simon", "tessa", "victor", "willa", "xavier", "yara",
    "zane", "adela", "bruno", "celine", "dmitri", "esme", "fabian", "greta",
    "hugo", "ines", "jasper", "karina", "leon", "mira", "nikolai", "olive",
    "pablo", "rhea", "soren", "tamar", "ulric", "vera", "wade", "xenia",
    "yusuf", "zelda", "aria", "boris",
)

LAST_NAMES = (
    "whitaker", "kessler", "navarro", "lindqvist", "okonkwo", "petrov",
    "delgado", "harrington", "sorensen", "mihailovic", "fontaine", "abbott",
    "brennan", "castillo", "dumas", "eriksen", "ferreira", "galloway",
    "halloran", "ivanova", "jansen", "kowalski", "lambert", "moreau",
    "nystrom", "ortega", "pemberton", "quintero", "radcliffe", "salazar",
    "thibault", "ulrich", "varga", "winslow", "yildiz", "zeller",
    "beckett", "cormier", "dvorak", "eastwood",
)

LOCALES = (
    ("en-US", "America/New_York"),
    ("en-US", "America/Chicago"),
    ("en-GB", "Europe/London"),
    ("en-CA", "America/Toronto"),
    ("en-AU", "Australia/Sydney"),
)

PASSWORD_SYMBOLS = "!@#$%^&*-_=+?"


def _password(rng: random.Random, length: int = 15) -> str:
    """Mixed-case + digits + symbol; avoids chars that break multipart or shells."""
    core: list[str] = [
        rng.choice(string.ascii_uppercase),
        rng.choice(string.ascii_lowercase),
        rng.choice(string.digits),
        rng.choice(PASSWORD_SYMBOLS),
    ]
    alphabet = string.ascii_letters + string.digits
    core += [rng.choice(alphabet) for _ in range(max(4, length - len(core)))]
    rng.shuffle(core)
    return "".join(core)


@dataclass(slots=True)
class Persona:
    first_name: str
    last_name: str
    local_part: str
    password: str
    locale: str
    timezone: str

    @property
    def display_name(self) -> str:
        return f"{self.first_name.title()} {self.last_name.title()}"

    def as_dict(self) -> dict[str, Any]:
        return asdict(self)


def make_persona(rng: random.Random | None = None, *, local_suffix: str = "") -> Persona:
    """`local_suffix` lets the caller append uniqueness (e.g. a counter)."""
    rng = rng or random.Random()
    first = rng.choice(FIRST_NAMES)
    last = rng.choice(LAST_NAMES)
    digits = "".join(rng.choice(string.digits) for _ in range(rng.choice((2, 3, 4))))
    local = f"{first}.{last}{digits}{local_suffix}"
    locale, tz = rng.choice(LOCALES)
    return Persona(
        first_name=first,
        last_name=last,
        local_part=local.lower(),
        password=_password(rng),
        locale=locale,
        timezone=tz,
    )
