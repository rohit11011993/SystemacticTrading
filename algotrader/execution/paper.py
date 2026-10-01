"""Paper broker: simulated fills with the configured cost and slippage model (FR-6.8).

Used for backtests (driven bar by bar) and paper trading (driven by quotes). Fill model:

* ``process_open(symbol, bar)``      - MARKET orders fill at the open (+/- slippage); LIMIT
                                       orders fill at the open if it is through the limit.
* ``process_intraday(symbol, bar)``  - resting LIMIT orders fill at the limit if the bar
                                       trades through it; SL-M stops trigger on the bar's range
                                       and fill at the stop, or at the open on a gap through it.
* ``on_quote(symbol, last, bid, ask)`` - for instruments without bars (option contracts):
                                       buys fill at the ask, sells at the bid.

Assumption, stated plainly: within a bar the engine places the protective stop after the
opening fill, so a stop is active from the same bar's intraday phase onward. Unfilled LIMIT
orders expire after ``limit_validity_bars``.
"""

from __future__ import annotations

import copy
import itertools
from collections import defaultdict
from datetime import datetime
from typing import Any, Callable

from ..core.costs import CostTable
from ..core.instruments import InstrumentRegistry
from ..core.models import Bar, Fill, Order, OrderSide, OrderStatus, OrderType
from .broker import BrokerGateway, OrderRejected, StatusUpdate


