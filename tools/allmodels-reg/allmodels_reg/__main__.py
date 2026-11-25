"""`python -m allmodels_reg` — see cli.py for the sub-commands."""

from __future__ import annotations

import sys

from .cli import main

if __name__ == "__main__":
    sys.exit(main())
