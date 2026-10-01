"""The only AlgoTrader module strategy plugins are allowed to import (FR-5.3, FR-15.2).

Plugins cannot reach the broker, order manager, risk gateway, credentials or configuration:
the loader rejects any import outside the allow-list, and this facade re-exports exactly what a
strategy needs to describe signals and expectations.
"""

from __future__ import annotations

from .. import indicators
from ..core.models import (DataSpec, Expectation, Fill, InstrumentType, Leg, OptionContract,
                           OrderSide, OrderType, Side, Signal, SignalAction, Trade)
from ..regime import Regime
from .base import Context, Strategy

__all__ = [
    "Context", "Strategy", "DataSpec", "Expectation", "Fill", "InstrumentType", "Leg",
    "OptionContract", "OrderSide", "OrderType", "Side", "Signal", "SignalAction", "Trade",
    "Regime", "indicators",
]
