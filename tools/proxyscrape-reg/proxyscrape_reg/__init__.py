"""proxyscrape_reg — proxyscrape.com premium 试用注册机 + 代理解析。"""

from __future__ import annotations

__version__ = "0.1.0"

from .config import Config  # noqa: F401
from .errors import ProxyScrapeError  # noqa: F401
from .protocol import ProxyScrape, extract_proxies, parse_proxy_text  # noqa: F401
from .runner import Runner  # noqa: F401
from .store import Store  # noqa: F401

__all__ = [
    "__version__",
    "Config",
    "ProxyScrape",
    "ProxyScrapeError",
    "Runner",
    "Store",
    "extract_proxies",
    "parse_proxy_text",
]
