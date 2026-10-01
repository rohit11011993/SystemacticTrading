"""Strategy plugin contract (PRD s.5) and the read-only ``Context`` handed to plugins.

A strategy is one Python module implementing ``Strategy``. It **never talks to the broker**:
it receives market data and its own state and returns ``Signal`` objects (intents). The same
code runs unchanged in backtest, paper and live modes (FR-5.6), because the engine builds the
same ``Context`` in all three.
"""

from __future__ import annotations

import copy
import math
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from datetime import date, datetime
from typing import Any, Callable, ClassVar

import pandas as pd
from pydantic import BaseModel

from ..core.calendar import EventCalendar
from ..core.instruments import InstrumentView
from ..core.models import DataSpec, Expectation, Fill, InstrumentType, Signal, Trade
from ..regime import RegimeSnapshot


@dataclass
class Context:
    """Everything a strategy may see on one bar. All objects are copies or read-only views."""

    now: datetime
    strategy_id: str
    instruments: list[str]
    nav: float
    risk_pct: float                     # per-trade risk as a fraction of NAV (0.005 = 0.5%)
    permission: float                   # regime size multiplier; 0 = no new entries
    regime: RegimeSnapshot
    view: InstrumentView
    calendar: EventCalendar
    trades: list[Trade]
    state: dict[str, Any]               # strategy-owned, persisted by the engine
    entries_blocked: bool = False       # kill switch / drawdown state blocks new entries
    risk_multiplier: float = 1.0        # drawdown ladder / soft stop / vol overlay
    risk_pct_stock: float | None = None  # per-trade risk on stock futures, if different
    _bars: dict[str, pd.DataFrame] = field(default_factory=dict, repr=False)
    _flags: dict[str, dict[str, Any]] = field(default_factory=dict, repr=False)
    _option_chain: Callable[[str, date], pd.DataFrame] | None = field(default=None, repr=False)

    # -- data ------------------------------------------------------------------------------
    @property
    def today(self) -> date:
        return self.now.date()

    def bars(self, key: str) -> pd.DataFrame:
        """Daily bars up to and including today (no look-ahead)."""
        df = self._bars.get(key)
        if df is None:
            return pd.DataFrame(columns=["open", "high", "low", "close", "volume"])
        return df

    def has_data(self, key: str, min_bars: int = 1) -> bool:
        df = self._bars.get(key)
        return df is not None and len(df) >= min_bars and df.index[-1].date() == self.today

    def close(self, key: str) -> float:
        return float(self.bars(key)["close"].iloc[-1])

    def option_chain(self, option_key: str, expiry: date) -> pd.DataFrame:
        """Option chain with columns strike, right, bid, ask, mid, iv, delta."""
        if self._option_chain is None:
            return pd.DataFrame()
        return self._option_chain(option_key, expiry)

    def expiries(self, option_key: str, start: date, end: date) -> list[date]:
        return self.view.get(option_key).expiries(start, end)

    # -- book ------------------------------------------------------------------------------
    def open_trades(self, role: str | None = None) -> list[Trade]:
        return [t for t in self.trades if t.status in ("OPEN", "PENDING")
                and (role is None or t.meta.get("role") == role)]

    def open_trade(self, key: str) -> Trade | None:
        """The open (non-hedge) single-instrument trade on ``key``, if any."""
        for t in self.open_trades():
            if t.meta.get("role") != "hedge" and t.primary.instrument == key:
                return t
        return None

    # -- permissions -----------------------------------------------------------------------
    def is_banned(self, key: str) -> bool:
        f = self._flags.get(key, {})
        return bool(f.get("banned") or f.get("circuit"))

    def blackout(self, key: str | None = None) -> str | None:
        expiries = self._flags.get(key, {}).get("expiries") if key else None
        return self.calendar.blackout(self.today, key, expiries)

    def can_enter(self, key: str) -> bool:
        """Strategy-side stand-aside check; the gateway enforces the same rules again."""
        return (self.permission > 0 and not self.entries_blocked and not self.is_banned(key)
                and self.blackout(key) is None)

    # -- sizing (strategy doc s.4) ---------------------------------------------------------
    def risk_pct_for(self, key: str) -> float:
        """Per-trade risk fraction for an instrument (S2: 0.25% on stock futures)."""
        if self.risk_pct_stock is not None and self.view.type(key) is InstrumentType.STOCK_FUTURE:
            return self.risk_pct_stock
        return self.risk_pct

    def size_lots(self, key: str, entry: float, stop: float, risk_pct: float | None = None) -> int:
        """N = floor(r x NAV / (|entry - stop| x LotSize)), scaled by regime and risk multipliers.

        Returns 0 when the instrument is too large for the account: the trade is then skipped,
        never forced to one lot.
        """
        r = (risk_pct if risk_pct is not None else self.risk_pct_for(key)) * self.permission * self.risk_multiplier
        per_lot = abs(entry - stop) * self.view.lot_size(key, self.today)
        if per_lot <= 0 or r <= 0:
            return 0
        return int(math.floor(r * self.nav / per_lot))


class Strategy(ABC):
    """Base class every plugin implements (PRD s.5 plugin contract)."""

    id: ClassVar[str]                           # registered strategy ID (maps to the algo tag)
    version: ClassVar[str]
    params_model: ClassVar[type[BaseModel]]     # validated, tunable parameters
    data_needs: ClassVar[DataSpec] = DataSpec()
    supported_types: ClassVar[frozenset[InstrumentType]] = frozenset()   # FR-5.7
    description: ClassVar[str] = ""

    def __init__(self, params: dict[str, Any] | None = None):
        self.params = self.params_model(**(params or {}))

    @abstractmethod
    def on_bar(self, ctx: Context) -> list[Signal]:
        """Called once per bar (after the daily close). Return entry / exit / adjust intents."""

    def on_fill(self, ctx: Context, fill: Fill) -> None:  # noqa: B027 - optional hook
        """Optional hook called when one of this strategy's orders fills."""

    def expected_path(self, ctx: Context, trade: Trade) -> Expectation:
        """What 'in line' means for this live trade (feeds the alignment indicator)."""
        return Expectation()

    # helper for plugins
    def _copy(self, obj: Any) -> Any:
        return copy.deepcopy(obj)
