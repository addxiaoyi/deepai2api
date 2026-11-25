"""Identity / persona generation for gab.ai signups."""

from __future__ import annotations

import random
import string
from dataclasses import dataclass


_FIRST = [
    "Ava","Mia","Luna","Sophia","Amelia","Harper","Evelyn","Abigail","Emily","Ella",
    "Noah","Liam","Oliver","Elijah","James","Benjamin","Lucas","Henry","Alexander","Ethan",
]
_LAST = [
    "Carter","Reed","Cooper","Bennett","Morgan","Parker","Hayes","Brooks","Sullivan","Turner",
    "Vance","Marsh","Salim","Lovelace","Turing","Hopper","Ng","Patel","Khan","Silva",
]


def _rand_local(rng: random.Random, n: int = 10) -> str:
    alphabet = string.ascii_lowercase + string.digits
    return "".join(rng.choice(alphabet) for _ in range(n))


def _rand_password(rng: random.Random) -> str:
    # gab.ai accepts >= 8 chars; keep a stable "looks normal" pattern.
    alphabet = string.ascii_letters + string.digits
    core = "".join(rng.choice(alphabet) for _ in range(14))
    return "G" + core + "!7"


@dataclass(slots=True)
class Persona:
    name: str
    local_part: str
    password: str

    def as_dict(self) -> dict:
        return {"name": self.name, "local_part": self.local_part, "password": self.password}


def make_persona(rng: random.Random | None = None) -> Persona:
    rng = rng or random.Random()
    first = rng.choice(_FIRST)
    last = rng.choice(_LAST)
    name = f"{first} {last}"
    local = f"{first}.{last}.{_rand_local(rng, 6)}".lower()
    return Persona(name=name, local_part=local, password=_rand_password(rng))
