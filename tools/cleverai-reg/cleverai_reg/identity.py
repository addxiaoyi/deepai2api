"""Personas: plausible name / address local part / password per signup unit."""

from __future__ import annotations

import random
import string
import time
from dataclasses import asdict, dataclass

FIRST = (
    "ada", "noor", "kai", "lena", "milo", "iris", "theo", "zara", "omar", "ruth",
    "ivan", "maya", "hugo", "elsa", "finn", "amara", "yuki", "dario", "nina", "sami",
    "jonas", "priya", "marco", "suki", "adam", "leila", "victor", "hana", "paul", "ines",
)
LAST = (
    "lovelace", "haddad", "okafor", "marsh", "vance", "keller", "nasser", "bloom",
    "petrov", "salim", "oka", "reyes", "holm", "abbas", "quinn", "silva", "weber",
    "tanaka", "moreau", "kowalski", "dubois", "rossi", "larsen", "mendes",
)
SYMBOLS = "!@#$%&*?"
# Firebase rejects anything under six characters; the dashboard then bounces the
# password again if it is all lower-case. Nine lower + four digits + a symbol is
# what every successful signup so far used.
LOWER = string.ascii_lowercase


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
    # A pure `first.last1234` local part is a fingerprint on its own: mix in the
    # year-ish suffix only half the time and let the runner's plus-tag carry the
    # uniqueness when the mailbox provider needs it.
    local = f"{first}.{last}{digits}" if rng.random() < 0.6 else f"{first}{digits}{last[:3]}"
    password = (
        rng.choice(string.ascii_uppercase)
        + "".join(rng.choices(LOWER, k=9))
        + "".join(rng.choices(string.digits, k=4))
        + rng.choice(SYMBOLS)
    )
    return Persona(
        first=first,
        last=last,
        local_part=local.lower(),
        name=f"{first.capitalize()} {last.capitalize()}",
        password=password,
    )


def plus_tag(rng: random.Random | None = None) -> str:
    """Short unique tag for catch-all / plus-addressed mailboxes."""
    rng = rng or random.Random()
    return "".join(rng.choices(string.ascii_lowercase + string.digits, k=9)) + str(int(time.time()) % 1000)
