"""S5 Defined-risk range-bound options: iron condors (strategy doc s.9). Goes live last.

Collects option premium when the index is moving sideways, using iron condors with bought
wings so the maximum loss is known before entry.

Entry conditions (all required):
  * Range regime: Nifty ER(10) <= 0.30 and ER(30) <= 0.35
  * Volatility:   India VIX between its 40th and 80th percentile over the past year
  * No event:     no Budget / RBI / election / calendar event before the planned exit date
  * Liquidity:    bid-ask spread on all four legs within a set limit
  * Size:         max loss per structure <= 0.5% of NAV; if one lot exceeds that, skip

Construction and exit:
  * Monthly expiry with about ``dte_entry`` days left
  * Short strikes at about ``short_delta`` absolute delta each side; wings a fixed width beyond
  * Profit exit at ``take_profit`` of credit; loss exit at ``loss_stop`` x credit or when a
    short strike's delta reaches 0.35; time exit at 7 days to expiry (avoids the 0.15% STT on
    exercised intrinsic value - the "expiry cost trap")
  * No adjustments: a losing condor is closed, never rolled or repaired
  * A VIX spike above the configured percentile closes all condors and blocks entries
"""

from __future__ import annotations

import math
from datetime import timedelta

import pandas as pd
from pydantic import BaseModel, Field

from algotrader.strategy.api import (Context, DataSpec, Expectation, InstrumentType, Leg, OptionContract,
                                     OrderSide, Side, Signal, SignalAction, Strategy, Trade,
                                     indicators as ind)

ER10_MAX, ER30_MAX = 0.30, 0.35
VIX_LO, VIX_HI = 0.40, 0.80
EXIT_DTE = 7
DELTA_EXIT, DELTA_DRIFT = 0.35, 0.25
IV_DRIFT = 0.20


class S5Params(BaseModel):
    # Tunable
    short_delta: float = Field(0.16, ge=0.10, le=0.25, description="Distance of short strikes")
    dte_entry: int = Field(30, ge=21, le=45, description="Days to expiry at entry")
    take_profit: float = Field(0.50, ge=0.40, le=0.75, description="Profit exit, fraction of credit")
    loss_stop: float = Field(1.0, ge=0.75, le=1.5, description="Loss exit, multiple of credit")
    # Configuration (not tuned)
    vix_series: str = "INDIAVIX"
    vix_spike_pct: float = 0.95
    max_spread_pct: float = 0.15       # (ask - bid) / mid per leg


