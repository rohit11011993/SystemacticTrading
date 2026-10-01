"""Crash handling for the frozen executables.

A console program that crashes on Windows closes its window instantly, so the operator sees
nothing. ``run_guarded`` keeps the error visible: it prints the traceback, appends it to
``crash.log`` in the working folder (Documents\\AlgoTrader when started from the Start Menu)
and, for console programs, waits for Enter before the window closes.
"""

from __future__ import annotations

import sys
import traceback
from datetime import datetime
from pathlib import Path
from typing import Callable

CRASH_LOG = "crash.log"


def write_crash_log(text: str, folder: str | Path | None = None) -> Path:
    path = Path(folder or Path.cwd()) / CRASH_LOG
    try:
        with path.open("a", encoding="utf-8") as fh:
            fh.write(f"\n=== {datetime.now().isoformat(timespec='seconds')} {' '.join(sys.argv)}\n{text}")
    except OSError:
        path = Path.home() / CRASH_LOG
        with path.open("a", encoding="utf-8") as fh:
            fh.write(text)
    return path


def run_guarded(fn: Callable[[], int], console: bool = True,
                show_error: Callable[[str], None] | None = None) -> int:
    try:
        return int(fn() or 0)
    except SystemExit as exc:          # argparse / deliberate exits keep their code
        code = exc.code if isinstance(exc.code, int) else 1
        if code and console and sys.stdin is not None and sys.stdin.isatty():
            input("\nAlgoTrader stopped. Press Enter to close...")
        return code
    except KeyboardInterrupt:
        return 130
    except Exception:  # noqa: BLE001 - last-resort handler: report, never vanish silently
        tb = traceback.format_exc()
        path = write_crash_log(tb)
        msg = f"AlgoTrader stopped with an error. Details were saved to:\n{path}\n\n{tb}"
        if console:
            print(msg, file=sys.stderr)
            if sys.stdin is not None and sys.stdin.isatty():
                input("Press Enter to close...")
        elif show_error:
            show_error(msg)
        return 1
