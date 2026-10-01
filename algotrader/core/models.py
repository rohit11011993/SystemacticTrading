"""Normalised domain model (PRD FR-6.1).

Every adapter (Kite, paper broker, CSV replay) translates vendor formats into these types, so
strategies and the risk gateway never see vendor objects.

Vocabulary
----------
* ``Signal``      - what a strategy *wants* (enter / exit / adjust stop / hedge). Not an order.
* ``OrderIntent`` - a sized signal, ready for the risk gateway.
* ``Order``       - one broker order produced from an approved intent (one per leg).
* ``Fill``        - an execution report from the broker.
* ``Trade``       - a strategy-level position made of one or more legs (single future, pair,
                    iron condor). Trades live in per-strategy virtual books (strategy doc s.10).
* ``Expectation`` - what a strategy says a healthy trade looks like now (``expected_path``),
                    which drives the green / amber / red alignment indicator (PRD s.8).
"""

from __future__ import annotations

import enum
import uuid
from dataclasses import dataclass, field
from datetime import date, datetime
from typing import Any


def new_id(prefix: str = "") -> str:
    """Short unique identifier used for trades, intents and client order references."""
    return f"{prefix}{uuid.uuid4().hex[:12]}"


# --------------------------------------------------------------------------------------------
# Enumerations
# --------------------------------------------------------------------------------------------
class Side(enum.IntEnum):
    """Direction of a position. The integer value doubles as the sign of the quantity."""

    LONG = 1
    SHORT = -1

    @property
    def opposite(self) -> "Side":
        return Side(-self.value)

    def entry_order_side(self) -> "OrderSide":
        """Order side that *opens* a position in this direction."""
        return OrderSide.BUY if self is Side.LONG else OrderSide.SELL

    def exit_order_side(self) -> "OrderSide":
        """Order side that *closes* a position in this direction."""
        return OrderSide.SELL if self is Side.LONG else OrderSide.BUY


class OrderSide(str, enum.Enum):
    BUY = "BUY"
    SELL = "SELL"

    @property
    def sign(self) -> int:
        return 1 if self is OrderSide.BUY else -1

    @property
    def opposite(self) -> "OrderSide":
        return OrderSide.SELL if self is OrderSide.BUY else OrderSide.BUY


class InstrumentType(str, enum.Enum):
    """Instrument types from the instrument registry (PRD s.5)."""

    INDEX_FUTURE = "index_future"
    STOCK_FUTURE = "stock_future"
    INDEX_OPTION = "index_option"
    EQUITY = "equity"
    CURRENCY_FUTURE = "currency_future"
    COMMODITY_FUTURE = "commodity_future"
    INDEX = "index"  # non-tradable reference series, e.g. India VIX


class SignalAction(str, enum.Enum):
    ENTER = "ENTER"            # open a new trade
    EXIT = "EXIT"              # close a trade completely
    REDUCE = "REDUCE"          # close a fraction of a trade (risk-reducing)
    ADJUST_STOP = "ADJUST_STOP"  # move a stop; only allowed in the direction of lower risk
    HEDGE = "HEDGE"            # set the target size of a hedge trade (S3 Nifty hedge)


class OrderType(str, enum.Enum):
    MARKET = "MARKET"   # sent with market protection by the live adapter
    LIMIT = "LIMIT"
    SL_M = "SL-M"       # stop-loss market: the protective stop that rests with the broker


class ProductType(str, enum.Enum):
    """Kite-style product codes (FR-10.4)."""

    INTRADAY = "MIS"
    NORMAL = "NRML"
    DELIVERY = "CNC"


class OrderStatus(str, enum.Enum):
    """Order lifecycle (PRD s.10). Transitions are enforced by ``execution.oms``."""

    CREATED = "CREATED"
    SENT = "SENT"
    ACKNOWLEDGED = "ACKNOWLEDGED"
    PARTIALLY_FILLED = "PARTIALLY_FILLED"
    FILLED = "FILLED"
    CANCELLED = "CANCELLED"
    REJECTED = "REJECTED"
    EXPIRED = "EXPIRED"

    @property
    def is_terminal(self) -> bool:
        return self in (OrderStatus.FILLED, OrderStatus.CANCELLED, OrderStatus.REJECTED,
                        OrderStatus.EXPIRED)


