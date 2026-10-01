"""Event calendar and blackout windows (strategy doc s.4 "Event and expiry rules").

No new entries in a configurable window around the Union Budget, RBI policy, election results
and expiry day; single-stock strategies also stand aside around company results. Existing
positions follow their own exits.
"""

from __future__ import annotations

from datetime import date
from pathlib import Path

import numpy as np
import yaml
from pydantic import BaseModel, Field


class MarketEvent(BaseModel):
    date: date
    type: str               # union_budget | rbi_policy | election_result | other
    note: str = ""


class CalendarConfig(BaseModel):
    blackout_days_before: int = 1      # trading days before a market-wide event
    blackout_days_after: int = 0       # trading days after
    results_window_days: int = 3       # trading days either side of company results
    expiry_day_blackout: bool = True
    holidays: list[date] = Field(default_factory=list)
    events: list[MarketEvent] = Field(default_factory=list)
    results: dict[str, list[date]] = Field(default_factory=dict)   # instrument key -> dates


class EventCalendar:
    def __init__(self, cfg: CalendarConfig | None = None):
        self.cfg = cfg or CalendarConfig()
        self._holidays = np.array(sorted(self.cfg.holidays), dtype="datetime64[D]")

    @classmethod
    def from_yaml(cls, path: str | Path) -> "EventCalendar":
        raw = yaml.safe_load(Path(path).read_text(encoding="utf-8")) or {}
        return cls(CalendarConfig(**raw))

    # -- trading-day arithmetic ------------------------------------------------------------
    def trading_days_between(self, a: date, b: date) -> int:
        """Signed number of trading days from ``a`` to ``b`` (exclusive of ``b``)."""
        return int(np.busday_count(np.datetime64(a, "D"), np.datetime64(b, "D"),
                                   holidays=self._holidays))

    def is_trading_day(self, d: date) -> bool:
        return bool(np.is_busday(np.datetime64(d, "D"), holidays=self._holidays))

    # -- queries ---------------------------------------------------------------------------
    def events_between(self, start: date, end: date) -> list[MarketEvent]:
        """Scheduled market-wide events in [start, end] (used by S5 'no event before exit')."""
        return [e for e in self.cfg.events if start <= e.date <= end]

    def market_blackout(self, d: date) -> str | None:
        """Reason string if ``d`` is inside a market-wide event window, else None."""
        for e in self.cfg.events:
            gap = self.trading_days_between(d, e.date)   # >0 when the event is in the future
            if 0 <= gap <= self.cfg.blackout_days_before or -self.cfg.blackout_days_after <= gap < 0:
                return f"event window: {e.type} on {e.date}"
        return None

    def results_blackout(self, instrument: str, d: date) -> str | None:
        for rd in self.cfg.results.get(instrument, []):
            if abs(self.trading_days_between(d, rd)) <= self.cfg.results_window_days:
                return f"company results on {rd}"
        return None

    def blackout(self, d: date, instrument: str | None = None,
                 expiries: list[date] | None = None) -> str | None:
        """Combined check used by strategies (stand-aside) and the gateway (market-state rule)."""
        reason = self.market_blackout(d)
        if reason:
            return reason
        if instrument:
            reason = self.results_blackout(instrument, d)
            if reason:
                return reason
        if self.cfg.expiry_day_blackout and expiries and d in expiries:
            return f"expiry day {d}"
        return None
