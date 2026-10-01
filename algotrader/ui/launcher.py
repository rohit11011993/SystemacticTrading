"""Start the paper engine from the desktop UI (no Qt dependency).

The UI only ever starts the engine in **paper** mode. Live trading is started deliberately
from the command line after the daily broker login, never by a button.
"""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path


def engine_command(config_root: str | Path) -> list[str]:
    """Command line that runs ``algotrader engine --mode paper`` for this installation."""
    config_root = str(Path(config_root).resolve())
    if getattr(sys, "frozen", False):             # inside AlgoTraderUI.exe: use the sibling exe
        name = "AlgoTrader.exe" if os.name == "nt" else "AlgoTrader"
        return [str(Path(sys.executable).with_name(name)), "engine", "--mode", "paper", "--config", config_root]
    return [sys.executable, "-m", "algotrader", "engine", "--mode", "paper", "--config", config_root]


def start_engine(config_root: str | Path) -> subprocess.Popen:
    """Launch the engine in its own console window so its output stays visible."""
    cmd = engine_command(config_root)
    cwd = str(Path(config_root).resolve().parent)
    kwargs: dict = {"cwd": cwd}
    if os.name == "nt":
        kwargs["creationflags"] = subprocess.CREATE_NEW_CONSOLE   # type: ignore[attr-defined]
    else:
        kwargs["start_new_session"] = True
    if not getattr(sys, "frozen", False):
        # Running from source: make sure the child can import this package.
        root = str(Path(__file__).resolve().parents[2])
        kwargs["env"] = {**os.environ, "PYTHONPATH": root + os.pathsep + os.environ.get("PYTHONPATH", "")}
    return subprocess.Popen(cmd, **kwargs)