class Verdict(str, enum.Enum):
    """Risk-gateway decision (PRD s.7)."""

    ALLOW = "ALLOW"
    RESIZE = "RESIZE"
    BLOCK = "BLOCK"
    FLATTEN = "FLATTEN"


class AlignmentState(str, enum.Enum):
    """Three-state alignment indicator. Text is always shown with colour (FR-12.4)."""

    IN_LINE = "IN_LINE"        # green
    DRIFTING = "DRIFTING"      # amber
    OFF_THESIS = "OFF_THESIS"  # red


class Mode(str, enum.Enum):
    BACKTEST = "backtest"
    PAPER = "paper"
    LIVE = "live"


class Purpose(str, enum.Enum):
    """Why an order exists; used to update the trade book correctly on fills."""

    ENTRY = "entry"
    EXIT = "exit"
    STOP = "stop"
    HEDGE = "hedge"
    REVERSAL = "reversal"  # unwinding a broken multi-leg group (FR-10.2)


# --------------------------------------------------------------------------------------------
# Market data
# --------------------------------------------------------------------------------------------
@dataclass(frozen=True)
class Bar:
    ts: datetime
    open: float
    high: float
    low: float
    close: float
    volume: float = 0.0


@dataclass(frozen=True)
class OptionContract:
    """One listed option contract. The symbol format is internal; adapters map it to vendor
    trading symbols."""

    underlying: str        # registry key of the option instrument, e.g. "NIFTY_OPT"
    expiry: date
    strike: float
    right: str             # "CE" or "PE"

    @property
    def symbol(self) -> str:
        strike = int(self.strike) if float(self.strike).is_integer() else self.strike
        return f"{self.underlying}:{self.expiry:%Y%m%d}:{strike}:{self.right}"


@dataclass
class Quote:
    """Latest price snapshot for one tradable symbol (used for data-health checks)."""

    symbol: str
    last: float
    ts: datetime
    bid: float | None = None
    ask: float | None = None
    cross_check_ok: bool = True   # FR-6.9 secondary-source check result


# --------------------------------------------------------------------------------------------
# Strategy output
# --------------------------------------------------------------------------------------------
@dataclass
class Leg:
    """One leg of a signal or intent.

    ``instrument`` is the registry key (spec lookup); ``contract`` is set for option legs.
    ``lots`` may be left ``None`` on single-leg entries, in which case the shared sizer
    computes it from the stop distance (strategy doc s.4).
    """

    instrument: str
    side: OrderSide
    lots: int | None = None
    contract: OptionContract | None = None
    ref_price: float | None = None
    meta: dict[str, Any] = field(default_factory=dict)

    @property
    def symbol(self) -> str:
        """Tradable symbol: the option contract symbol or the instrument key."""
        return self.contract.symbol if self.contract else self.instrument


@dataclass
class Signal:
    """An intent emitted by a strategy. It never reaches the broker directly."""

    strategy_id: str
    action: SignalAction
    legs: list[Leg]
    reason: str
    trade_id: str | None = None          # required for EXIT / REDUCE / ADJUST_STOP / HEDGE
    side: Side | None = None             # trade direction for ENTER
    stop_price: float | None = None      # protective stop (single-leg) or new stop (ADJUST)
    ref_price: float | None = None       # decision price (close at signal time)
    atr: float | None = None             # volatility at signal time; used for costs / alignment
    max_loss: float | None = None        # rupees, for pre-sized or defined-risk structures
    risk_pct: float | None = None        # per-trade risk as a fraction of NAV
    expected_edge: float | None = None   # optional expected gross profit (rupees) for cost rule
    reduce_fraction: float = 1.0         # for REDUCE
    order_type: OrderType = OrderType.MARKET
    limit_price: float | None = None
    meta: dict[str, Any] = field(default_factory=dict)

    @staticmethod
    def enter(strategy_id: str, instrument: str, side: Side, stop: float, ref_price: float,
              reason: str, atr: float | None = None, **kw: Any) -> "Signal":
        """Convenience constructor for the common single-leg entry."""
        return Signal(strategy_id=strategy_id, action=SignalAction.ENTER,
                      legs=[Leg(instrument=instrument, side=side.entry_order_side(),
                                ref_price=ref_price)],
                      reason=reason, side=side, stop_price=stop, ref_price=ref_price,
                      atr=atr, **kw)

    @staticmethod
    def exit(strategy_id: str, trade_id: str, reason: str, **kw: Any) -> "Signal":
        """Close the whole trade; legs are filled in by the engine from the book."""
        return Signal(strategy_id=strategy_id, action=SignalAction.EXIT, legs=[],
                      reason=reason, trade_id=trade_id, **kw)


