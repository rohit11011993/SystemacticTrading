"""S1 Adaptive trend: KAMA direction with the efficiency ratio (strategy doc s.5).

Follows persistent moves with Kaufman's adaptive moving average, which tracks price closely when
moves are clean and slows down when price is choppy.

Rules (daily bars, long and short; orders go in after the close for the next session):

| Step          | Long                                                   | Short (mirror)       |
|---------------|--------------------------------------------------------|----------------------|
| Trend filter  | Slow KAMA(10,5,30) higher than 5 days ago              | lower than 5 days ago|
| Entry trigger | Close > fast KAMA(10,2,30) + f_atr x ATR(20), ER>=er_min | Close < fast - margin|
| Initial stop  | Entry - k_init x ATR(20)                               | Entry + k_init x ATR |
| Trailing stop | Highest close since entry - k_trail x ATR(20)          | Lowest close + ...   |
| Exit          | Trailing stop hit, or close back below fast KAMA by the margin                 |

Only four tunable parameters; the ER window (10) and KAMA constants stay at published defaults.
No pyramiding in version 1. No fixed time stop; a review is raised at 60 trading days.
"""

from __future__ import annotations

import math

from pydantic import BaseModel, Field

from algotrader.strategy.api import (Context, DataSpec, Expectation, InstrumentType, Side, Signal,
                                     SignalAction, Strategy, Trade, indicators as ind)

# Fixed (not tuned) constants from the design document.
ER_N = 10
FAST = (10, 2, 30)      # KAMA(10,2,30): entry line
SLOW = (10, 5, 30)      # KAMA(10,5,30): trend direction
ATR_N = 20
SLOPE_LAG = 5
REVIEW_DAYS = 60
VOL_TOLERANCE = 1.5
DRIFT_AFTER_DAYS = 10


class S1Params(BaseModel):
    """Tunable parameters with their test ranges (validated on load)."""

    er_min: float = Field(0.30, ge=0.20, le=0.40, description="Skip entries in noise")
    f_atr: float = Field(0.30, ge=0.10, le=0.50, description="Entry margin beyond KAMA, in ATRs")
    k_init: float = Field(2.5, ge=2.0, le=3.5, description="Initial stop distance in ATRs (sets size)")
    k_trail: float = Field(3.0, ge=2.5, le=4.5, description="Trailing stop distance in ATRs")


