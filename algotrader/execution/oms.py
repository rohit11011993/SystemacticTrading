"""Order manager: strict order state machine, idempotency, pacing and multi-leg groups (PRD s.10).

* Every transition is persisted *before* the next action (FR-11.1), so a crash and restart
  resumes from stored state.
* Each order carries the strategy's registered algo tag (REG-3) and a client reference
  (FR-6.6 / FR-10.3). A retry after an unknown outcome first reads the broker's order book and
  never increases risk (FR-10.8).
* A token bucket enforces the orders-per-second ceiling (REG-4, FR-6.7) by pacing.
* Multi-leg structures are sent as a group; if any leg fails, the executor reverses the filled
  legs and raises an alert (FR-10.2).
* Rejections are classified (margin, price band, ban, session, other) and handled by rule, not
  blind retry (FR-10.7).
"""

from __future__ import annotations

import logging
import time as _time
from datetime import datetime
from typing import Callable

from ..core.audit import AuditLog
from ..core.models import (Fill, Order, OrderSide, OrderStatus, OrderType, ProductType, Purpose,
                           new_id)
from ..core.state import StateStore
from .broker import BrokerGateway, BrokerTimeout, OrderRejected

log = logging.getLogger(__name__)

S = OrderStatus
ALLOWED_TRANSITIONS: dict[OrderStatus, set[OrderStatus]] = {
    S.CREATED: {S.SENT, S.REJECTED, S.CANCELLED},
    S.SENT: {S.ACKNOWLEDGED, S.PARTIALLY_FILLED, S.FILLED, S.REJECTED, S.CANCELLED, S.EXPIRED},
    S.ACKNOWLEDGED: {S.PARTIALLY_FILLED, S.FILLED, S.CANCELLED, S.EXPIRED, S.REJECTED},
    S.PARTIALLY_FILLED: {S.PARTIALLY_FILLED, S.FILLED, S.CANCELLED, S.EXPIRED},
}


def classify_rejection(reason: str) -> str:
    r = reason.lower()
    if "margin" in r or "fund" in r:
        return "margin"
    if "band" in r or "circuit" in r or "price range" in r:
        return "price_band"
    if "ban" in r:
        return "ban"
    if "session" in r or "market closed" in r or "outside" in r:
        return "session"
    return "other"


class RateLimiter:
    """Token bucket. In backtests the clock is virtual, so pacing never sleeps."""

    def __init__(self, rate_per_sec: float, virtual: bool = False):
        self.rate = rate_per_sec
        self.capacity = max(1.0, rate_per_sec)
        self.tokens = self.capacity
        self.last = _time.monotonic()
        self.virtual = virtual

    def acquire(self) -> None:
        if self.virtual or self.rate <= 0:
            return
        while True:
            now = _time.monotonic()
            self.tokens = min(self.capacity, self.tokens + (now - self.last) * self.rate)
            self.last = now
            if self.tokens >= 1:
                self.tokens -= 1
                return
            _time.sleep((1 - self.tokens) / self.rate)


