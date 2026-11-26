"""Personas: name / password per signup unit."""

from __future__ import annotations

import random
import string
from dataclasses import asdict, dataclass

FIRST = ("ada", "noor", "kai", "lena", "milo", "iris", "theo", "zara", "omar", "ruth",
         "ivan", "maya", "hugo", "elsa", "finn", "amara", "yuki", "dario", "nina", "sami")
LAST = ("lovelace", "haddad", "okafor", "marsh", "vance", "keller", "nasser", "bloom",
        "petrov", "salim", "oka", "reyes", "holm", "abbas", "quinn", "silva")
SYMBOLS = "!@#$%&*?"


@dataclass(slots=True)
class Persona:
    first: str
    last: str
    local_part: str
    name: str
    password: str

    def as_dict(self) -> dict[str, str]:
        return asdict(self)


def make_persona(rng: random.Random | None = None) -> Persona:
    rng = rng or random.Random()
    first = rng.choice(FIRST)
    last = rng.choice(LAST)
    digits = "".join(rng.choices(string.digits, k=rng.randint(2, 4)))
    password = (
        rng.choice(string.ascii_uppercase)
        + "".join(rng.choices(string.ascii_lowercase, k=10))
        + "".join(rng.choices(string.digits, k=4))
        + rng.choice(SYMBOLS)
    )
    return Persona(
        first=first,
        last=last,
        local_part=f"{first}.{last}{digits}".lower(),
        name=f"{first.capitalize()} {last.capitalize()}",
        password=password,
    )
