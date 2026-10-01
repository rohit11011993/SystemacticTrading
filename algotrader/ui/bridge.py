"""UI <-> engine bridge (no Qt dependency).

Reads:  the engine's published snapshot, orders, equity history, kill-switch state, audit log
        and command results from the state database (SQLite WAL, safe for a second reader).
Writes: operator commands to the command queue. The engine applies each one through the risk
        gateway and records the outcome, so the UI can never bypass a limit (FR-7.1).
"""

from __future__ import annotations

import json
import time as _time
from collections import deque
from pathlib import Path
from typing import Any

from ..core.audit import AuditLog
from ..core.config import load_config
from ..core.costs import CostTable
from ..core.instruments import InstrumentRegistry
from ..core.models import OrderSide
from ..core.state import StateStore

EMPTY_SNAPSHOT: dict[str, Any] = {
    "ts": None, "mode": "-", "capital": 0.0, "nav": 0.0, "day_pnl": 0.0, "week_pnl": 0.0, "month_pnl": 0.0,
    "peak": 0.0, "drawdown_pct": 0.0, "ladder": "GREEN", "margin_used": 0.0, "margin_util_pct": 0.0,
    "margin_cap_pct": 40, "gross": 0.0, "net_beta": 0.0, "gross_cap": 0.0, "net_cap": 0.0, "book_risk": 0.0,
    "book_risk_budget": 0.0, "health": {}, "regime": {}, "kill_switches": [], "trades": [], "strategies": [],
    "prices": {}, "alerts": [], "pending_limit_changes": [], "instrument_flags": {}, "config_hash": "",
}


