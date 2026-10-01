"""Drift monitor (FR-10.6, FR-16.4, strategy doc s.11 "After going live").

Compares live behaviour - slippage, hit rate, average trade, loss streaks - with the backtest
bands. The kill-switch manager consumes ``StrategyStats`` and trips the strategy when a band is
left.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime

from ..core.models import AlignmentState, Trade


@dataclass
class StrategyStats:
    n_trades: int = 0
    hit_rate: float = 0.0
    avg_win: float = 0.0
    avg_loss: float = 0.0
    avg_win_recent: float = 0.0     # last 20 trades
    avg_loss_recent: float = 0.0
    loss_streak: int = 0            # current consecutive losers
    max_loss_streak: int = 0
    slippage_ratio: float | None = None   # live slippage / assumed slippage
    rejections: int = 0
    open_trades: int = 0
    red_share: float = 0.0
    last_full_loss: datetime | None = None


def compute_stats(closed: list[Trade], open_trades: list[Trade], assumed_slippage: float | None = None,
                  rejections: int = 0, full_loss_frac: float = 0.9) -> StrategyStats:
    """Statistics over closed trades (net PnL) and the alignment of open trades.

    ``assumed_slippage`` is the backtest's total slippage assumption in rupees for the same
    trades; when given, ``slippage_ratio`` = realised / assumed.
    """
    s = StrategyStats(rejections=rejections, open_trades=len(open_trades))
    closed = sorted(closed, key=lambda t: t.exit_ts or datetime.min)
    pnls = [t.realized() - t.charges for t in closed]
    s.n_trades = len(pnls)
    if pnls:
        wins = [p for p in pnls if p > 0]
        losses = [p for p in pnls if p <= 0]
        s.hit_rate = len(wins) / len(pnls)
        s.avg_win = sum(wins) / len(wins) if wins else 0.0
        s.avg_loss = -sum(losses) / len(losses) if losses else 0.0
        recent = pnls[-20:]
        rw = [p for p in recent if p > 0]
        rl = [p for p in recent if p <= 0]
        s.avg_win_recent = sum(rw) / len(rw) if rw else 0.0
        s.avg_loss_recent = -sum(rl) / len(rl) if rl else 0.0
        streak = 0
        for p in pnls:
            streak = streak + 1 if p <= 0 else 0
            s.max_loss_streak = max(s.max_loss_streak, streak)
        s.loss_streak = streak
    if assumed_slippage:
        realised = sum(t.slippage for t in closed)
        s.slippage_ratio = realised / assumed_slippage if assumed_slippage > 0 else None
    for t, p in zip(closed, pnls):
        cap = t.meta.get("max_loss")
        if cap and p <= -full_loss_frac * cap:
            s.last_full_loss = t.exit_ts
    if open_trades:
        reds = sum(1 for t in open_trades if t.alignment and t.alignment[-1][1] == AlignmentState.OFF_THESIS.value)
        s.red_share = reds / len(open_trades)
    return s
