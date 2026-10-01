"""Backtest runner and performance report.

The backtest drives the *same* engine, strategy code, risk gateway and kill switches as paper
and live trading (FR-16.1, NFR-6), with the paper broker filling orders bar by bar.

Every result is net of costs from the versioned cost table. Run it twice - once with the
historical table and once with ``pin_costs_today`` - because today's rates (STT on futures
sales 0.05% since 1 April 2026) are what the strategy will actually face (strategy doc s.11).
"""

from __future__ import annotations

import json
import math
from dataclasses import dataclass, field
from datetime import date
from pathlib import Path
from typing import Any

import numpy as np
import pandas as pd

from .engine import TradingEngine


@dataclass
class BacktestResult:
    equity: pd.Series
    trades: pd.DataFrame
    summary: dict[str, Any]
    regime: pd.DataFrame
    alerts: list[str] = field(default_factory=list)


def run_backtest(engine: TradingEngine, start: date | None = None, end: date | None = None,
                 progress: bool = False) -> BacktestResult:
    days = engine.data.trading_days(engine.cfg.portfolio.regime.market_series, start, end)
    for i, ts in enumerate(days):
        engine.run_day(ts)
        if progress and i % 250 == 0:
            print(f"  {ts.date()}  equity {engine.equity_curve[-1][1]:,.0f}  ladder {engine.ks.ladder_state}")
    equity = pd.Series({pd.Timestamp(d): e for d, e in engine.equity_curve}, name="equity")
    trades = trades_frame(engine)
    summary = summarize(equity, trades, engine.capital)
    summary["kill_switch_trips"] = [f"{t.level.value}:{t.scope} {t.action.name} - {t.reason}"
                                    for t in engine.ks.active()]
    summary["ladder_state"] = engine.ks.ladder_state
    regime = pd.DataFrame(engine.regime_log)
    return BacktestResult(equity, trades, summary, regime, list(dict.fromkeys(engine.alerts)))


def trades_frame(engine: TradingEngine) -> pd.DataFrame:
    prices = engine.prices()
    rows = []
    for t in engine.book.trades.values():
        if t.status == "CANCELLED":
            continue
        rows.append({
            "trade_id": t.trade_id, "strategy": t.strategy_id, "role": t.meta.get("role", "trade"),
            "instruments": "+".join(l.symbol for l in t.legs.values()), "side": t.side.name,
            "status": t.status, "entry_ts": t.entry_ts, "exit_ts": t.exit_ts, "entry_price": t.entry_price,
            "initial_risk": t.initial_risk, "pnl_gross": t.pnl(prices, net=False), "charges": t.charges,
            "pnl_net": t.pnl(prices), "r_multiple": t.r_multiple(prices), "bars_held": t.bars_held,
            "mfe": t.mfe, "mae": t.mae, "slippage": t.slippage, "reason": t.reason,
            "exit_reason": t.exit_reason,
            "last_alignment": t.alignment[-1][1] if t.alignment else None})
    return pd.DataFrame(rows)


def max_drawdown(equity: pd.Series) -> float:
    if equity.empty:
        return 0.0
    peak = equity.cummax()
    return float((equity / peak - 1.0).min())


def summarize(equity: pd.Series, trades: pd.DataFrame, capital: float) -> dict[str, Any]:
    out: dict[str, Any] = {"start": str(equity.index[0].date()) if len(equity) else None,
                           "end": str(equity.index[-1].date()) if len(equity) else None,
                           "capital": capital}
    if len(equity) < 2:
        return out
    rets = equity.pct_change().dropna()
    years = max((equity.index[-1] - equity.index[0]).days / 365.25, 1e-9)
    final = float(equity.iloc[-1])
    out.update({
        "final_equity": final,
        "total_return_pct": (final / capital - 1) * 100,
        "cagr_pct": ((final / capital) ** (1 / years) - 1) * 100 if final > 0 else -100.0,
        "ann_vol_pct": float(rets.std() * math.sqrt(252) * 100),
        "sharpe": float(rets.mean() / rets.std() * math.sqrt(252)) if rets.std() > 0 else 0.0,
        "max_drawdown_pct": max_drawdown(equity) * 100,
    })
    if not trades.empty:
        closed = trades[(trades["status"] == "CLOSED") & (trades["role"] != "hedge")]
        out["trades_closed"] = int(len(closed))
        out["hit_rate"] = float((closed["pnl_net"] > 0).mean()) if len(closed) else None
        out["avg_r"] = float(closed["r_multiple"].mean()) if len(closed) else None
        out["total_charges"] = float(trades["charges"].sum())
        per = {}
        for sid, g in trades.groupby("strategy"):
            c = g[(g["status"] == "CLOSED") & (g["role"] != "hedge")]
            per[sid] = {"trades": int(len(c)), "pnl_net": float(g["pnl_net"].sum()),
                        "charges": float(g["charges"].sum()),
                        "hit_rate": float((c["pnl_net"] > 0).mean()) if len(c) else None,
                        "avg_r": float(c["r_multiple"].mean()) if len(c) else None}
        out["by_strategy"] = per
    return out


def write_report(result: BacktestResult, out_dir: str | Path, tag: str = "backtest") -> list[Path]:
    out = Path(out_dir)
    out.mkdir(parents=True, exist_ok=True)
    paths = [out / f"{tag}_equity.csv", out / f"{tag}_trades.csv", out / f"{tag}_summary.json",
             out / f"{tag}_regime.csv"]
    result.equity.to_csv(paths[0])
    result.trades.to_csv(paths[1], index=False)
    paths[2].write_text(json.dumps(result.summary, indent=2, default=str), encoding="utf-8")
    result.regime.to_csv(paths[3], index=False)
    return paths


def format_summary(s: dict[str, Any]) -> str:
    def f(v: Any, fmt: str = "{:,.2f}") -> str:
        return "-" if v is None or (isinstance(v, float) and np.isnan(v)) else fmt.format(v)
    lines = [
        f"Period            {s.get('start')} -> {s.get('end')}",
        f"Final equity      {f(s.get('final_equity'), '{:,.0f}')}  (capital {s.get('capital'):,.0f})",
        f"Total return      {f(s.get('total_return_pct'))}%   CAGR {f(s.get('cagr_pct'))}%",
        f"Volatility        {f(s.get('ann_vol_pct'))}%   Sharpe {f(s.get('sharpe'))}",
        f"Max drawdown      {f(s.get('max_drawdown_pct'))}%   ladder state {s.get('ladder_state')}",
        f"Closed trades     {s.get('trades_closed', 0)}   hit rate {f(s.get('hit_rate'), '{:.1%}')}"
        f"   avg R {f(s.get('avg_r'))}",
        f"Charges paid      {f(s.get('total_charges'), '{:,.0f}')}",
        "",
        f"{'strategy':<26}{'trades':>7}{'net PnL':>14}{'charges':>12}{'hit':>8}{'avg R':>8}",
    ]
    for sid, p in (s.get("by_strategy") or {}).items():
        lines.append(f"{sid:<26}{p['trades']:>7}{p['pnl_net']:>14,.0f}{p['charges']:>12,.0f}"
                     f"{f(p['hit_rate'], '{:.0%}'):>8}{f(p['avg_r']):>8}")
    if s.get("kill_switch_trips"):
        lines += ["", "Active kill-switch trips at end:"] + [f"  {t}" for t in s["kill_switch_trips"]]
    return "\n".join(lines)
