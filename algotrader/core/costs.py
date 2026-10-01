"""Versioned cost table (PRD s.3, strategy doc s.4).

All taxes and charges live in ``config/costs.yaml`` keyed by an effective date, so a Budget
change is a data update, never a code change. Rates are stored in **percent**.

From 1 April 2026: STT 0.05% on futures sales, 0.15% of premium on option sales and 0.15% of
intrinsic value on exercised options (BasisPoint report cited in both documents). Verify every
rate against the broker's current charge sheet before release.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date
from pathlib import Path

import yaml
from pydantic import BaseModel, Field

from .models import OrderSide


class CostProfile(BaseModel):
    brokerage_flat: float = 20.0       # rupees per executed order (cap)
    brokerage_pct: float = 0.03        # % of turnover; brokerage = min(flat, pct x turnover)
    stt_buy_pct: float = 0.0
    stt_sell_pct: float = 0.0          # futures 0.05; options 0.15 on premium; CTT for MCX
    stt_exercise_pct: float = 0.0      # options: % of intrinsic value on exercise
    exchange_pct: float = 0.0          # exchange transaction charges
    sebi_per_crore: float = 10.0       # SEBI turnover fee, rupees per crore
    stamp_buy_pct: float = 0.0         # stamp duty, buy side only
    gst_pct: float = 18.0              # on brokerage + exchange + SEBI fees
    slippage_ticks: float = 1.0        # per side, in ticks
    slippage_pct: float = 0.0          # per side, % of price (added to ticks)


class CostTableVersion(BaseModel):
    effective: date
    profiles: dict[str, CostProfile] = Field(default_factory=dict)


@dataclass
class ChargeBreakdown:
    brokerage: float = 0.0
    stt: float = 0.0
    exchange: float = 0.0
    sebi: float = 0.0
    stamp: float = 0.0
    gst: float = 0.0

    @property
    def total(self) -> float:
        return self.brokerage + self.stt + self.exchange + self.sebi + self.stamp + self.gst


class CostTable:
    """Looks up the profile in force on a given date and prices orders / round trips."""

    def __init__(self, versions: list[CostTableVersion]):
        if not versions:
            raise ValueError("cost table needs at least one version")
        self.versions = sorted(versions, key=lambda v: v.effective)

    @classmethod
    def from_yaml(cls, path: str | Path) -> "CostTable":
        raw = yaml.safe_load(Path(path).read_text(encoding="utf-8")) or {}
        return cls([CostTableVersion(**v) for v in raw.get("cost_tables", [])])

    def pinned(self, on: date | None = None) -> "CostTable":
        """A table that always prices at the rates in force on ``on`` (latest if None).

        Used to report every backtest at today's rates as well (strategy doc s.11).
        """
        chosen = self.versions[-1]
        if on is not None:
            for v in self.versions:
                if v.effective <= on:
                    chosen = v
        return CostTable([chosen.model_copy(update={"effective": date.min})])

    def scaled(self, mult: float) -> "CostTable":
        """Every charge and slippage multiplied by ``mult`` (cost-stress test, strategy doc s.11)."""
        out = []
        for v in self.versions:
            profiles = {}
            for name, p in v.profiles.items():
                d = p.model_dump()
                for k in d:
                    if k != "gst_pct":     # GST is a rate on other charges; it scales with them
                        d[k] *= mult
                profiles[name] = CostProfile(**d)
            out.append(CostTableVersion(effective=v.effective, profiles=profiles))
        return CostTable(out)

    def profile(self, name: str, on: date | None = None) -> CostProfile:
        """Profile in force on ``on``. Results must also be reported at today's rates."""
        chosen = self.versions[-1]
        if on is not None:
            for v in self.versions:
                if v.effective <= on:
                    chosen = v
        if name not in chosen.profiles:
            raise KeyError(f"cost profile '{name}' missing in table effective {chosen.effective}")
        return chosen.profiles[name]

    # -- pricing ---------------------------------------------------------------------------
    @staticmethod
    def order_charges(p: CostProfile, side: OrderSide, price: float, qty: int) -> ChargeBreakdown:
        """Statutory charges and brokerage for one executed order.

        ``price`` is the premium for options, so STT on option sales is charged on premium.
        """
        turnover = abs(price * qty)
        c = ChargeBreakdown()
        c.brokerage = min(p.brokerage_flat, turnover * p.brokerage_pct / 100.0)
        c.stt = turnover * (p.stt_sell_pct if side is OrderSide.SELL else p.stt_buy_pct) / 100.0
        c.exchange = turnover * p.exchange_pct / 100.0
        c.sebi = turnover * p.sebi_per_crore / 1e7
        c.stamp = turnover * p.stamp_buy_pct / 100.0 if side is OrderSide.BUY else 0.0
        c.gst = (c.brokerage + c.exchange + c.sebi) * p.gst_pct / 100.0
        return c

    @staticmethod
    def slippage(p: CostProfile, price: float, qty: int, tick: float) -> float:
        """Estimated slippage in rupees for one side."""
        per_unit = p.slippage_ticks * tick + abs(price) * p.slippage_pct / 100.0
        return per_unit * abs(qty)

    def round_trip(self, profile: str, price: float, qty: int, tick: float,
                   on: date | None = None, include_slippage: bool = True) -> float:
        """Full round-trip cost (buy + sell) including slippage, in rupees."""
        p = self.profile(profile, on)
        total = (self.order_charges(p, OrderSide.BUY, price, qty).total
                 + self.order_charges(p, OrderSide.SELL, price, qty).total)
        if include_slippage:
            total += 2 * self.slippage(p, price, qty, tick)
        return total

    def exercise_stt(self, profile: str, intrinsic: float, qty: int, on: date | None = None) -> float:
        """STT on an exercised in-the-money option (the S5 'expiry cost trap')."""
        return abs(intrinsic * qty) * self.profile(profile, on).stt_exercise_pct / 100.0


def cost_viable(round_trip_cost: float, avg_gross_profit: float | None, atr_rupees: float | None,
                max_cost_to_profit: float = 0.10, max_cost_to_atr: float = 0.15) -> tuple[bool, str]:
    """Cost-viability gate from strategy doc s.4.

    A strategy-instrument pair passes only if round-trip cost is below 10% of average gross
    profit per trade *and* below 15% of one daily ATR (both configurable). When the average
    profit is unknown (live pre-trade check) only the ATR test is applied.
    """
    if atr_rupees is not None and atr_rupees > 0 and round_trip_cost > max_cost_to_atr * atr_rupees:
        return False, f"cost {round_trip_cost:.0f} > {max_cost_to_atr:.0%} of ATR {atr_rupees:.0f}"
    if avg_gross_profit is not None:
        if avg_gross_profit <= 0 or round_trip_cost > max_cost_to_profit * avg_gross_profit:
            return False, f"cost {round_trip_cost:.0f} vs avg gross profit {avg_gross_profit:.0f}"
    return True, "ok"
