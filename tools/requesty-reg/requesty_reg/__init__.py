"""requesty_reg —— Requesty 注册机（共用实现见 tools/linekit）。"""

from __future__ import annotations

import pathlib
import sys

_KIT = pathlib.Path(__file__).resolve().parents[3] / "tools" / "linekit"
if _KIT.exists() and str(_KIT) not in sys.path:
    sys.path.insert(0, str(_KIT))

from .site import SITE  # noqa: E402

__version__ = "0.1.0"
__all__ = ["SITE", "__version__"]
