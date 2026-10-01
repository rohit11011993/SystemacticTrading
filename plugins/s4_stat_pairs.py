"""S4 Statistical pairs on sector spreads (strategy doc s.8).

Trades the price gap between two closely related stocks: buys the cheap one and sells the
expensive one when the gap is unusually wide, exits when it narrows. Market-neutral.

Pair selection and monitoring (weekly):
  1. Formation window: 250 trading days of daily log prices.
  2. Relationship test: spread = log A - beta x log B must pass ADF at the 5% level.
  3. Speed test: half-life of mean reversion between 3 and 20 trading days.
  4. Hedge ratio: rolling regression over 120 days, converted to whole lots; skip if the lot
     ratio leaves notional imbalance above 10%.
  5. Disable rule: switched off when the test fails at the 10% level; re-enabled only after
     passing again.

| Step        | Long the spread (buy A, sell B)  | Short the spread (sell A, buy B) |
|-------------|----------------------------------|----------------------------------|
| z-score     | (spread - 60d mean) / 60d std    | same                             |
| Entry       | z <= -z_in                       | z >= z_in                        |
| Exit        | |z| <= z_out                     | same                             |
| Stop        | z <= -z_stop                     | z >= z_stop                      |
| Time stop   | twice the half-life, capped at 15 days                              |

Stand-down: spread trending (ER(20) of the spread > 0.5), a leg in ban / event window, pair
risk above 0.5% of NAV. At most 4 pairs, one per sector. Stressed regime -> half size (via the
regime permission table).
"""

from __future__ import annotations

import math
from datetime import date

import numpy as np
from pydantic import BaseModel, Field

from algotrader.strategy.api import (Context, DataSpec, Expectation, InstrumentType, Leg, OrderSide,
                                     Side, Signal, SignalAction, Strategy, Trade, indicators as ind)

FORMATION_N = 250
BETA_N = 120
HL_MIN, HL_MAX = 3.0, 20.0
ADF_ENABLE = ind.ADF_CRITICAL[0.05]
ADF_DISABLE = ind.ADF_CRITICAL[0.10]
IMBALANCE_MAX = 0.10
SPREAD_ER_N, SPREAD_ER_MAX = 20, 0.5
MAX_PAIRS = 4
TIME_CAP = 15
RESELECT_DAYS = 5


class S4Params(BaseModel):
    # Tunable
    z_in: float = Field(2.0, ge=1.5, le=2.5, description="Entry threshold")
    z_out: float = Field(0.5, ge=0.0, le=1.0, description="Exit threshold")
    z_stop: float = Field(3.5, ge=3.0, le=4.5, description="Relationship-break stop")
    z_window: int = Field(60, ge=40, le=90, description="Mean and deviation window")
    # Configuration (candidate pairs for screening, not recommendations)
    pairs: list[list[str]] = Field(default_factory=list)


def _pair_id(a: str, b: str) -> str:
    return f"{a}/{b}"


