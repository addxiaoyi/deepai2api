from __future__ import annotations

import sys

from linekit.cli import main as _main

from .site import SITE


def main(argv: list[str] | None = None) -> int:
    return _main(SITE, argv)


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main(sys.argv[1:]))
