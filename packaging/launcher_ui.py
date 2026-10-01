"""PyInstaller entry point for AlgoTraderUI.exe (windowed desktop UI)."""

import sys

from algotrader.crashlog import run_guarded


def _show_error(msg: str) -> None:
    try:
        from PySide6.QtWidgets import QApplication, QMessageBox
        _app = QApplication.instance() or QApplication(sys.argv)
        QMessageBox.critical(None, "AlgoTrader", msg[:4000])
    except Exception:  # noqa: BLE001 - the crash log already has the details
        pass


def _main() -> int:
    from algotrader.ui.main_window import run_ui
    config = sys.argv[1] if len(sys.argv) > 1 else "config"
    return run_ui(config)


if __name__ == "__main__":
    sys.exit(run_guarded(_main, console=False, show_error=_show_error))
