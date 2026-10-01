"""Active-trade blotter rows (PRD s.8 column groups) and a plain-text renderer.

The PySide6 desktop UI (PRD s.12) renders the same rows; the text renderer is used by the CLI
``status`` command and in headless mode.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any

from ..core.models import Trade


def blotter_rows(trades: list[Trade], prices: dict[str, float], nav: float, now: datetime) -> list[dict[str, Any]]:
    rows = []
    for t in trades:
        leg = t.primary
        last = prices.get(leg.symbol)
        pnl = t.pnl(prices)
        rows.append({
            # Identity
            "trade_id": t.trade_id, "strategy": t.strategy_id, "instrument": leg.instrument,
            "side": t.side.name, "qty": sum(abs(l.qty) for l in t.legs.values()),
            "legs": len(t.legs), "entry_time": t.entry_ts,
            # Price
            "entry": t.entry_price, "last": last, "stop": t.stop_price,
            # PnL
            "pnl_net": pnl, "pnl_pct_nav": pnl / nav * 100 if nav else 0.0,
            "r_multiple": t.r_multiple(prices), "charges": t.charges,
            # Risk
            "initial_risk": t.initial_risk,
            "dist_to_stop": (abs(last - t.stop_price) if last is not None and t.stop_price is not None else None),
            # Behaviour
            "mfe": t.mfe, "mae": t.mae, "days": t.bars_held,
            "alignment": t.alignment[-1][1] if t.alignment else "n/a",
            "why": "; ".join(t.alignment[-1][2]) if t.alignment else "",
            "data_age_s": None,
        })
    return rows


def render_text(rows: list[dict[str, Any]]) -> str:
    if not rows:
        return "(no open trades)"
    head = f"{'strategy':<24}{'instrument':<16}{'side':<6}{'qty':>7}{'entry':>11}{'last':>11}{'stop':>11}" \
           f"{'PnL':>12}{'R':>7}{'days':>5}  alignment"
    lines = [head, "-" * len(head)]
    for r in rows:
        fmt = lambda v: f"{v:>11.2f}" if isinstance(v, (int, float)) else f"{'-':>11}"  # noqa: E731
        lines.append(f"{r['strategy']:<24}{r['instrument']:<16}{r['side']:<6}{r['qty']:>7}{fmt(r['entry'])}"
                     f"{fmt(r['last'])}{fmt(r['stop'])}{r['pnl_net']:>12,.0f}{r['r_multiple']:>7.2f}"
                     f"{r['days']:>5}  {r['alignment']} {r['why']}")
    return "\n".join(lines)
