"""Allow ``python -m algotrader <sub-command>``; also the PyInstaller entry point."""

import sys

from .cli import main

sys.exit(main())
