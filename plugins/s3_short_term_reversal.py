"""S3 Short-term reversal hedged with index futures (strategy doc s.7).

Buys liquid stocks that fell more than the market explains and sells those that rose more than
it explains, hedges the market exposure with Nifty futures, and holds for a few days while the
move partly reverses.

Signal: for each stock, the ``ret_window``-day return net of beta x Nifty return, divided by its
own residual volatility (residual z-score).

| Step            | Long                                   | Short                                  |
|-----------------|----------------------------------------|----------------------------------------|
| Candidate       | residual z <= -z_entry                 | residual z >= z_entry                  |
| Noise condition | stock ER(10) <= 0.30 (fixed)           | same                                   |
| Trend guard     | close above its 100-day average        | close below its 100-day average        |
| Selection       | most stretched first, up to the cap    |                                        |
| Stop            | entry - k_stop x ATR(20)               | entry + k_stop x ATR(20)               |
| Exit            | z back within 0.25 of zero, or after n_hold days                                |

Hedge: the book's beta-weighted notional is offset with Nifty futures in whole lots; the
residual beta must stay within 15% of gross exposure or the new trade is skipped.
Stand-down: India VIX above its 80th percentile, Nifty down more than 5% in 5 days, or an
event window. At most 5 positions, 2 per sector.
"""

from __future__ import annotations

import math

import numpy as np
from pydantic import BaseModel, Field

from algotrader.strategy.api import (Context, DataSpec, Expectation, InstrumentType, Leg, OrderSide,
                                     Side, Signal, SignalAction, Strategy, Trade, indicators as ind)

BETA_N = 120
RESID_N = 60
ER_NOISE = 0.30
SMA_N = 100
EXIT_BAND = 0.25
MAX_POSITIONS = 5
MAX_PER_SECTOR = 2
VIX_STRESS = 0.80
NIFTY_DROP_5D = -0.05
HEDGE_TOLERANCE = 0.15
ATR_N = 20


class S3Params(BaseModel):
    # Tunable (with test ranges)
    z_entry: float = Field(1.5, ge=1.0, le=2.5, description="How stretched a move must be")
    n_hold: int = Field(5, ge=3, le=8, description="Time stop in days")
    k_stop: float = Field(2.0, ge=1.5, le=3.0, description="Stop and size basis in ATRs")
    ret_window: int = Field(3, ge=2, le=5, description="Look-back for the residual move")
    # Wiring (not tuned)
    hedge_instrument: str = "NIFTY_FUT"
    vix_series: str = "INDIAVIX"