@dataclass
class OrderIntent:
    """A sized signal submitted to the risk gateway."""

    strategy_id: str
    action: SignalAction
    legs: list[Leg]
    reason: str
    reducing: bool                       # risk-reducing intents skip most rules (FR-7.1)
    trade_id: str | None = None
    side: Side | None = None
    stop_price: float | None = None
    ref_price: float | None = None
    atr: float | None = None
    risk_amount: float | None = None     # rupees at risk (stop distance x units, or max loss)
    expected_edge: float | None = None
    order_type: OrderType = OrderType.MARKET
    limit_price: float | None = None
    source: str = "strategy"             # strategy | manual | killswitch | engine
    intent_id: str = field(default_factory=lambda: new_id("int-"))
    meta: dict[str, Any] = field(default_factory=dict)

    def total_lots(self) -> int:
        return sum(abs(leg.lots or 0) for leg in self.legs)


@dataclass
class Order:
    """One broker order. Persisted on every state transition (FR-11.1)."""

    client_ref: str
    strategy_id: str
    algo_tag: str                        # exchange algo ID (REG-3)
    symbol: str
    instrument: str
    side: OrderSide
    qty: int                             # units (lots x lot size)
    lots: int
    order_type: OrderType
    product: ProductType
    purpose: Purpose
    trade_id: str | None = None
    group_id: str | None = None          # multi-leg group (FR-10.2)
    intent_id: str | None = None
    limit_price: float | None = None
    trigger_price: float | None = None
    contract: OptionContract | None = None
    status: OrderStatus = OrderStatus.CREATED
    filled_qty: int = 0
    avg_fill_price: float = 0.0
    broker_order_id: str | None = None
    reject_reason: str | None = None
    decision_price: float | None = None  # for slippage measurement (FR-10.6)
    created_ts: datetime | None = None
    history: list[tuple[str, str, str]] = field(default_factory=list)

    @property
    def remaining(self) -> int:
        return self.qty - self.filled_qty


@dataclass
class Fill:
    client_ref: str
    broker_order_id: str
    symbol: str
    side: OrderSide
    qty: int
    price: float
    ts: datetime
    charges: float = 0.0
    fill_id: str = field(default_factory=lambda: new_id("fill-"))


# --------------------------------------------------------------------------------------------
# Trade book
# --------------------------------------------------------------------------------------------
@dataclass
class TradeLeg:
    instrument: str
    symbol: str
    lot_size: int
    qty: int = 0                         # signed units currently held
    avg_price: float = 0.0
    realized_pnl: float = 0.0
    contract: OptionContract | None = None
    target_qty: int = 0                  # signed units the entry intends to reach

    def apply(self, signed_qty: int, price: float) -> float:
        """Apply a signed fill; returns realised PnL produced by this fill."""
        realized = 0.0
        if self.qty == 0 or (self.qty > 0) == (signed_qty > 0):
            # Adding to (or opening) the position: weighted average price.
            new_qty = self.qty + signed_qty
            self.avg_price = (self.avg_price * self.qty + price * signed_qty) / new_qty
            self.qty = new_qty
        else:
            closing = min(abs(signed_qty), abs(self.qty))
            direction = 1 if self.qty > 0 else -1
            realized = (price - self.avg_price) * closing * direction
            remainder = abs(signed_qty) - closing
            self.qty += signed_qty
            if self.qty == 0:
                self.avg_price = 0.0
            elif remainder > 0:  # flipped through zero: the remainder opens at fill price
                self.avg_price = price
        self.realized_pnl += realized
        return realized

    def unrealized(self, price: float) -> float:
        return (price - self.avg_price) * self.qty