class AdaptiveTrend(Strategy):
    id = "S1_ADAPTIVE_TREND"
    version = "1.0.0"
    params_model = S1Params
    data_needs = DataSpec(timeframe="day", lookback=250)
    supported_types = frozenset({InstrumentType.INDEX_FUTURE, InstrumentType.COMMODITY_FUTURE,
                                 InstrumentType.CURRENCY_FUTURE, InstrumentType.STOCK_FUTURE})
    description = "KAMA trend with efficiency-ratio filter, ATR stops"

    # -- indicators ------------------------------------------------------------------------
    @staticmethod
    def _snapshot(df) -> dict | None:
        """Latest indicator values; None while look-backs are still filling."""
        close = df["close"]
        fast = ind.kama(close, *FAST)
        slow = ind.kama(close, *SLOW)
        er = ind.efficiency_ratio(close, ER_N)
        atr = ind.atr(df, ATR_N)
        rv = ind.realized_vol(close, ATR_N)
        if len(slow.dropna()) <= SLOPE_LAG:
            return None
        snap = {"close": float(close.iloc[-1]), "fast": float(fast.iloc[-1]), "slow": float(slow.iloc[-1]),
                "slow_lag": float(slow.iloc[-1 - SLOPE_LAG]), "er": float(er.iloc[-1]),
                "atr": float(atr.iloc[-1]), "rv": float(rv.iloc[-1])}
        if any(math.isnan(v) for v in snap.values()):
            return None
        return snap

    # -- signals ---------------------------------------------------------------------------
    def on_bar(self, ctx: Context) -> list[Signal]:
        p = self.params
        out: list[Signal] = []
        for key in ctx.instruments:
            if not ctx.has_data(key, 80):
                continue
            x = self._snapshot(ctx.bars(key))
            if x is None:
                continue
            margin = p.f_atr * x["atr"]
            trade = ctx.open_trade(key)

            if trade is not None:
                if trade.status != "OPEN":
                    continue                       # entry still working
                if trade.side is Side.LONG:
                    if x["close"] < x["fast"] - margin:
                        out.append(Signal.exit(self.id, trade.trade_id, "close back below fast KAMA by margin"))
                        continue
                    trail = (trade.highest_close or x["close"]) - p.k_trail * x["atr"]
                    if trade.stop_price is None or trail > trade.stop_price:
                        out.append(Signal(self.id, SignalAction.ADJUST_STOP, [], "trail stop",
                                          trade_id=trade.trade_id, stop_price=trail))
                else:
                    if x["close"] > x["fast"] + margin:
                        out.append(Signal.exit(self.id, trade.trade_id, "close back above fast KAMA by margin"))
                        continue
                    trail = (trade.lowest_close or x["close"]) + p.k_trail * x["atr"]
                    if trade.stop_price is None or trail < trade.stop_price:
                        out.append(Signal(self.id, SignalAction.ADJUST_STOP, [], "trail stop",
                                          trade_id=trade.trade_id, stop_price=trail))
                continue

            if not ctx.can_enter(key):
                continue
            meta = {"entry_vol": x["rv"], "entry_er": x["er"]}
            if x["slow"] > x["slow_lag"] and x["close"] > x["fast"] + margin and x["er"] >= p.er_min:
                out.append(Signal.enter(self.id, key, Side.LONG, x["close"] - p.k_init * x["atr"], x["close"],
                                        f"KAMA up-trend, ER {x['er']:.2f}", atr=x["atr"], meta=meta))
            elif x["slow"] < x["slow_lag"] and x["close"] < x["fast"] - margin and x["er"] >= p.er_min:
                out.append(Signal.enter(self.id, key, Side.SHORT, x["close"] + p.k_init * x["atr"], x["close"],
                                        f"KAMA down-trend, ER {x['er']:.2f}", atr=x["atr"], meta=meta))
        return out

    # -- alignment -------------------------------------------------------------------------
    def expected_path(self, ctx: Context, trade: Trade) -> Expectation:
        key = trade.primary.instrument
        x = self._snapshot(ctx.bars(key)) if ctx.has_data(key, 80) else None
        if x is None:
            return Expectation(drift_reasons=["no fresh data"])
        long = trade.side is Side.LONG
        on_side = x["close"] > x["fast"] if long else x["close"] < x["fast"]
        hold = on_side and x["er"] >= self.params.er_min / 2
        stop_hit = trade.stop_price is not None and (
            x["close"] <= trade.stop_price if long else x["close"] >= trade.stop_price)
        pnl = (x["close"] - (trade.entry_price or x["close"])) * trade.primary.qty
        r = pnl / trade.initial_risk if trade.initial_risk else 0.0
        drift, off = [], []
        if trade.bars_held >= DRIFT_AFTER_DAYS and r < 0:
            drift.append(f"R {r:.2f} below zero after {DRIFT_AFTER_DAYS} days")
        if x["er"] < self.params.er_min / 2:
            drift.append(f"ER {x['er']:.2f} below half of er_min")
        if trade.bars_held >= REVIEW_DAYS:
            drift.append(f"review due: {trade.bars_held} trading days in trade")
        if not on_side:
            off.append("close crossed fast KAMA against the position")
        entry_vol = trade.meta.get("entry_vol")
        return Expectation(corridor=(-1.05, None), hold_condition=hold,
                           invalidation_level=trade.stop_price, invalidation_crossed=stop_hit,
                           vol_ratio=(x["rv"] / entry_vol) if entry_vol else None,
                           vol_tolerance=VOL_TOLERANCE, time_budget_days=None,
                           drift_reasons=drift, off_reasons=off)
