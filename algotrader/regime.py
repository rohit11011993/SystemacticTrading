"""Regime engine (strategy doc s.3).

Classifies the market once a day, after the close, on two axes:

* horizontal: Nifty efficiency ratio ER(10) against one shared threshold (0.30)
  -> TREND (ER >= 0.30) or RANGE (ER < 0.30)
* vertical:   India VIX percentile over its own one-year range against 0.80
  -> STRESSED (>= 80th pct) or CALM

Rules implemented:
* **Hysteresis** - a regime change takes effect only after the new condition holds on
  ``confirm_days`` consecutive days (default 2).
* **Permissions, not forecasts** - the engine publishes a table of size multipliers per
  strategy (0 = no new entries). It never closes open trades.
* **Enforced twice** - strategies read ``permission()`` and the risk gateway checks the same
  table in its market-state rule.
"""

from __future__ import annotations

import enum
from dataclasses import dataclass, field
from datetime import date
from typing import Any

import pandas as pd

from .core.config import RegimeConfig
from .indicators import efficiency_ratio, percentile_rank_last


class Regime(str, enum.Enum):
    TREND_CALM = "TREND_CALM"
    TREND_STRESSED = "TREND_STRESSED"
    RANGE_CALM = "RANGE_CALM"
    RANGE_STRESSED = "RANGE_STRESSED"

    @property
    def stressed(self) -> bool:
        return self in (Regime.TREND_STRESSED, Regime.RANGE_STRESSED)

    @property
    def trending(self) -> bool:
        return self in (Regime.TREND_CALM, Regime.TREND_STRESSED)


@dataclass
class RegimeSnapshot:
    day: date | None
    regime: Regime | None
    raw: Regime | None
    er: float | None
    vix_pct: float | None
    changed: bool = False
    pending: Regime | None = None
    pending_days: int = 0
    permissions: dict[str, float] = field(default_factory=dict)


def classify(er: float, vix_pct: float, cfg: RegimeConfig) -> Regime:
    trending = er >= cfg.er_threshold
    stressed = vix_pct >= cfg.vix_stress_pct
    if trending:
        return Regime.TREND_STRESSED if stressed else Regime.TREND_CALM
    return Regime.RANGE_STRESSED if stressed else Regime.RANGE_CALM


class RegimeEngine:
    def __init__(self, cfg: RegimeConfig):
        self.cfg = cfg
        self.current: Regime | None = None
        self.pending: Regime | None = None
        self.pending_days = 0
        self.last: RegimeSnapshot = RegimeSnapshot(None, None, None, None, None)
        self.history: list[RegimeSnapshot] = []

    # -- daily update ----------------------------------------------------------------------
    def update(self, day: date, market_close: pd.Series, vix_close: pd.Series | None) -> RegimeSnapshot:
        """Compute today's raw regime and apply hysteresis. Call once per day after close."""
        er_series = efficiency_ratio(market_close, self.cfg.er_window)
        er = float(er_series.iloc[-1]) if len(er_series) and pd.notna(er_series.iloc[-1]) else None
        vix_pct = None
        if vix_close is not None and len(vix_close) > 20:
            pr = percentile_rank_last(vix_close, self.cfg.vix_lookback)
            vix_pct = pr if pd.notna(pr) else None
        if er is None:
            # Not enough history: publish no permissions at all (fail closed).
            self.last = RegimeSnapshot(day, None, None, None, vix_pct)
            return self.last
        raw = classify(er, vix_pct if vix_pct is not None else 0.0, self.cfg)

        changed = False
        if self.current is None:
            self.current = raw                      # bootstrap on first valid day
            changed = True
        elif raw == self.current:
            self.pending, self.pending_days = None, 0
        else:
            if raw == self.pending:
                self.pending_days += 1
            else:
                self.pending, self.pending_days = raw, 1
            if self.pending_days >= self.cfg.confirm_days:
                self.current, changed = raw, True
                self.pending, self.pending_days = None, 0

        self.last = RegimeSnapshot(day, self.current, raw, er, vix_pct, changed,
                                   self.pending, self.pending_days, self.permission_table())
        self.history.append(self.last)
        return self.last

    # -- permissions -----------------------------------------------------------------------
    def permission_table(self) -> dict[str, float]:
        if self.current is None:
            return {}
        return dict(self.cfg.permissions.get(self.current.value, {}))

    def permission(self, strategy_id: str) -> float:
        """Size multiplier for new entries. Unknown regime or strategy -> 0 (fail closed)."""
        return float(self.permission_table().get(strategy_id, 0.0))

    # -- persistence -----------------------------------------------------------------------
    def to_dict(self) -> dict[str, Any]:
        return {"current": self.current.value if self.current else None,
                "pending": self.pending.value if self.pending else None,
                "pending_days": self.pending_days}

    def load(self, d: dict[str, Any]) -> None:
        self.current = Regime(d["current"]) if d.get("current") else None
        self.pending = Regime(d["pending"]) if d.get("pending") else None
        self.pending_days = int(d.get("pending_days", 0))
