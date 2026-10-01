"""PyInstaller entry point for AlgoTrader.exe (absolute import; the package uses relative ones)."""

import sys

from algotrader.cli import main
from algotrader.crashlog import run_guarded

if __name__ == "__main__":
    sys.exit(run_guarded(main, console=True))