class OrderManager:
    def __init__(self, broker: BrokerGateway, store: StateStore, audit: AuditLog,
                 clock: Callable[[], datetime], algo_tags: dict[str, str],
                 limiter: RateLimiter, on_reject: Callable[[Order, str], None] | None = None):
        self.broker = broker
        self.store = store
        self.audit = audit
        self.clock = clock
        self.algo_tags = algo_tags
        self.limiter = limiter
        self.on_reject = on_reject
        self.orders: dict[str, Order] = {}
        self.alerts: list[str] = []
        self._unwound: set[str] = set()

    # -- construction ----------------------------------------------------------------------
    def new_order(self, *, strategy_id: str, symbol: str, instrument: str, side: OrderSide,
                  lots: int, lot_size: int, purpose: Purpose, order_type: OrderType = OrderType.MARKET,
                  trade_id: str | None = None, limit_price: float | None = None,
                  trigger_price: float | None = None, contract=None, intent_id: str | None = None,
                  decision_price: float | None = None, product: ProductType = ProductType.NORMAL) -> Order:
        """Build an order with its algo tag (REG-3) and a fresh client reference (FR-6.6)."""
        return Order(client_ref=new_id("c"), strategy_id=strategy_id,
                     algo_tag=self.algo_tags.get(strategy_id, strategy_id)[:20], symbol=symbol,
                     instrument=instrument, side=side, qty=abs(lots) * lot_size, lots=abs(lots),
                     order_type=order_type, product=product, purpose=purpose, trade_id=trade_id,
                     limit_price=limit_price, trigger_price=trigger_price, contract=contract,
                     intent_id=intent_id, decision_price=decision_price, created_ts=self.clock())

    # -- state machine ---------------------------------------------------------------------
    def _transition(self, order: Order, status: OrderStatus, note: str = "") -> None:
        if status == order.status:
            return
        allowed = ALLOWED_TRANSITIONS.get(order.status, set())
        if status not in allowed:
            raise RuntimeError(f"illegal order transition {order.status.value} -> {status.value} "
                               f"for {order.client_ref}")
        order.status = status
        order.history.append((self.clock().isoformat(), status.value, note))
        self.store.save_order(order)          # persisted before anything else happens

    # -- submission ------------------------------------------------------------------------
    def submit(self, order: Order) -> Order:
        if self.broker.exit_only and order.purpose in (Purpose.ENTRY, Purpose.HEDGE):
            order.status = OrderStatus.REJECTED
            order.reject_reason = "fallback broker is exit-only"
            self.store.save_order(order)
            return order
        self.orders[order.client_ref] = order
        self.store.save_order(order)          # CREATED is persisted first
        self.limiter.acquire()
        self._transition(order, OrderStatus.SENT)
        try:
            order.broker_order_id = self._place_idempotent(order)
            self._transition(order, OrderStatus.ACKNOWLEDGED)
        except OrderRejected as exc:
            order.reject_reason = f"{classify_rejection(exc.reason)}: {exc.reason}"
            self._transition(order, OrderStatus.REJECTED, order.reject_reason)
            self.audit.record("order_rejected", "oms", {"client_ref": order.client_ref,
                              "symbol": order.symbol, "reason": order.reject_reason}, ts=self.clock())
            if self.on_reject:
                self.on_reject(order, order.reject_reason)
        return order

    def _place_idempotent(self, order: Order) -> str:
        """Place once; on an unknown outcome read the broker's book before any retry."""
        try:
            return self.broker.place_order(order)
        except BrokerTimeout:
            existing = self.broker.find_order(order.client_ref)
            if existing:
                return existing["broker_order_id"]
            # Not found: safe to retry exactly the same order (same client ref, same size).
            return self.broker.place_order(order)

    def submit_group(self, orders: list[Order]) -> list[Order]:
        """Send a multi-leg structure as a group. A failed leg reverses the others (FR-10.2).

        Long (protective) legs are sent before short legs so a partially placed options
        structure is never naked.
        """
        gid = new_id("g-")
        ordered = sorted(orders, key=lambda o: 0 if o.side is OrderSide.BUY else 1)
        for o in ordered:
            o.group_id = gid
        done: list[Order] = []
        for o in ordered:
            self.submit(o)
            done.append(o)
            if o.status is OrderStatus.REJECTED:
                self._unwind_group(done, f"leg {o.symbol} rejected: {o.reject_reason}")
                break
        return done

    def _unwind_group(self, legs: list[Order], why: str) -> None:
        self._unwound.add(legs[0].group_id or "")
        msg = f"multi-leg group {legs[0].group_id} broken ({why}); reversing other legs"
        self.alerts.append(msg)
        self.audit.record("group_unwind", "oms", {"group": legs[0].group_id, "why": why}, ts=self.clock())
        for o in legs:
            if o.status in (OrderStatus.SENT, OrderStatus.ACKNOWLEDGED):
                self.cancel(o.client_ref)
            if o.filled_qty:
                lot_size = max(o.qty // max(o.lots, 1), 1)
                rev = self.new_order(strategy_id=o.strategy_id, symbol=o.symbol, instrument=o.instrument,
                                     side=o.side.opposite, lots=o.filled_qty // lot_size,
                                     lot_size=lot_size, purpose=Purpose.REVERSAL,
                                     trade_id=o.trade_id, contract=o.contract)
                self.submit(rev)

    def cancel(self, client_ref: str) -> None:
        o = self.orders.get(client_ref)
        if o is None or o.status.is_terminal or not o.broker_order_id:
            return
        self.broker.cancel_order(o.broker_order_id)

    def modify_stop(self, client_ref: str, trigger: float) -> None:
        o = self.orders.get(client_ref)
        if o is None or o.status.is_terminal or not o.broker_order_id:
            return
        self.broker.modify_order(o.broker_order_id, trigger_price=trigger)
        o.trigger_price = trigger
        o.history.append((self.clock().isoformat(), o.status.value, f"stop -> {trigger}"))
        self.store.save_order(o)

    # -- broker events ---------------------------------------------------------------------
    def poll(self) -> list[tuple[Order, Fill]]:
        """Apply broker fills / status updates. Returns (order, fill) pairs for the book."""
        fills, updates = self.broker.poll()
        out: list[tuple[Order, Fill]] = []
        for f in fills:
            o = self.orders.get(f.client_ref)
            if o is None:
                # Activity the system did not initiate -> system kill switch (PRD s.9).
                self.alerts.append(f"unknown fill {f.client_ref} on {f.symbol}")
                continue
            o.avg_fill_price = (o.avg_fill_price * o.filled_qty + f.price * f.qty) / (o.filled_qty + f.qty)
            o.filled_qty += f.qty
            self._transition(o, OrderStatus.FILLED if o.filled_qty >= o.qty else OrderStatus.PARTIALLY_FILLED)
            self.store.save_fill(f)
            out.append((o, f))
        for u in updates:
            o = self.orders.get(u.client_ref)
            if o is None or u.status in (OrderStatus.ACKNOWLEDGED, OrderStatus.FILLED):
                continue
            if not o.status.is_terminal:
                o.reject_reason = u.reason or o.reject_reason
                self._transition(o, u.status, u.reason)
                if u.status is OrderStatus.REJECTED and self.on_reject:
                    self.on_reject(o, u.reason)
        self._check_groups()
        return out

    def _check_groups(self) -> None:
        groups: dict[str, list[Order]] = {}
        for o in self.orders.values():
            if o.group_id and o.purpose is not Purpose.REVERSAL:
                groups.setdefault(o.group_id, []).append(o)
        for gid, legs in groups.items():
            failed = [o for o in legs if o.status in (OrderStatus.REJECTED, OrderStatus.CANCELLED,
                                                      OrderStatus.EXPIRED)]
            filled = [o for o in legs if o.filled_qty]
            if failed and filled and gid not in self._unwound:
                self._unwind_group(legs, f"{failed[0].symbol} {failed[0].status.value}")

    # -- queries ---------------------------------------------------------------------------
    def open_orders(self, trade_id: str | None = None, purpose: Purpose | None = None) -> list[Order]:
        return [o for o in self.orders.values() if not o.status.is_terminal
                and (trade_id is None or o.trade_id == trade_id)
                and (purpose is None or o.purpose is purpose)]

    def recover(self) -> int:
        """After a restart, reload open orders and re-read their state from the broker."""
        n = 0
        for d in self.store.load_orders(open_only=True):
            info = self.broker.find_order(d["client_ref"])
            self.audit.record("order_recovered", "oms", {"client_ref": d["client_ref"],
                              "broker_state": str(info)}, ts=self.clock())
            n += 1
        return n