class UiBridge:
    def __init__(self, config_root: str | Path, store: StateStore | None = None):
        self.root = Path(config_root).resolve()
        self.base = self.root.parent
        self.cfg = load_config(self.root)
        self.registry = InstrumentRegistry.from_yaml(self.root / "instruments.yaml")
        self.costs = CostTable.from_yaml(self.root / "costs.yaml")
        self.store = store or StateStore(str(self.base / self.cfg.system.state_db))
        self.audit_path = self.base / self.cfg.system.audit_log

    # -- reads -----------------------------------------------------------------------------
    def snapshot(self) -> dict[str, Any]:
        snap = self.store.get("ui_snapshot")
        if not snap:
            return dict(EMPTY_SNAPSHOT, capital=self.cfg.system.capital, nav=self.cfg.system.capital)
        return {**EMPTY_SNAPSHOT, **snap}

    def engine_status(self, timeout: float | None = None) -> tuple[bool, float | None]:
        """(alive, seconds since last engine heartbeat)."""
        timeout = timeout or self.cfg.system.heartbeat_timeout_sec
        ts = self.store.heartbeats().get("engine")
        if ts is None:
            return False, None
        age = _time.time() - ts
        return age <= timeout, age

    def orders(self, limit: int = 500) -> list[dict[str, Any]]:
        rows = []
        for o in self.store.load_orders(limit=limit):
            slip = None
            if o.get("filled_qty") and o.get("decision_price"):
                sign = 1 if o["side"] == "BUY" else -1
                slip = (o["avg_fill_price"] - o["decision_price"]) * sign * o["filled_qty"]
            hist = o.get("history") or []
            rows.append({**o, "slippage": slip, "updated": hist[-1][0] if hist else o.get("created_ts")})
        return rows

    def equity(self) -> list[tuple[str, float]]:
        return [(r["day"], r["equity"]) for r in self.store.load_equity()]

    def commands(self, limit: int = 100) -> list[dict[str, Any]]:
        return self.store.commands(limit)

    def audit(self, limit: int = 2000, text: str | None = None) -> list[dict[str, Any]]:
        if not self.audit_path.exists():
            return []
        with self.audit_path.open("r", encoding="utf-8") as fh:
            tail = deque((line for line in fh if line.strip()), maxlen=limit)
        recs = [json.loads(line) for line in tail]
        if text:
            t = text.lower()
            recs = [r for r in recs if t in json.dumps(r).lower()]
        return list(reversed(recs))

    def verify_audit(self) -> tuple[bool, int | None]:
        return AuditLog(self.audit_path).verify() if self.audit_path.exists() else (True, None)

    def instruments(self) -> list[dict[str, Any]]:
        snap = self.snapshot()
        flags = snap.get("instrument_flags", {})
        prices = snap.get("prices", {})
        rows = []
        for i in self.registry.all():
            f = flags.get(i.key, {"banned": i.flags.banned, "circuit": i.flags.circuit})
            spec = i.spec()
            rows.append({"key": i.key, "symbol": i.symbol, "exchange": i.exchange, "type": i.type.value,
                         "lot_size": spec.lot_size, "tick": spec.tick_size, "margin_pct": i.margin_pct,
                         "sector": i.sector, "cluster": i.cluster or "", "cost_profile": i.cost_profile,
                         "banned": f.get("banned"), "circuit": f.get("circuit"), "tradable": i.tradable,
                         "last": (prices.get(i.key) or {}).get("last"),
                         "quote_ts": (prices.get(i.key) or {}).get("ts"),
                         "specs": [f"{s.effective}: lot {s.lot_size}, tick {s.tick_size}" for s in i.specs]})
        return rows

    def effective_limits(self) -> list[dict[str, Any]]:
        """Effective (strictest-wins) limits for every binding x instrument."""
        rows = []
        for b in self.cfg.bindings.bindings:
            for key in b.instruments:
                inst = self.registry.get(key)
                lim = self.cfg.risk.effective_limits(b.strategy, key, inst.type.value, b.risk_profile)
                rows.append({"strategy": b.strategy, "instrument": key, "profile": b.risk_profile or "-",
                             **{k: v for k, v in lim.items() if not isinstance(v, (dict, list))},
                             "blocked_windows": ", ".join(lim.get("blocked_windows", [])),
                             "options": json.dumps(lim.get("options", {}))})
        return rows

    def estimate_exit_cost(self, trades: list[dict[str, Any]], fraction: float = 1.0) -> float:
        """Estimated charges + slippage in rupees to close ``trades`` (shown before confirming)."""
        total = 0.0
        for t in trades:
            for leg in t.get("legs_detail", []):
                qty = int(abs(leg["qty"]) * fraction)
                px = leg.get("last") or leg.get("avg") or 0.0
                if not qty or not px:
                    continue
                key = leg["symbol"].split(":")[0]
                if key not in self.registry:
                    continue
                inst = self.registry.get(key)
                prof = self.costs.profile(inst.cost_profile)
                side = OrderSide.SELL if leg["qty"] > 0 else OrderSide.BUY
                total += CostTable.order_charges(prof, side, px, qty).total
                total += CostTable.slippage(prof, px, qty, inst.tick_size())
        return total

    # -- commands --------------------------------------------------------------------------
    def send(self, kind: str, actor: str = "ui", **body: Any) -> int:
        return self.store.enqueue_command(kind, body, actor)

    def manual_exit(self, trade_id: str, fraction: float = 1.0, reason: str = "") -> int:
        return self.send("manual_exit", trade_id=trade_id, fraction=fraction, reason=reason)

    def exit_all(self, strategy: str | None = None, instrument: str | None = None, reason: str = "") -> int:
        return self.send("exit_all", strategy=strategy, instrument=instrument, reason=reason)

    def adjust_stop(self, trade_id: str, stop: float) -> int:
        return self.send("adjust_stop", trade_id=trade_id, stop=stop)

    def kill(self, flatten: bool = False, reason: str = "") -> int:
        # Also written as an external kill, which the engine honours even before it reaches
        # the command queue, and which the watchdog path already understands.
        self.store.set("external_kill", {"reason": reason or "UI kill button", "actor": "ui",
                                         "flatten": flatten, "handled": False})
        return self.send("kill", flatten=flatten, reason=reason)

    def trip(self, level: str, scope: str, action: str = "BLOCK_ENTRIES", reason: str = "") -> int:
        return self.send("trip", level=level, scope=scope, action=action, reason=reason)

    def reset_request(self, level: str, scope: str, reason: str) -> int:
        return self.send("reset_request", level=level, scope=scope, reason=reason)

    def reset_confirm(self, level: str, scope: str, code: str | None = None) -> int:
        return self.send("reset_confirm", level=level, scope=scope, code=code)

    def pause(self, strategy: str, paused: bool = True) -> int:
        return self.send("pause_strategy" if paused else "resume_strategy", strategy=strategy)

    def set_instrument_flag(self, instrument: str, **flags: bool) -> int:
        return self.send("instrument_flag", instrument=instrument, **flags)

    def propose_limit(self, level: str, scope: str | None, key: str, value: Any, reason: str) -> int:
        return self.send("propose_limit", level=level, scope=scope, key=key, value=value, reason=reason)

    def confirm_limit(self, change_id: int) -> int:
        return self.send("confirm_limit", change_id=change_id)
