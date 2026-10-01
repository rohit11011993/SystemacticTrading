"""Research and validation helpers (strategy doc s.11).

A strategy is accepted only if it survives a fixed sequence of tests designed to catch
overfitting and cost illusions. This module implements the tests that operate on backtest
output, plus a driver for parameter-plateau and cost-stress runs:

* ``monte_carlo_drawdown``   - trade order resampled; 95th-percentile drawdown vs risk budget
* ``top_trade_removal``      - result with the best 5% of trades removed
* ``deflated_sharpe``        - Sharpe ratio adjusted for the number of trials (multiple testing)
* ``plateau_grid``           - each tunable parameter varied +/-20% (stable plateau, not a peak)
* ``walk_forward_windows``   - rolling design / out-of-sample windows
* ``cost_viability``         - cost gate from strategy doc s.4, per strategy-instrument pair

Rules the protocol enforces procedurally (not in code): write the hypothesis first, fix the
parameter list before testing, open the hold-out once, pool parameters across instruments.
"""

from __future__ import annotations

import math
from datetime import date
from typing import Any, Callable, Iterable

import numpy as np
import pandas as pd

from .core.costs import cost_viable


def monte_carlo_drawdown(trade_pnls: Iterable[float], capital: float, n: int = 5000,
                         seed: int = 1) -> dict[str, float]:
    """Resample trade order (with replacement); report drawdown percentiles in % of capital."""
    pnl = np.asarray(list(trade_pnls), dtype=float)
    if len(pnl) == 0:
        return {"p50": 0.0, "p95": 0.0, "p99": 0.0}
    rng = np.random.default_rng(seed)
    dds = np.empty(n)
    for i in range(n):
        path = capital + np.cumsum(rng.choice(pnl, size=len(pnl), replace=True))
        peak = np.maximum.accumulate(np.concatenate([[capital], path]))[1:]
        dds[i] = ((path - peak) / peak).min() * 100
    return {"p50": float(np.percentile(dds, 50)), "p95": float(np.percentile(dds, 5)),
            "p99": float(np.percentile(dds, 1))}


def top_trade_removal(trade_pnls: Iterable[float], frac: float = 0.05) -> float:
    """Net result with the best ``frac`` of trades removed."""
    pnl = sorted(trade_pnls, reverse=True)
    k = int(math.ceil(len(pnl) * frac))
    return float(sum(pnl[k:]))


def _norm_ppf(p: float) -> float:
    """Inverse standard normal CDF (Acklam's rational approximation)."""
    a = [-3.969683028665376e+01, 2.209460984245205e+02, -2.759285104469687e+02,
         1.383577518672690e+02, -3.066479806614716e+01, 2.506628277459239e+00]
    b = [-5.447609879822406e+01, 1.615858368580409e+02, -1.556989798598866e+02,
         6.680131188771972e+01, -1.328068155288572e+01]
    c = [-7.784894002430293e-03, -3.223964580411365e-01, -2.400758277161838e+00,
         -2.549732539343734e+00, 4.374664141464968e+00, 2.938163982698783e+00]
    d = [7.784695709041462e-03, 3.224671290700398e-01, 2.445134137142996e+00, 3.754408661907416e+00]
    lo, hi = 0.02425, 1 - 0.02425
    if p < lo:
        q = math.sqrt(-2 * math.log(p))
        return (((((c[0] * q + c[1]) * q + c[2]) * q + c[3]) * q + c[4]) * q + c[5]) / \
               ((((d[0] * q + d[1]) * q + d[2]) * q + d[3]) * q + 1)
    if p > hi:
        return -_norm_ppf(1 - p)
    q = p - 0.5
    r = q * q
    return (((((a[0] * r + a[1]) * r + a[2]) * r + a[3]) * r + a[4]) * r + a[5]) * q / \
           (((((b[0] * r + b[1]) * r + b[2]) * r + b[3]) * r + b[4]) * r + 1)