class ShortTermReversal(Strategy):
    id = "S3_SHORT_TERM_REVERSAL"
    version = "1.0.0"
    params_model = S3Params
    data_needs = DataSpec(timeframe="day", lookback=300, extra_series=("INDIAVIX", "NIFTY_FUT"))
    supported_types = frozenset({InstrumentType.STOCK_FUTURE, InstrumentType.EQUITY})
    description = "Beta-hedged residual reversal in Nifty 50 names"

    # -- analytics -------------------------------------------------------------------------
    def _stock(self, ctx: Context, key: str, mret) -> dict | None:
        df = ctx.bars(key)
        if not ctx.has_data(key, max(BETA_N, SMA_N) + 5):
            return None
        sret = np.log(df["close"]).diff()
        joined = sret.to_frame("s").join(mret.rename("m"), how="inner").dropna()
        if len(joined) < BETA_N:
            return None
        _, beta = ind.ols_beta(joined["s"].to_numpy()[-BETA_N:], joined["m"].to_numpy()[-BETA_N:])
        resid = joined["s"] - beta * joined["m"]
        vol = float(resid.iloc[-RESID_N:].std())
        w = self.params.ret_window
        if vol <= 0 or math.isnan(vol):
            return None
        z = float(resid.iloc[-w:].sum()) / (vol * math.sqrt(w))
        return {"z": z, "beta": beta, "close": float(df["close"].iloc[-1]),
                "er": float(ind.efficiency_ratio(df["close"], 10).iloc[-1]),
                "sma": float(ind.sma(df["close"], SMA_N).iloc[-1]),
                "atr": float(ind.atr(df, ATR_N).iloc[-1])}

    def _stress(self, ctx: Context) -> str | None:
        vix = ctx.bars(self.params.vix_series)["close"]
        if len(vix) > 30:
            pct = ind.percentile_rank_last(vix, 252)
            if pct >= VIX_STRESS:
                return f"India VIX at {pct:.0%} percentile"
        m = ctx.bars(self.params.hedge_instrument)["close"]
        if len(m) > 6 and float(m.iloc[-1] / m.iloc[-6] - 1.0) <= NIFTY_DROP_5D:
            return "Nifty down more than 5% in 5 days"
        return None

    @staticmethod
    def _beta_notional(book: list[tuple[str, int, int, float, float]]) -> tuple[float, float]:
        """(beta-weighted net notional, gross notional) for (key, side, units, price, beta) rows."""
        bn = sum(side * units * px * beta for _, side, units, px, beta in book)
        gross = sum(abs(units * px) for _, _, units, px, _ in book)
        return bn, gross

    def _hedge_lots(self, bn: float, hedge_px: float, hedge_lot: int) -> int:
        return -int(round(bn / (hedge_px * hedge_lot))) if hedge_px > 0 else 0

    # -- signals ---------------------------------------------------------------------------
    def on_bar(self, ctx: Context) -> list[Signal]:
        p = self.params
        hkey = p.hedge_instrument
        if not ctx.has_data(hkey, BETA_N + 5):
            return []
        mret = np.log(ctx.bars(hkey)["close"]).diff()
        hedge_px = ctx.close(hkey)
        hedge_lot = ctx.view.lot_size(hkey, ctx.today)
        stats = {k: s for k in ctx.instruments if (s := self._stock(ctx, k, mret)) is not None}
        stress = self._stress(ctx)
        out: list[Signal] = []
        reduced = set(ctx.state.setdefault("stress_reduced", []))

        # 1. Exits for open stock trades.
        book: list[tuple[str, int, int, float, float]] = []
        sectors: dict[str, int] = {}
        for t in ctx.open_trades():
            if t.meta.get("role") == "hedge":
                continue
            key = t.primary.instrument
            s = stats.get(key)
            exiting = False
            if t.status == "OPEN" and s is not None:
                if abs(s["z"]) <= EXIT_BAND:
                    out.append(Signal.exit(self.id, t.trade_id, f"residual z {s['z']:.2f} back near zero"))
                    exiting = True
                elif t.bars_held >= p.n_hold:
                    out.append(Signal.exit(self.id, t.trade_id, f"time stop {p.n_hold} days"))
                    exiting = True
                elif ctx.is_banned(key) or ctx.blackout(key):
                    out.append(Signal.exit(self.id, t.trade_id, "ban or event window"))
                    exiting = True
                elif stress and t.trade_id not in reduced and t.lots >= 2:
                    out.append(Signal(self.id, SignalAction.REDUCE, [], f"stress stand-down: {stress}",
                                      trade_id=t.trade_id, reduce_fraction=0.5))
                    reduced.add(t.trade_id)
            if not exiting:
                units = abs(t.primary.qty or t.primary.target_qty)
                px = s["close"] if s else (t.entry_price or 0.0)
                beta = s["beta"] if s else ctx.view.beta(key)
                book.append((key, t.side.value, units, px, beta))
                sec = ctx.view.sector(key)
                sectors[sec] = sectors.get(sec, 0) + 1
        ctx.state["stress_reduced"] = sorted(reduced)

        # 2. New entries (stand-down rules first).
        if not stress and ctx.permission > 0 and not ctx.entries_blocked and ctx.blackout(None) is None:
            held = {row[0] for row in book}
            cands = []
            for key, s in stats.items():
                if key in held or not ctx.can_enter(key) or s["er"] > ER_NOISE:
                    continue
                if s["z"] <= -p.z_entry and s["close"] > s["sma"]:
                    cands.append((abs(s["z"]), key, Side.LONG))
                elif s["z"] >= p.z_entry and s["close"] < s["sma"]:
                    cands.append((abs(s["z"]), key, Side.SHORT))
            for _, key, side in sorted(cands, reverse=True):
                if len(book) >= MAX_POSITIONS:
                    break
                sec = ctx.view.sector(key)
                if sectors.get(sec, 0) >= MAX_PER_SECTOR:
                    continue
                s = stats[key]
                stop = s["close"] - side.value * p.k_stop * s["atr"]
                lots = ctx.size_lots(key, s["close"], stop)
                if lots <= 0:
                    continue
                units = lots * ctx.view.lot_size(key, ctx.today)
                trial = book + [(key, side.value, units, s["close"], s["beta"])]
                bn, gross = self._beta_notional(trial)
                h = self._hedge_lots(bn, hedge_px, hedge_lot)
                residual = bn + h * hedge_px * hedge_lot
                if gross > 0 and abs(residual) > HEDGE_TOLERANCE * gross:
                    continue      # cannot be hedged inside tolerance with whole lots: skip
                book = trial
                sectors[sec] = sectors.get(sec, 0) + 1
                out.append(Signal.enter(self.id, key, side, stop, s["close"],
                                        f"residual z {s['z']:.2f}, beta {s['beta']:.2f}", atr=s["atr"],
                                        meta={"z_entry": s["z"], "beta": s["beta"]}))
                out[-1].legs[0].lots = lots

        # 3. Market hedge: whole Nifty lots offsetting the beta-weighted book.
        bn, _ = self._beta_notional(book)
        target = self._hedge_lots(bn, hedge_px, hedge_lot) if book else 0
        current = next((t.primary.qty // hedge_lot for t in ctx.open_trades("hedge")), 0)
        if target != current:
            side = OrderSide.BUY if target > current else OrderSide.SELL
            out.append(Signal(self.id, SignalAction.HEDGE, [Leg(hkey, side, abs(target - current))],
                              f"beta hedge to {target} lots (book beta notional {bn:,.0f})",
                              meta={"target_lots": target}))
        return out

    # -- alignment -------------------------------------------------------------------------
    def expected_path(self, ctx: Context, trade: Trade) -> Expectation:
        p = self.params
        key = trade.primary.instrument
        hkey = p.hedge_instrument
        if not ctx.has_data(hkey, BETA_N + 5):
            return Expectation(drift_reasons=["no market data"])
        mret = np.log(ctx.bars(hkey)["close"]).diff()
        s = self._stock(ctx, key, mret)
        if s is None:
            return Expectation(drift_reasons=["no fresh data"])
        z0 = trade.meta.get("z_entry", s["z"])
        drift, off = [], []
        if trade.bars_held > 2 and abs(s["z"]) > abs(z0) + 0.5:
            drift.append(f"residual z {s['z']:.2f} worse than entry {z0:.2f} by 0.5")
        # Hedge integrity across the whole S3 book.
        rows = []
        for t in ctx.open_trades():
            if t.meta.get("role") == "hedge" or not t.primary.qty:
                continue
            k = t.primary.instrument
            if ctx.has_data(k):
                rows.append((k, 1 if t.primary.qty > 0 else -1, abs(t.primary.qty), ctx.close(k),
                             t.meta.get("beta", 1.0)))
        bn, gross = self._beta_notional(rows)
        hedge_units = sum(t.primary.qty for t in ctx.open_trades("hedge"))
        residual = bn + hedge_units * ctx.close(hkey)
        hedge_ok = gross == 0 or abs(residual) <= HEDGE_TOLERANCE * gross
        stress = self._stress(ctx)
        if stress:
            off.append(f"market stress: {stress}")
        if ctx.is_banned(key) or ctx.blackout(key):
            off.append("stock in ban or event window")
        long = trade.side is Side.LONG
        stop_hit = trade.stop_price is not None and (
            s["close"] <= trade.stop_price if long else s["close"] >= trade.stop_price)
        return Expectation(corridor=(-1.05, None), hold_condition=abs(s["z"]) <= abs(z0) + 0.5,
                           invalidation_level=trade.stop_price, invalidation_crossed=stop_hit,
                           time_budget_days=p.n_hold, hedge_ok=hedge_ok,
                           drift_reasons=drift, off_reasons=off)