@dataclass
class Trade:
    """A strategy-level position. ``legs`` is keyed by tradable symbol."""

    trade_id: str
    strategy_id: str
    side: Side
    legs: dict[str, TradeLeg]
    entry_ts: datetime | None = None
    entry_price: float | None = None     # primary-leg average entry
    stop_price: float | None = None      # current protective stop (single-leg trades)
    initial_stop: float | None = None
    initial_risk: float = 0.0            # rupees at risk at entry (R = PnL / initial_risk)
    entry_atr: float | None = None
    status: str = "PENDING"              # PENDING -> OPEN -> CLOSED (or CANCELLED)
    exit_ts: datetime | None = None
    bars_held: int = 0
    highest_close: float | None = None
    lowest_close: float | None = None
    mfe: float = 0.0                     # maximum favourable excursion, rupees
    mae: float = 0.0                     # maximum adverse excursion, rupees (<= 0)
    charges: float = 0.0
    slippage: float = 0.0                # rupees versus decision price (FR-10.6)
    reason: str = ""
    exit_reason: str = ""
    meta: dict[str, Any] = field(default_factory=dict)
    alignment: list[tuple[str, str, list[str]]] = field(default_factory=list)

    @property
    def primary(self) -> TradeLeg:
        return next(iter(self.legs.values()))

    @property
    def is_open(self) -> bool:
        return self.status == "OPEN"

    @property
    def is_flat(self) -> bool:
        return all(leg.qty == 0 for leg in self.legs.values())

    @property
    def lots(self) -> int:
        leg = self.primary
        return abs(leg.qty) // max(leg.lot_size, 1)

    def realized(self) -> float:
        return sum(leg.realized_pnl for leg in self.legs.values())

    def unrealized(self, prices: dict[str, float]) -> float:
        total = 0.0
        for sym, leg in self.legs.items():
            if leg.qty and sym in prices:
                total += leg.unrealized(prices[sym])
        return total

    def pnl(self, prices: dict[str, float], net: bool = True) -> float:
        gross = self.realized() + self.unrealized(prices)
        return gross - self.charges if net else gross

    def r_multiple(self, prices: dict[str, float]) -> float:
        """Profit divided by initial risk (strategy doc s.4)."""
        if self.initial_risk <= 0:
            return 0.0
        return self.pnl(prices, net=False) / self.initial_risk


@dataclass
class Expectation:
    """The five ``expected_path`` items of strategy doc s.4, plus strategy-specific reasons.

    * corridor:      expected R-multiple range at this point in the trade
    * hold:          the entry logic re-evaluated now (True = thesis stands)
    * invalidation:  price at which the thesis is wrong (usually the stop)
    * vol tolerance: realised / entry volatility limit
    * time budget:   maximum days before the trade is stale
    """

    corridor: tuple[float | None, float | None] = (None, None)
    hold_condition: bool = True
    invalidation_level: float | None = None
    invalidation_crossed: bool = False
    vol_ratio: float | None = None
    vol_tolerance: float | None = None
    time_budget_days: int | None = None
    mae_limit_r: float | None = None     # excursion check vs backtest winners
    hedge_ok: bool = True                # hedge integrity for spreads / hedged books
    drift_reasons: list[str] = field(default_factory=list)
    off_reasons: list[str] = field(default_factory=list)


@dataclass(frozen=True)
class DataSpec:
    """What a strategy needs from the data layer (plugin contract, PRD s.5)."""

    timeframe: str = "day"
    lookback: int = 300
    extra_series: tuple[str, ...] = ()   # e.g. ("INDIAVIX", "NIFTY_FUT")
    needs_option_chain: bool = False