class RangeIronCondor(Strategy):
    id = "S5_RANGE_IRON_CONDOR"
    version = "1.0.0"
    params_model = S5Params
    data_needs = DataSpec(timeframe="day", lookback=300, extra_series=("INDIAVIX",), needs_option_chain=True)
    supported_types = frozenset({InstrumentType.INDEX_OPTION})
    description = "Defined-risk iron condor in low-ER, mid-VIX regimes"

    # -- helpers ---------------------------------------------------------------------------
    def _vix_pct(self, ctx: Context) -> float | None:
        vix = ctx.bars(self.params.vix_series)["close"]
        if len(vix) < 60:
            return None
        v = ind.percentile_rank_last(vix, 252)
        return None if pd.isna(v) else float(v)

    @staticmethod
    def _row(chain: pd.DataFrame, strike: float, right: str) -> pd.Series | None:
        r = chain[(chain["strike"] == strike) & (chain["right"] == right)]
        return None if r.empty else r.iloc[0]

    def _mark(self, ctx: Context, trade: Trade) -> dict | None:
        """Cost to close at bid/ask, PnL vs credit, short-strike deltas, average short IV."""
        key = trade.meta["opt_key"]
        chain = ctx.option_chain(key, pd.Timestamp(trade.meta["expiry"]).date())
        if chain.empty:
            return None
        cost, deltas, ivs = 0.0, [], []
        for leg in trade.legs.values():
            c = leg.contract
            row = self._row(chain, c.strike, c.right)
            if row is None:
                return None
            units = abs(leg.qty or leg.target_qty)
            if (leg.qty or leg.target_qty) < 0:          # short leg: buy back at the ask
                cost += row["ask"] * units
                deltas.append(abs(row["delta"]))
                ivs.append(row["iv"])
            else:                                         # long wing: sell at the bid
                cost -= row["bid"] * units
        credit = trade.meta["credit"]
        return {"pnl": credit - cost, "max_short_delta": max(deltas) if deltas else 0.0,
                "iv": sum(ivs) / len(ivs) if ivs else None, "spot": float(chain["spot"].iloc[0])}

    # -- signals ---------------------------------------------------------------------------
    def on_bar(self, ctx: Context) -> list[Signal]:
        p = self.params
        out: list[Signal] = []
        vix_pct = self._vix_pct(ctx)
        spike = vix_pct is not None and vix_pct >= p.vix_spike_pct
        for opt_key in ctx.instruments:
            inst = ctx.view.get(opt_key)
            under = inst.underlying
            if not under or not ctx.has_data(under, 40):
                continue
            trade = next((t for t in ctx.open_trades() if t.meta.get("opt_key") == opt_key), None)
            if trade is not None:
                if trade.status == "OPEN":
                    reason = self._exit_reason(ctx, trade, spike)
                    if reason:
                        out.append(Signal.exit(self.id, trade.trade_id, reason))
                continue
            sig = self._entry(ctx, opt_key, under, vix_pct, spike)
            if sig is not None:
                out.append(sig)
        return out

    def _exit_reason(self, ctx: Context, trade: Trade, spike: bool) -> str | None:
        p = self.params
        m = self._mark(ctx, trade)
        expiry = pd.Timestamp(trade.meta["expiry"]).date()
        if spike:
            return "VIX spike: close all condors"
        if (expiry - ctx.today).days <= EXIT_DTE:
            return f"time exit at {EXIT_DTE} days to expiry"
        if ctx.calendar.events_between(ctx.today, expiry):
            return "scheduled event entered the holding window"
        if m is None:
            return None
        credit = trade.meta["credit"]
        if m["pnl"] >= p.take_profit * credit:
            return f"profit target {p.take_profit:.0%} of credit"
        if -m["pnl"] >= p.loss_stop * credit:
            return f"loss stop {p.loss_stop}x credit"
        if m["max_short_delta"] >= DELTA_EXIT:
            return f"short strike delta {m['max_short_delta']:.2f} >= {DELTA_EXIT}"
        return None

    def _entry(self, ctx: Context, opt_key: str, under: str, vix_pct: float | None, spike: bool) -> Signal | None:
        p = self.params
        if spike or vix_pct is None or not (VIX_LO <= vix_pct <= VIX_HI) or not ctx.can_enter(opt_key):
            return None
        close = ctx.bars(under)["close"]
        er10 = float(ind.efficiency_ratio(close, 10).iloc[-1])
        er30 = float(ind.efficiency_ratio(close, 30).iloc[-1])
        if not (er10 <= ER10_MAX and er30 <= ER30_MAX):
            return None
        # Monthly expiry with about dte_entry days left.
        expiries = ctx.expiries(opt_key, ctx.today + timedelta(days=p.dte_entry - 10),
                                ctx.today + timedelta(days=p.dte_entry + 15))
        if not expiries:
            return None
        expiry = min(expiries, key=lambda d: abs((d - ctx.today).days - p.dte_entry))
        if ctx.calendar.events_between(ctx.today, expiry - timedelta(days=EXIT_DTE)):
            return None
        chain = ctx.option_chain(opt_key, expiry)
        if chain.empty:
            return None
        spot = float(chain["spot"].iloc[0])
        calls = chain[(chain["right"] == "CE") & (chain["strike"] > spot)]
        puts = chain[(chain["right"] == "PE") & (chain["strike"] < spot)]
        if calls.empty or puts.empty:
            return None
        sc = calls.iloc[(calls["delta"] - p.short_delta).abs().argsort().iloc[0]]
        sp = puts.iloc[(puts["delta"] + p.short_delta).abs().argsort().iloc[0]]
        inst = ctx.view.get(opt_key)
        width = inst.wing_width or 4 * (inst.strike_step or 50.0)
        lc = self._row(chain, sc["strike"] + width, "CE")
        lp = self._row(chain, sp["strike"] - width, "PE")
        if lc is None or lp is None:
            return None
        rows = {"sc": sc, "sp": sp, "lc": lc, "lp": lp}
        for r in rows.values():
            if r["mid"] <= 0 or (r["ask"] - r["bid"]) / r["mid"] > p.max_spread_pct:
                return None
        credit_unit = sc["bid"] + sp["bid"] - lc["ask"] - lp["ask"]
        if credit_unit <= 0:
            return None
        lot = ctx.view.lot_size(opt_key, ctx.today)
        max_loss_lot = (width - credit_unit) * lot
        budget = ctx.risk_pct * ctx.nav * ctx.permission * ctx.risk_multiplier
        lots = int(math.floor(budget / max_loss_lot)) if max_loss_lot > 0 else 0
        if lots <= 0:
            return None
        units = lots * lot

        def leg(r, side: OrderSide) -> Leg:
            c = OptionContract(opt_key, expiry, float(r["strike"]), str(r["right"]))
            px = float(r["bid"] if side is OrderSide.SELL else r["ask"])
            return Leg(opt_key, side, lots, contract=c, ref_price=px, meta={"delta": float(r["delta"])})

        legs = [leg(lc, OrderSide.BUY), leg(lp, OrderSide.BUY), leg(sc, OrderSide.SELL), leg(sp, OrderSide.SELL)]
        return Signal(self.id, SignalAction.ENTER, legs,
                      f"iron condor {sp['strike']:.0f}/{sc['strike']:.0f} wings {width:.0f}, "
                      f"ER10 {er10:.2f}, VIX pct {vix_pct:.0%}",
                      side=Side.SHORT, ref_price=spot, max_loss=max_loss_lot * lots,
                      expected_edge=credit_unit * units * p.take_profit,
                      meta={"opt_key": opt_key, "expiry": expiry.isoformat(), "credit": credit_unit * units,
                            "iv_entry": float((sc["iv"] + sp["iv"]) / 2), "short_call": float(sc["strike"]),
                            "short_put": float(sp["strike"]), "width": width})

    # -- alignment -------------------------------------------------------------------------
    def expected_path(self, ctx: Context, trade: Trade) -> Expectation:
        p = self.params
        m = self._mark(ctx, trade)
        if m is None:
            return Expectation(drift_reasons=["no option quotes"])
        credit = trade.meta["credit"]
        expiry = pd.Timestamp(trade.meta["expiry"]).date()
        drift, off = [], []
        lo, hi = trade.meta["short_put"], trade.meta["short_call"]
        buffer = 0.25 * (hi - lo) / 2
        if not (lo + buffer < m["spot"] < hi - buffer):
            drift.append("index close to a short strike")
        if m["max_short_delta"] > DELTA_DRIFT:
            drift.append(f"short delta {m['max_short_delta']:.2f} above {DELTA_DRIFT}")
        if m["iv"] and m["iv"] > (1 + IV_DRIFT) * trade.meta["iv_entry"]:
            drift.append("implied volatility more than 20% above entry")
        if (expiry - ctx.today).days <= EXIT_DTE:
            drift.append("time exit due (7 days to expiry)")
        if m["max_short_delta"] >= DELTA_EXIT:
            off.append(f"short delta at {DELTA_EXIT}")
        if -m["pnl"] >= p.loss_stop * credit:
            off.append("loss at the stop")
        if ctx.calendar.events_between(ctx.today, expiry):
            off.append("an event entered the holding window")
        r_floor = -p.loss_stop * credit / trade.initial_risk if trade.initial_risk else None
        return Expectation(corridor=(r_floor, None), hold_condition=m["max_short_delta"] < DELTA_DRIFT,
                           drift_reasons=drift, off_reasons=off)
