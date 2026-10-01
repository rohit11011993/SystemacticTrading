"""Alignment indicator: "is the move in line with the strategy?" (PRD s.8, R5).

The strategy states what a healthy trade looks like through ``expected_path``; this module
compares reality against it with the PRD's seven checks and returns one of three states,
always together with the failing checks (colour is never the only carrier, FR-12.4):

* IN_LINE (green)     all checks pass
* DRIFTING (amber)    one or more checks outside tolerance, no invalidation
* OFF_THESIS (red)    invalidation crossed, or signal validity fails together with a path breach
"""

from __future__ import annotations

from dataclasses import dataclass, field

from ..core.models import AlignmentState, Expectation, Trade


@dataclass
class AlignmentResult:
    state: AlignmentState
    failing: list[str] = field(default_factory=list)

    @property
    def label(self) -> str:
        icon = {AlignmentState.IN_LINE: "[OK]", AlignmentState.DRIFTING: "[!]",
                AlignmentState.OFF_THESIS: "[X]"}[self.state]
        return f"{icon} {self.state.value}"


def evaluate_alignment(trade: Trade, exp: Expectation, r_multiple: float, days_in_trade: int) -> AlignmentResult:
    drift: list[str] = []
    off: list[str] = []

    # 1. Path: R-multiple inside the expected corridor for the time elapsed.
    lo, hi = exp.corridor
    path_breach = (lo is not None and r_multiple < lo) or (hi is not None and r_multiple > hi)
    if path_breach:
        drift.append(f"path: R {r_multiple:.2f} outside corridor ({lo}, {hi})")
    # 2. Signal validity: the entry logic re-evaluated now.
    if not exp.hold_condition:
        drift.append("signal validity: hold condition failed")
    # 3. Invalidation level.
    if exp.invalidation_crossed:
        off.append(f"invalidation: price crossed {exp.invalidation_level}")
    # 4. Volatility versus entry.
    if exp.vol_ratio is not None and exp.vol_tolerance is not None and exp.vol_ratio > exp.vol_tolerance:
        drift.append(f"volatility: {exp.vol_ratio:.2f}x entry (tolerance {exp.vol_tolerance}x)")
    # 5. Excursion versus backtest winners.
    if exp.mae_limit_r is not None and trade.initial_risk > 0:
        mae_r = trade.mae / trade.initial_risk
        if mae_r < -abs(exp.mae_limit_r):
            drift.append(f"excursion: MAE {mae_r:.2f}R beyond winners' range")
    # 6. Time in trade.
    if exp.time_budget_days is not None and days_in_trade > exp.time_budget_days:
        drift.append(f"time: {days_in_trade}d > budget {exp.time_budget_days}d")
    # 7. Hedge integrity.
    if not exp.hedge_ok:
        drift.append("hedge integrity outside tolerance")

    drift += exp.drift_reasons
    off += exp.off_reasons
    if not exp.hold_condition and path_breach:
        off.append("signal validity failed together with a path breach")

    if off:
        return AlignmentResult(AlignmentState.OFF_THESIS, off + drift)
    if drift:
        return AlignmentResult(AlignmentState.DRIFTING, drift)
    return AlignmentResult(AlignmentState.IN_LINE, [])
