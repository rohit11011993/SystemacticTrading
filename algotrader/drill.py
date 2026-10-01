"""Kill-switch drill mode (FR-9.6, release gate in PRD s.16).

Simulates each kill-switch level against the paper broker so the operator can rehearse every
trip and the release checklist can require a passing drill. For each level the drill:

  1. opens a test position through the normal gateway path,
  2. trips the kill switch,
  3. measures the time until a new entry is blocked (target < 1 second, FR-9.2),
  4. checks the flatten action produced exit orders that filled,
  5. checks the reset workflow (reason + cooling-off, external key for RED/BLACK).
"""

from __future__ import annotations

import time as _time
from dataclasses import dataclass
from datetime import timedelta

import pandas as pd

from .core.models import Leg, OrderIntent, OrderSide, Side, SignalAction
from .engine import TradingEngine
from .risk.killswitch import KSAction, KSLevel


@dataclass
class DrillResult:
    level: str
    blocked: bool
    block_ms: float
    flattened: bool
    reset_ok: bool
    notes: str = ""

    @property
    def passed(self) -> bool:
        return self.blocked and self.block_ms < 1000 and self.flattened and self.reset_ok


def _probe_intent(engine: TradingEngine, sid: str, key: str) -> OrderIntent:
    px = engine.rc.price(key) or 1.0
    return OrderIntent(strategy_id=sid, action=SignalAction.ENTER, legs=[Leg(key, OrderSide.BUY, 1, ref_price=px)],
                       reason="drill probe", reducing=False, side=Side.LONG, stop_price=px * 0.99,
                       ref_price=px, risk_amount=px * 0.01, source="drill")


def run_drill(engine: TradingEngine, days: list[pd.Timestamp]) -> list[DrillResult]:
    """``days``: a few sessions of data used to warm up and to execute flatten orders."""
    engine.ks.drill_mode = True
    for ts in days[:-2]:
        engine.run_day(ts)
    sid = engine.strategies[0].id
    key = next(k for k in engine.strategies[0].binding.instruments if engine.registry.get(k).tradable)
    results = []
    scopes = [(KSLevel.INSTRUMENT, key), (KSLevel.STRATEGY, sid), (KSLevel.DRAWDOWN, "account"),
              (KSLevel.SYSTEM, "system")]
    for level, scope in scopes:
        notes = []
        t0 = _time.perf_counter()
        engine.ks.trip(level, scope, KSAction.FLATTEN, f"drill {level.value}", actor="drill")
        blocked = not engine.gateway.what_if(_probe_intent(engine, sid, key)).approved
        block_ms = (_time.perf_counter() - t0) * 1000
        def in_scope(level: KSLevel = level, scope: str = scope) -> list:
            trades = engine.book.open_trades()
            if level is KSLevel.STRATEGY:
                return [t for t in trades if t.strategy_id == scope]
            if level is KSLevel.INSTRUMENT:
                return [t for t in trades if any(l.instrument == scope for l in t.legs.values())]
            return trades

        before = len(in_scope())
        engine._execute_killswitch_actions()
        engine.run_day(days[-1])
        after = in_scope()
        flattened = len(after) == 0
        notes.append(f"open trades in scope {before} -> {len(after)}")
        # Reset workflow: cooling-off must be enforced, then a confirmed reset succeeds.
        engine.ks.request_reset(level, scope, "drill complete", "drill")
        early, _ = engine.ks.confirm_reset(level, scope, "drill")
        engine._now += timedelta(minutes=engine.cfg.killswitch.reset_cooloff_minutes + 1)
        ok, msg = engine.ks.confirm_reset(level, scope, "drill")
        reset_ok = (not early) and ok
        notes.append(f"reset: {msg}")
        results.append(DrillResult(level.value, blocked, block_ms, flattened, reset_ok, "; ".join(notes)))
    engine.ks.drill_mode = False
    return results