class PaperBroker(BrokerGateway):
    name = "paper"

    def __init__(self, registry: InstrumentRegistry, costs: CostTable, clock: Callable[[], datetime],
                 slippage_mult: float = 1.0, limit_validity_bars: int = 1, exit_only: bool = False):
        self.registry = registry
        self.costs = costs
        self.clock = clock
        self.slippage_mult = slippage_mult
        self.limit_validity = limit_validity_bars
        self.exit_only = exit_only
        self._ids = itertools.count(1)
        self.orders: dict[str, Order] = {}            # broker id -> order copy
        self.by_ref: dict[str, str] = {}              # client ref -> broker id
        self._age: dict[str, int] = defaultdict(int)
        self._fills: list[Fill] = []
        self._updates: list[StatusUpdate] = []
        self._positions: dict[str, int] = defaultdict(int)
        self.reject_next: str | None = None           # fault injection for drills / tests
        self.connected_flag = True

    # -- interface -------------------------------------------------------------------------
    def place_order(self, order: Order) -> str:
        if not self.connected_flag:
            from .broker import BrokerTimeout
            raise BrokerTimeout("paper broker disconnected")
        if self.reject_next:
            reason, self.reject_next = self.reject_next, None
            raise OrderRejected(reason)
        if order.qty <= 0:
            raise OrderRejected("quantity must be positive")
        if order.client_ref in self.by_ref:  # idempotent: same client ref -> same order
            return self.by_ref[order.client_ref]
        bid = f"P{next(self._ids):08d}"
        o = copy.deepcopy(order)
        o.broker_order_id = bid
        o.status = OrderStatus.ACKNOWLEDGED
        self.orders[bid] = o
        self.by_ref[order.client_ref] = bid
        self._updates.append(StatusUpdate(order.client_ref, OrderStatus.ACKNOWLEDGED))
        return bid

    def modify_order(self, broker_order_id: str, trigger_price: float | None = None,
                     limit_price: float | None = None) -> None:
        o = self.orders[broker_order_id]
        if o.status.is_terminal:
            raise OrderRejected("cannot modify a completed order")
        if trigger_price is not None:
            o.trigger_price = trigger_price
        if limit_price is not None:
            o.limit_price = limit_price

    def cancel_order(self, broker_order_id: str) -> None:
        o = self.orders.get(broker_order_id)
        if o and not o.status.is_terminal:
            o.status = OrderStatus.CANCELLED
            self._updates.append(StatusUpdate(o.client_ref, OrderStatus.CANCELLED, "cancelled"))

    def find_order(self, client_ref: str) -> dict[str, Any] | None:
        bid = self.by_ref.get(client_ref)
        if not bid:
            return None
        o = self.orders[bid]
        return {"broker_order_id": bid, "status": o.status, "filled_qty": o.filled_qty,
                "avg_price": o.avg_fill_price}

    def poll(self) -> tuple[list[Fill], list[StatusUpdate]]:
        fills, ups = self._fills, self._updates
        self._fills, self._updates = [], []
        return fills, ups

    def positions(self) -> dict[str, int]:
        return {k: v for k, v in self._positions.items() if v}

    def margins(self) -> dict[str, float]:
        return {"available": float("inf"), "used": 0.0}

    def connected(self) -> bool:
        return self.connected_flag

    def restore_positions(self, positions: dict[str, int]) -> None:
        """Paper mode across restarts: the simulated account starts from the internal book."""
        self._positions = defaultdict(int, positions)

    # -- simulation ------------------------------------------------------------------------
    def _open_orders(self, symbol: str) -> list[Order]:
        return [o for o in self.orders.values() if o.symbol == symbol and not o.status.is_terminal]

    def _slip(self, o: Order) -> float:
        inst = self.registry.get(o.instrument)
        on = self.clock().date()
        prof = self.costs.profile(inst.cost_profile, on)
        tick = inst.tick_size(on)
        return self.slippage_mult * (prof.slippage_ticks * tick)

    def _fill(self, o: Order, price: float) -> None:
        inst = self.registry.get(o.instrument)
        on = self.clock().date()
        tick = inst.tick_size(on)
        price = max(tick, round(price / tick) * tick)
        prof = self.costs.profile(inst.cost_profile, on)
        qty = o.remaining
        charges = self.costs.order_charges(prof, o.side, price, qty).total
        o.avg_fill_price = (o.avg_fill_price * o.filled_qty + price * qty) / (o.filled_qty + qty)
        o.filled_qty += qty
        o.status = OrderStatus.FILLED
        self._positions[o.symbol] += qty * o.side.sign
        self._fills.append(Fill(o.client_ref, o.broker_order_id or "", o.symbol, o.side, qty, price,
                                self.clock(), charges))
        self._updates.append(StatusUpdate(o.client_ref, OrderStatus.FILLED))

    def process_open(self, symbol: str, bar: Bar) -> None:
        for o in self._open_orders(symbol):
            slip = self._slip(o) * o.side.sign
            if o.order_type is OrderType.MARKET:
                self._fill(o, bar.open + slip)
            elif o.order_type is OrderType.LIMIT and o.limit_price is not None:
                through = bar.open <= o.limit_price if o.side is OrderSide.BUY else bar.open >= o.limit_price
                if through:
                    self._fill(o, bar.open)
            elif o.order_type is OrderType.SL_M and o.trigger_price is not None:
                gap = bar.open <= o.trigger_price if o.side is OrderSide.SELL else bar.open >= o.trigger_price
                if gap:  # gapped through the stop: filled at the open, not the stop
                    self._fill(o, bar.open + slip)

    def process_intraday(self, symbol: str, bar: Bar) -> None:
        for o in self._open_orders(symbol):
            slip = self._slip(o) * o.side.sign
            if o.order_type is OrderType.LIMIT and o.limit_price is not None:
                hit = bar.low <= o.limit_price if o.side is OrderSide.BUY else bar.high >= o.limit_price
                if hit:
                    self._fill(o, o.limit_price)
                else:
                    self._age[o.client_ref] += 1
                    if self._age[o.client_ref] >= self.limit_validity:
                        o.status = OrderStatus.EXPIRED
                        self._updates.append(StatusUpdate(o.client_ref, OrderStatus.EXPIRED,
                                                          "limit not reached within validity"))
            elif o.order_type is OrderType.SL_M and o.trigger_price is not None:
                hit = bar.low <= o.trigger_price if o.side is OrderSide.SELL else bar.high >= o.trigger_price
                if hit:
                    self._fill(o, o.trigger_price + slip)
            elif o.order_type is OrderType.MARKET:
                self._fill(o, bar.close + slip)

    def on_quote(self, symbol: str, last: float, bid: float | None = None, ask: float | None = None) -> None:
        """Quote-driven fills (paper mode and instruments without bars, e.g. options)."""
        for o in self._open_orders(symbol):
            if o.order_type is OrderType.MARKET:
                px = (ask if o.side is OrderSide.BUY else bid) or last
                self._fill(o, px)
            elif o.order_type is OrderType.LIMIT and o.limit_price is not None:
                px = (ask if o.side is OrderSide.BUY else bid) or last
                if (o.side is OrderSide.BUY and px <= o.limit_price) or (
                        o.side is OrderSide.SELL and px >= o.limit_price):
                    self._fill(o, px)
            elif o.order_type is OrderType.SL_M and o.trigger_price is not None:
                if (o.side is OrderSide.SELL and last <= o.trigger_price) or (
                        o.side is OrderSide.BUY and last >= o.trigger_price):
                    self._fill(o, last)
