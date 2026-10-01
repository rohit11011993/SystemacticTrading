"""S2 Volatility channel breakout (strategy doc s.6).

Enters when price leaves a recent range after a period of quiet, which catches the start of
moves that S1's smoothing enters later, and uses a failed-breakout rule to keep false starts
cheap.

| Step            | Long                                              | Short (mirror)               |
|-----------------|---------------------------------------------------|------------------------------|
| Compression     | Previous day's ATR(10) / ATR(60) <= comp_max      | same                         |
| Entry           | Close above highest high of last n_entry days     | below lowest low             |
| Initial stop    | Entry - k_stop x ATR(20)                          | Entry + k_stop x ATR(20)     |
| Failed breakout | Close back inside the broken channel within 3 days: exit next session            |
| Trend exit      | Close below lowest low of last n_exit days        | above highest high           |

Entries use limit orders with protection (close +/- 0.5 ATR) to avoid chasing a gap.
Per-trade risk is 0.5% of NAV, 0.25% on stock futures (from the portfolio allocation).
"""

from __future__ import annotations

import math

from pydantic import BaseModel, Field

from algotrader.strategy.api import (Context, DataSpec, Expectation, InstrumentType, OrderType, Side,
                                     Signal, Strategy, Trade, indicators as ind)

ATR_FAST, ATR_SLOW, ATR_N = 10, 60, 20
FAIL_DAYS = 3
TIME_BUDGET = 40
LIMIT_PROTECTION_ATR = 0.5
ATR_RATIO_DRIFT = 1.5


class S2Params(BaseModel):
    n_entry: int = Field(20, ge=15, le=40, description="Channel length for the breakout")
    comp_max: float = Field(0.85, ge=0.70, le=1.00, description="Max ATR(10)/ATR(60) before breakout")
    k_stop: float = Field(2.5, ge=2.0, le=3.5, description="Initial stop and size basis, in ATRs")
    n_exit: int = Field(10, ge=7, le=20, description="Exit channel length")


class ChannelBreakout(Strategy):
    id = "S2_CHANNEL_BREAKOUT"
    version = "1.0.0"
    params_model = S2Params
    data_needs = DataSpec(timeframe="day", lookback=160)
    supported_types = frozenset({InstrumentType.INDEX_FUTURE, InstrumentType.COMMODITY_FUTURE,
                                 InstrumentType.CURRENCY_FUTURE, InstrumentType.STOCK_FUTURE})
    description = "Volatility-filtered Donchian breakout with failed-breakout exit"

    def _snapshot(self, df) -> dict | None:
        p = self.params
        a_fast = ind.atr(df, ATR_FAST)
        a_slow = ind.atr(df, ATR_SLOW)
        ratio = a_fast / a_slow
        snap = {
            "close": float(df["close"].iloc[-1]),
            "atr": float(ind.atr(df, ATR_N).iloc[-1]),
            "ratio_prev": float(ratio.iloc[-2]) if len(ratio) > 1 else math.nan,
            "ratio": float(ratio.iloc[-1]),
            "upper": float(ind.highest_high(df, p.n_entry).iloc[-1]),
            "lower": float(ind.lowest_low(df, p.n_entry).iloc[-1]),
            "exit_low": float(ind.lowest_low(df, p.n_exit).iloc[-1]),
            "exit_high": float(ind.highest_high(df, p.n_exit).iloc[-1]),
        }
        return None if any(math.isnan(v) for v in snap.values()) else snap

    @staticmethod
    def _round_tick(px: float, tick: float, up: bool) -> float:
        n = px / tick
        return (math.ceil(n) if up else math.floor(n)) * tick

    def on_bar(self, ctx: Context) -> list[Signal]:
        p = self.params
        out: list[Signal] = []
        for key in ctx.instruments:
            if not ctx.has_data(key, ATR_SLOW + 5):
                continue
            x = self._snapshot(ctx.bars(key))
            if x is None:
                continue
            trade = ctx.open_trade(key)
            if trade is not None:
                if trade.status != "OPEN":
                    continue
                level = trade.meta.get("level", x["close"])
                long = trade.side is Side.LONG
                back_inside = x["close"] < level if long else x["close"] > level
                if trade.bars_held <= FAIL_DAYS and back_inside:
                    out.append(Signal.exit(self.id, trade.trade_id, "failed breakout: back inside channel"))
                elif (long and x["close"] < x["exit_low"]) or (not long and x["close"] > x["exit_high"]):
                    out.append(Signal.exit(self.id, trade.trade_id, f"trend exit ({p.n_exit}-day channel)"))
                continue

            if not ctx.can_enter(key) or x["ratio_prev"] > p.comp_max:
                continue
            tick = ctx.view.get(key).tick_size(ctx.today)
            meta = {"atr_ratio": x["ratio"]}
            if x["close"] > x["upper"]:
                meta["level"] = x["upper"]
                limit = self._round_tick(x["close"] + LIMIT_PROTECTION_ATR * x["atr"], tick, up=True)
                out.append(Signal.enter(self.id, key, Side.LONG, x["close"] - p.k_stop * x["atr"], x["close"],
                                        f"breakout above {p.n_entry}-day high after compression "
                                        f"({x['ratio_prev']:.2f})", atr=x["atr"], meta=meta,
                                        order_type=OrderType.LIMIT, limit_price=limit))
            elif x["close"] < x["lower"]:
                meta["level"] = x["lower"]
                limit = self._round_tick(x["close"] - LIMIT_PROTECTION_ATR * x["atr"], tick, up=False)
                out.append(Signal.enter(self.id, key, Side.SHORT, x["close"] + p.k_stop * x["atr"], x["close"],
                                        f"breakdown below {p.n_entry}-day low after compression "
                                        f"({x['ratio_prev']:.2f})", atr=x["atr"], meta=meta,
                                        order_type=OrderType.LIMIT, limit_price=limit))
        return out

    def expected_path(self, ctx: Context, trade: Trade) -> Expectation:
        key = trade.primary.instrument
        if not ctx.has_data(key, ATR_SLOW + 5):
            return Expectation(drift_reasons=["no fresh data"])
        x = self._snapshot(ctx.bars(key))
        if x is None:
            return Expectation()
        long = trade.side is Side.LONG
        level = trade.meta.get("level", x["close"])
        inside = x["close"] < level if long else x["close"] > level
        drift, off = [], []
        if inside and trade.bars_held > FAIL_DAYS:
            drift.append("close back inside the channel after day 3")
        if trade.meta.get("atr_ratio") and x["ratio"] > ATR_RATIO_DRIFT * trade.meta["atr_ratio"]:
            drift.append("ATR ratio above 1.5x its entry value")
        if inside and trade.bars_held <= FAIL_DAYS:
            off.append("failed-breakout rule triggered")
        if (long and x["close"] < x["exit_low"]) or (not long and x["close"] > x["exit_high"]):
            off.append("trend exit hit")
        stop_hit = trade.stop_price is not None and (
            x["close"] <= trade.stop_price if long else x["close"] >= trade.stop_price)
        return Expectation(corridor=(-1.05, None), hold_condition=not inside,
                           invalidation_level=trade.stop_price, invalidation_crossed=stop_hit,
                           time_budget_days=TIME_BUDGET, drift_reasons=drift, off_reasons=off)
