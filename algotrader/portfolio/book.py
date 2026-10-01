"""Trade book: one virtual book per strategy (strategy doc s.10 "Netting and attribution").

The broker nets positions for margin, but every strategy keeps its own trades here so that
profit, risk and kill switches are measured separately, and a hedge from one strategy (S3's
Nifty short) never hides risk in another. The broker's figures remain the truth: the
reconciler compares ``net_positions()`` with the broker (FR-11.3).
"""

from __future__ import annotations

from collections import defaultdict
from datetime import datetime
from typing import Iterable

from ..core.models import Fill, Order, OrderSide, Purpose, Side, Trade, TradeLeg


class Book:
    def __init__(self) -> None:
        self.trades: dict[str, Trade] = {}

    # -- lifecycle -------------------------------------------------------------------------
    def add(self, trade: Trade) -> Trade:
        self.trades[trade.trade_id] = trade
        return trade

    def get(self, trade_id: str | None) -> Trade | None:
        return self.trades.get(trade_id) if trade_id else None

    def apply_fill(self, order: Order, fill: Fill) -> Trade | None:
        """Update the owning trade from one fill and return it."""
        trade = self.get(order.trade_id)
        if trade is None:
            return None
        leg = trade.legs.get(order.symbol)
        if leg is None:  # e.g. a hedge leg added later
            raise KeyError(f"trade {trade.trade_id} has no leg {order.symbol}")
        signed = fill.qty * (1 if fill.side is OrderSide.BUY else -1)
        leg.apply(signed, fill.price)
        trade.charges += fill.charges
        if order.decision_price and order.purpose is Purpose.ENTRY:
            # Entry slippage versus the decision price (positive = cost to us). Stop fills
            # that gap are a risk event, not execution quality, so they are not counted here.
            trade.slippage += (fill.price - order.decision_price) * signed

        if order.purpose is Purpose.ENTRY:
            if all(l.qty == l.target_qty for l in trade.legs.values()):
                trade.status = "OPEN"
                trade.entry_ts = trade.entry_ts or fill.ts
                trade.entry_price = trade.primary.avg_price
                trade.highest_close = trade.lowest_close = trade.entry_price
        elif order.purpose is Purpose.HEDGE:
            trade.status = "OPEN" if not trade.is_flat else trade.status
            trade.entry_ts = trade.entry_ts or fill.ts
            trade.entry_price = trade.primary.avg_price or trade.entry_price
            if trade.is_flat and all(l.target_qty == 0 for l in trade.legs.values()):
                self._close(trade, fill.ts, trade.exit_reason or "hedge removed")
        else:  # EXIT / STOP / REVERSAL reduce the trade
            if trade.is_flat:
                self._close(trade, fill.ts, trade.exit_reason or order.purpose.value)
        return trade

    @staticmethod
    def _close(trade: Trade, ts: datetime, reason: str) -> None:
        trade.status = "CLOSED"
        trade.exit_ts = ts
        trade.exit_reason = reason

    def cancel_pending(self, trade_id: str, reason: str) -> None:
        """An entry that never filled (rejected / expired) is cancelled, not closed."""
        t = self.get(trade_id)
        if t and t.status == "PENDING" and t.is_flat:
            t.status = "CANCELLED"
            t.exit_reason = reason

    # -- queries ---------------------------------------------------------------------------
    def open_trades(self, strategy_id: str | None = None, include_pending: bool = True) -> list[Trade]:
        states = ("OPEN", "PENDING") if include_pending else ("OPEN",)
        return [t for t in self.trades.values()
                if t.status in states and (strategy_id is None or t.strategy_id == strategy_id)]

    def closed_trades(self, strategy_id: str | None = None) -> list[Trade]:
        return [t for t in self.trades.values()
                if t.status == "CLOSED" and (strategy_id is None or t.strategy_id == strategy_id)]

    def net_positions(self) -> dict[str, int]:
        """Net signed units per tradable symbol across all strategies (for reconciliation)."""
        net: dict[str, int] = defaultdict(int)
        for t in self.trades.values():
            for sym, leg in t.legs.items():
                if leg.qty:
                    net[sym] += leg.qty
        return {k: v for k, v in net.items() if v}

    def legs(self, strategies: Iterable[str] | None = None) -> list[tuple[Trade, TradeLeg]]:
        wanted = set(strategies) if strategies is not None else None
        return [(t, leg) for t in self.open_trades() for leg in t.legs.values()
                if (wanted is None or t.strategy_id in wanted)]

    # -- PnL and risk ----------------------------------------------------------------------
    def strategy_pnl(self, strategy_id: str, prices: dict[str, float]) -> float:
        """Realised + unrealised PnL net of charges for one strategy's virtual book."""
        return sum(t.pnl(prices) for t in self.trades.values() if t.strategy_id == strategy_id)

    def total_pnl(self, prices: dict[str, float]) -> float:
        return sum(t.pnl(prices) for t in self.trades.values())

    @staticmethod
    def trade_risk(trade: Trade, prices: dict[str, float]) -> float:
        """Rupees currently at risk for a trade.

        Single-leg trades with a stop: distance from price to stop (zero once the trailing stop
        has locked in profit), capped at the initial risk. Otherwise: the initial risk.
        """
        if trade.stop_price is not None and len(trade.legs) == 1:
            leg = trade.primary
            px = prices.get(leg.symbol, leg.avg_price)
            qty = leg.qty if leg.qty else leg.target_qty
            risk = (px - trade.stop_price) * qty
            return max(0.0, min(risk, trade.initial_risk)) if trade.initial_risk else max(0.0, risk)
        return trade.initial_risk

    def open_risk(self, prices: dict[str, float], strategies: Iterable[str] | None = None) -> float:
        wanted = set(strategies) if strategies is not None else None
        return sum(self.trade_risk(t, prices) for t in self.open_trades()
                   if (wanted is None or t.strategy_id in wanted) and t.meta.get("role") != "hedge")

    def mark(self, prices: dict[str, float], closes: dict[str, float]) -> None:
        """Daily mark-to-market: update excursions and the highest / lowest close since entry."""
        for t in self.open_trades(include_pending=False):
            pnl = t.pnl(prices, net=False)
            t.mfe = max(t.mfe, pnl)
            t.mae = min(t.mae, pnl)
            c = closes.get(t.primary.symbol)
            if c is not None:
                t.highest_close = max(t.highest_close or c, c)
                t.lowest_close = min(t.lowest_close or c, c)


def new_trade(trade_id: str, strategy_id: str, side: Side, legs: list[TradeLeg], **kw) -> Trade:
    return Trade(trade_id=trade_id, strategy_id=strategy_id, side=side,
                 legs={leg.symbol: leg for leg in legs}, **kw)