def deflated_sharpe(returns: pd.Series, n_trials: int, trials_sharpe_std: float = 0.5) -> dict[str, float]:
    """Deflated Sharpe ratio (Bailey & Lopez de Prado): probability that the observed Sharpe
    exceeds the maximum expected from ``n_trials`` unskilled variants.

    Works in per-period units; ``trials_sharpe_std`` is the dispersion of annualised Sharpe
    ratios across the variants tried (record every variant, strategy doc s.11 rule 5).
    """
    r = returns.dropna().to_numpy()
    T = len(r)
    if T < 30 or r.std() == 0:
        return {"sharpe": 0.0, "expected_max": 0.0, "dsr": 0.0}
    sr = r.mean() / r.std(ddof=1)
    skew = float(((r - r.mean()) ** 3).mean() / r.std() ** 3)
    kurt = float(((r - r.mean()) ** 4).mean() / r.std() ** 4)
    gamma = 0.5772156649
    sd = trials_sharpe_std / math.sqrt(252)
    n = max(n_trials, 2)
    sr0 = sd * ((1 - gamma) * _norm_ppf(1 - 1 / n) + gamma * _norm_ppf(1 - 1 / (n * math.e)))
    denom = math.sqrt(max(1e-12, 1 - skew * sr + (kurt - 1) / 4 * sr * sr))
    z = (sr - sr0) * math.sqrt(T - 1) / denom
    dsr = 0.5 * (1 + math.erf(z / math.sqrt(2)))
    return {"sharpe": sr * math.sqrt(252), "expected_max": sr0 * math.sqrt(252), "dsr": dsr}


def plateau_grid(base: dict[str, float], bounds: dict[str, tuple[float, float]] | None = None,
                 pct: float = 0.20) -> list[dict[str, float]]:
    """One-at-a-time +/-pct variations of each tunable parameter, clipped to its test range."""
    out = []
    for k, v in base.items():
        for m in (1 - pct, 1 + pct):
            nv = v * m
            if bounds and k in bounds:
                nv = min(max(nv, bounds[k][0]), bounds[k][1])
            if isinstance(v, int):
                nv = int(round(nv))
            if nv != v:
                out.append({**base, k: nv})
    return out


def run_plateau(run: Callable[[dict[str, float]], float], base: dict[str, float],
                bounds: dict[str, tuple[float, float]] | None = None, band: float = 0.5) -> dict[str, Any]:
    """Pass if every variant stays positive and within ``band`` (fraction) of the baseline."""
    baseline = run(base)
    results = [(v, run(v)) for v in plateau_grid(base, bounds)]
    ok = all(r > 0 and abs(r - baseline) <= band * abs(baseline) for _, r in results) and baseline > 0
    return {"baseline": baseline, "variants": results, "pass": ok}


def walk_forward_windows(start: date, end: date, design_years: int = 3, test_months: int = 6
                         ) -> list[tuple[date, date, date, date]]:
    """Rolling (design_start, design_end, test_start, test_end) windows."""
    windows = []
    ds = pd.Timestamp(start)
    while True:
        de = ds + pd.DateOffset(years=design_years) - pd.Timedelta(days=1)
        ts = de + pd.Timedelta(days=1)
        te = ts + pd.DateOffset(months=test_months) - pd.Timedelta(days=1)
        if te > pd.Timestamp(end):
            break
        windows.append((ds.date(), de.date(), ts.date(), te.date()))
        ds = ds + pd.DateOffset(months=test_months)
    return windows


def cost_viability(trades: pd.DataFrame, round_trip_cost_by_trade: pd.Series | None = None,
                   max_cost_to_profit: float = 0.10) -> pd.DataFrame:
    """Per strategy-instrument pair: average gross profit per trade versus average charges."""
    closed = trades[(trades["status"] == "CLOSED") & (trades["role"] != "hedge")]
    rows = []
    for (sid, inst), g in closed.groupby(["strategy", "instruments"]):
        avg_gross = float(g["pnl_gross"].mean())
        avg_cost = float(g["charges"].mean() if round_trip_cost_by_trade is None
                         else round_trip_cost_by_trade.loc[g.index].mean())
        ok, why = cost_viable(avg_cost, avg_gross, None, max_cost_to_profit)
        rows.append({"strategy": sid, "instrument": inst, "trades": len(g), "avg_gross": avg_gross,
                     "avg_cost": avg_cost, "viable": ok, "detail": why})
    return pd.DataFrame(rows)
