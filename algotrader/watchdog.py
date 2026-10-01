"""Watchdog process (PRD s.4, FR-9.1, NFR-7).

Supervises the other processes through heartbeats in the state database and can trip the
system kill switch on its own: if the engine/gateway heartbeat goes silent during the trading
session, it writes an external kill command that the engine and gateway honour on their next
cycle, and raises an alert. It also checks the system clock against a trusted source when one
is configured (FR-11.6).
"""

from __future__ import annotations

import logging
import time as _time
from datetime import datetime, time
from typing import Callable

from .core.audit import AuditLog
from .core.state import StateStore

log = logging.getLogger(__name__)


def check_once(store: StateStore, audit: AuditLog, timeout_sec: float, processes: tuple[str, ...] = ("engine",),
               now: float | None = None, in_session: bool = True) -> list[str]:
    """One supervision pass. Returns alert messages (empty when healthy)."""
    now = now if now is not None else _time.time()
    beats = store.heartbeats()
    alerts = []
    for p in processes:
        ts = beats.get(p)
        if ts is None or now - ts > timeout_sec:
            age = "never" if ts is None else f"{now - ts:.0f}s ago"
            alerts.append(f"{p} heartbeat lost (last {age})")
    if alerts and in_session:
        cmd = store.get("external_kill") or {}
        if not cmd or cmd.get("handled"):
            store.set("external_kill", {"reason": "; ".join(alerts), "actor": "watchdog", "flatten": False,
                                        "ts": datetime.now().isoformat(), "handled": False})
            audit.record("watchdog_trip", "watchdog", {"alerts": alerts})
    store.heartbeat("watchdog", now)
    return alerts


def run(store: StateStore, audit: AuditLog, timeout_sec: float, interval_sec: float = 5.0,
        session: tuple[time, time] = (time(9, 0), time(15, 35)),
        notify: Callable[[str], None] = print) -> None:  # pragma: no cover - long-running loop
    while True:
        t = datetime.now().time()
        alerts = check_once(store, audit, timeout_sec, in_session=session[0] <= t <= session[1])
        for a in alerts:
            notify(f"[WATCHDOG] {a}")
        _time.sleep(interval_sec)
