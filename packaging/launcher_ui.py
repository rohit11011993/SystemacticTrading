"""PyInstaller entry point for AlgoTraderUI.exe (windowed desktop UI)."""

import sys

from algotrader.ui.main_window import run_ui

if __name__ == "__main__":
    config = sys.argv[1] if len(sys.argv) > 1 else "config"
    sys.exit(run_ui(config))
