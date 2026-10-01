"""PyInstaller entry point for AlgoTrader.exe (absolute import; the package uses relative ones)."""

import sys

from algotrader.cli import main

if __name__ == "__main__":
    sys.exit(main())
