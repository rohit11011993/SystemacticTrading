"""Shared position sizing (strategy doc s.4).

    N = floor( r x NAV / (k x ATR_n x LotSize) )

``k x ATR_n`` is simply the stop distance, so the general form is
``floor(r x NAV / (|entry - stop| x LotSize))``. If N rounds to zero the instrument is too large
for the account and the trade is skipped, never forced to one lot.

A portfolio overlay scales *all* new positions down when realised portfolio volatility exceeds
its target, within configured bounds.
"""

from __future__ import annotations

import math

import numpy as np


def lots_for_risk(nav: float, risk_pct: float, entry: float, stop: float, lot_size: int,
                  multiplier: float = 1.0) -> int:
    """Lots such that a stop-out loses at most ``risk_pct`` x NAV x multiplier.

    ``risk_pct`` is a fraction (0.005 = 0.5%).
    """
    per_lot = abs(entry - stop) * lot_size
    budget = risk_pct * nav * multiplier
    if per_lot <= 0 or budget <= 0:
        return 0
    return int(math.floor(budget / per_lot))


class VolTargetOverlay:
    """Scale factor in [min_scale, 1] from realised portfolio volatility vs target."""

    def __init__(self, target_annual: float | None, min_scale: float = 0.5, lookback: int = 60):
        self.target = target_annual
        self.min_scale = min_scale
        self.lookback = lookback

    def scale(self, equity_curve: list[float]) -> float:
        if not self.target or len(equity_curve) < self.lookback // 2:
            return 1.0
        eq = np.asarray(equity_curve[-(self.lookback + 1):], dtype=float)
        rets = np.diff(eq) / eq[:-1]
        if len(rets) < 10:
            return 1.0
        realised = float(np.std(rets, ddof=1) * math.sqrt(252))
        if realised <= self.target or realised == 0:
            return 1.0
        return max(self.min_scale, self.target / realised)