class StatPairs(Strategy):
    id = "S4_STAT_PAIRS"
    version = "1.0.0"
    params_model = S4Params
    data_needs = DataSpec(timeframe="day", lookback=320)
    supported_types = frozenset({InstrumentType.STOCK_FUTURE, InstrumentType.INDEX_FUTURE})
    description = "Cointegrated sector pairs, z-score entries, ADF / half-life monitoring"

    # -- pair table (weekly) ---------------------------------------------------------------
    def _logs(self, ctx: Context, a: str, b: str, n: int):
        la = np.log(ctx.bars(a)["close"])
        lb = np.log(ctx.bars(b)["close"])
        j = la.to_frame("a").join(lb.rename("b"), how="inner").dropna()
        return j.iloc[-n:]

    def _select(self, ctx: Context) -> None:
        table = ctx.state.setdefault("pairs", {})
        for a, b in self.params.pairs:
            pid = _pair_id(a, b)
            if not (ctx.has_data(a, FORMATION_N) and ctx.has_data(b, FORMATION_N)):
                continue
            j = self._logs(ctx, a, b, FORMATION_N)
            if len(j) < FORMATION_N:
                continue
            _, beta = ind.ols_beta(j["a"].to_numpy()[-BETA_N:], j["b"].to_numpy()[-BETA_N:])
            spread = (j["a"] - beta * j["b"]).to_numpy()
            t = ind.adf_tstat(spread)
            hl = ind.half_life(spread)
            prev = table.get(pid, {}).get("enabled", False)
            speed_ok = HL_MIN <= hl <= HL_MAX
            enabled = (t < ADF_DISABLE) if prev else (t < ADF_ENABLE and speed_ok)
            table[pid] = {"a": a, "b": b, "beta": beta, "adf": t, "half_life": hl if math.isfinite(hl) else None,
                          "enabled": bool(enabled and beta > 0), "updated": ctx.today.isoformat()}
        ctx.state["last_select"] = ctx.today.isoformat()

    def _spread_z(self, ctx: Context, row: dict) -> dict | None:
        w = self.params.z_window
        j = self._logs(ctx, row["a"], row["b"], w + SPREAD_ER_N + 5)
        if len(j) < w:
            return None
        s = j["a"] - row["beta"] * j["b"]
        mean, std = s.iloc[-w:].mean(), s.iloc[-w:].std()
        if not std or math.isnan(std):
            return None
        er = float(ind.efficiency_ratio(s, SPREAD_ER_N).iloc[-1])
        return {"z": float((s.iloc[-1] - mean) / std), "sigma": float(std), "er": er}

    # -- signals ---------------------------------------------------------------------------
    def on_bar(self, ctx: Context) -> list[Signal]:
        p = self.params
        last = ctx.state.get("last_select")
        if last is None or (ctx.today - date.fromisoformat(last)).days >= RESELECT_DAYS:
            self._select(ctx)
        table = ctx.state.get("pairs", {})
        out: list[Signal] = []
        open_by_pair = {t.meta.get("pair"): t for t in ctx.open_trades()}
        used_sectors = {ctx.view.sector(t.primary.instrument) for t in open_by_pair.values()}

        for pid, row in table.items():
            a, b = row["a"], row["b"]
            if not (ctx.has_data(a) and ctx.has_data(b)):
                continue
            zz = self._spread_z(ctx, row)
            if zz is None:
                continue
            z = zz["z"]
            trade = open_by_pair.get(pid)
            if trade is not None:
                if trade.status != "OPEN":
                    continue
                d = trade.meta["dir"]
                hl = trade.meta.get("half_life") or HL_MAX
                if abs(z) <= p.z_out:
                    out.append(Signal.exit(self.id, trade.trade_id, f"spread reverted (z {z:.2f})"))
                elif d * z <= -p.z_stop:
                    out.append(Signal.exit(self.id, trade.trade_id, f"z-stop: relationship break (z {z:.2f})"))
                elif trade.bars_held >= min(2 * hl, TIME_CAP):
                    out.append(Signal.exit(self.id, trade.trade_id, "time stop: twice the half-life"))
                elif not row["enabled"]:
                    out.append(Signal.exit(self.id, trade.trade_id, "stationarity test failing"))
                continue

            # Entry filters.
            if not row["enabled"] or zz["er"] > SPREAD_ER_MAX or len(open_by_pair) >= MAX_PAIRS:
                continue
            sector = ctx.view.sector(a)
            if sector in used_sectors or not (ctx.can_enter(a) and ctx.can_enter(b)):
                continue
            if z <= -p.z_in:
                d = 1       # long the spread: buy A, sell B
            elif z >= p.z_in:
                d = -1      # short the spread: sell A, buy B
            else:
                continue
            sig = self._size(ctx, pid, row, zz, d)
            if sig is not None:
                out.append(sig)
                open_by_pair[pid] = None  # type: ignore[assignment]  # count toward the cap
                used_sectors.add(sector)
        return out

    def _size(self, ctx: Context, pid: str, row: dict, zz: dict, d: int) -> Signal | None:
        """Risk-based sizing: loss if z runs from here to z_stop <= per-pair risk budget."""
        p = self.params
        a, b, beta = row["a"], row["b"], row["beta"]
        pa, pb = ctx.close(a), ctx.close(b)
        lot_a, lot_b = ctx.view.lot_size(a, ctx.today), ctx.view.lot_size(b, ctx.today)
        notional_a_lot = pa * lot_a
        if abs(zz["z"]) >= p.z_stop:
            return None          # already beyond the relationship-break stop
        # Risk is measured from z_in to z_stop at least: entering close to z_stop must not
        # shrink the stop distance and inflate the size (negative-skew tail).
        stop_distance = max(p.z_stop - abs(zz["z"]), p.z_stop - p.z_in)
        loss_per_lot = stop_distance * zz["sigma"] * notional_a_lot
        budget = ctx.risk_pct * ctx.nav * ctx.permission * ctx.risk_multiplier
        if loss_per_lot <= 0 or budget <= 0:
            return None
        lots_a = int(math.floor(budget / loss_per_lot))
        if lots_a <= 0:
            return None
        target_b = beta * lots_a * notional_a_lot
        lots_b = int(round(target_b / (pb * lot_b)))
        if lots_b <= 0 or abs(lots_b * pb * lot_b - target_b) / target_b > IMBALANCE_MAX:
            return None      # lot ratio leaves too much notional imbalance
        side_a = OrderSide.BUY if d > 0 else OrderSide.SELL
        legs = [Leg(a, side_a, lots_a, ref_price=pa), Leg(b, side_a.opposite, lots_b, ref_price=pb)]
        max_loss = loss_per_lot * lots_a
        edge = (abs(zz["z"]) - p.z_out) * zz["sigma"] * lots_a * notional_a_lot
        return Signal(self.id, SignalAction.ENTER, legs, f"pair {pid} z {zz['z']:.2f}",
                      side=Side.LONG if d > 0 else Side.SHORT, ref_price=pa, max_loss=max_loss,
                      expected_edge=edge,
                      meta={"pair": pid, "dir": d, "beta": beta, "z_entry": zz["z"],
                            "half_life": row.get("half_life"), "sigma": zz["sigma"]})

    # -- alignment -------------------------------------------------------------------------
    def expected_path(self, ctx: Context, trade: Trade) -> Expectation:
        p = self.params
        row = ctx.state.get("pairs", {}).get(trade.meta.get("pair"))
        if row is None:
            return Expectation(off_reasons=["pair no longer in the table"])
        zz = self._spread_z(ctx, row)
        if zz is None:
            return Expectation(drift_reasons=["no fresh data"])
        z0 = trade.meta.get("z_entry", zz["z"])
        hl = trade.meta.get("half_life") or HL_MAX
        drift, off = [], []
        if abs(zz["z"]) > abs(z0) + 0.5:
            drift.append(f"|z| {abs(zz['z']):.2f} above entry {abs(z0):.2f} by 0.5")
        if trade.bars_held > hl:
            drift.append(f"time beyond one half-life ({hl:.1f}d)")
        if trade.meta["dir"] * zz["z"] <= -p.z_stop:
            off.append("z_stop reached")
        if not row["enabled"]:
            off.append("stationarity test failing")
        for k in (row["a"], row["b"]):
            if ctx.is_banned(k) or ctx.blackout(k):
                off.append(f"{k} suspended / in ban or event window")
        legs_ok = all(l.qty == l.target_qty for l in trade.legs.values())
        return Expectation(hold_condition=abs(zz["z"]) <= abs(z0) + 0.5,
                           time_budget_days=int(min(2 * hl, TIME_CAP)), hedge_ok=legs_ok,
                           drift_reasons=drift, off_reasons=off)
