"""Persona generation — same source of truth as the DeepAI factory."""

from __future__ import annotations

import random
import sys
from pathlib import Path
from typing import Any

_SIBLING = Path(__file__).resolve().parents[2] / "deepai-reg"


def make_persona(rng: random.Random | None = None, *, local_suffix: str = "") -> Any:
    if _SIBLING.exists() and str(_SIBLING) not in sys.path:
        sys.path.append(str(_SIBLING))
    try:
        from deepai_reg.identity import make_persona as _make  # type: ignore
    except Exception as exc:  # noqa: BLE001
        raise RuntimeError(f"需要 tools/deepai-reg 的 identity 模块：{_SIBLING} -> {exc}") from exc
    return _make(rng, local_suffix=local_suffix)
