"""Synthetic daily data for demos, tests and plumbing checks.

The generator produces regime-switching series (trending, ranging, volatile spells) so every
strategy has something to react to. It is **not** a substitute for real data: results on
synthetic data say nothing about edge, only that the system runs end to end.

Series produced (keys match ``config/instruments.yaml``):
  NIFTY_FUT, BANKNIFTY_FUT, INDIAVIX, GOLD_FUT, SILVER_FUT, CRUDE_FUT, USDINR_FUT,
  HDFCBANK_FUT, ICICIBANK_FUT, KOTAKBANK_FUT, AXISBANK_FUT, INFY_FUT, TCS_FUT
"""

from __future__ import annotations

from pathlib import Path

import numpy as np
import pandas as pd


def _regime_drift(n: int, rng: np.random.Generator, ann_drift: float = 0.25,
                  mean_len: int = 60) -> np.ndarray:
    """Piecewise-constant daily drift: up-trend, down-trend or flat spells."""
    out = np.zeros(n)
    i = 0
    while i < n:
        length = int(rng.exponential(mean_len)) + 10
        state = rng.choice([1.0, -1.0, 0.0], p=[0.35, 0.25, 0.40])
        out[i:i + length] = state * ann_drift / 252
        i += length
    return out


def _vol_path(n: int, rng: np.random.Generator, base: float = 0.15) -> np.ndarray:
    """Mean-reverting annualised volatility with occasional spikes."""
    v = np.empty(n)
    v[0] = base
    for t in range(1, n):
        shock = rng.normal(0, 0.01) + (0.12 if rng.random() < 0.006 else 0.0)
        v[t] = max(0.07, v[t - 1] + 0.04 * (base - v[t - 1]) + shock)
    return v


def _ohlc(close: np.ndarray, vol_daily: np.ndarray, rng: np.random.Generator,
          volume: float) -> pd.DataFrame:
    """Build open/high/low around a close path."""
    prev = np.concatenate([[close[0]], close[:-1]])
    gap = rng.normal(0, 0.25, len(close)) * vol_daily
    open_ = prev * np.exp(gap)
    rng_up = np.abs(rng.normal(0, 0.6, len(close))) * vol_daily
    rng_dn = np.abs(rng.normal(0, 0.6, len(close))) * vol_daily
    high = np.maximum(open_, close) * np.exp(rng_up)
    low = np.minimum(open_, close) * np.exp(-rng_dn)
    vol = volume * np.exp(rng.normal(0, 0.3, len(close)))
    return pd.DataFrame({"open": open_, "high": high, "low": low, "close": close, "volume": vol})


def generate(start: str = "2019-01-01", end: str = "2026-09-30", seed: int = 7) -> dict[str, pd.DataFrame]:
    rng = np.random.default_rng(seed)
    days = pd.bdate_range(start, end)
    n = len(days)
    frames: dict[str, pd.DataFrame] = {}

    # Market factor (Nifty) with regime-switching drift and stochastic volatility.
    mvol = _vol_path(n, rng, 0.15)
    mret = _regime_drift(n, rng, 0.30) + rng.normal(0, 1, n) * mvol / np.sqrt(252)
    nifty = 18000 * np.exp(np.cumsum(mret))
    frames["NIFTY_FUT"] = _ohlc(nifty, mvol / np.sqrt(252), rng, 2e5)

    # India VIX tracks market volatility with noise, plus spikes on sell-offs.
    vix = mvol * 100 * np.exp(rng.normal(0, 0.05, n)) + np.clip(-mret * 400, 0, 8)
    vix = pd.Series(vix).rolling(3, min_periods=1).mean().to_numpy()
    frames["INDIAVIX"] = _ohlc(vix, np.full(n, 0.03), rng, 0)

    bank_ret = 1.15 * mret + rng.normal(0, 0.006, n)
    frames["BANKNIFTY_FUT"] = _ohlc(42000 * np.exp(np.cumsum(bank_ret)), mvol * 1.2 / np.sqrt(252), rng, 1e5)

    # Commodities and currency: independent trend processes.
    for key, p0, base_vol, drift, vol_lots in [("GOLD_FUT", 60000, 0.12, 0.20, 2e4),
                                               ("SILVER_FUT", 70000, 0.22, 0.25, 1e4),
                                               ("CRUDE_FUT", 6000, 0.30, 0.35, 5e4),
                                               ("USDINR_FUT", 82, 0.04, 0.05, 5e5)]:
        v = _vol_path(n, rng, base_vol)
        r = _regime_drift(n, rng, drift, 80) + rng.normal(0, 1, n) * v / np.sqrt(252)
        frames[key] = _ohlc(p0 * np.exp(np.cumsum(r)), v / np.sqrt(252), rng, vol_lots)

    # Stocks: beta to the market + sector factor + idiosyncratic noise with short-term
    # reversal (gives S3 something to find) and cointegrated pairs within each sector (S4).
    sectors = {"bank": (["HDFCBANK_FUT", "ICICIBANK_FUT", "KOTAKBANK_FUT", "AXISBANK_FUT"],
                        [1600, 1000, 1800, 1100], 1.1),
               "it": (["INFY_FUT", "TCS_FUT"], [1500, 3500], 0.8)}
    for _, (keys, p0s, beta) in sectors.items():
        sector_f = rng.normal(0, 0.007, n)
        common = np.cumsum(beta * mret + sector_f)
        for key, p0 in zip(keys, p0s):
            # Idiosyncratic component: AR(1) in levels (mean-reverting -> pairs cointegrate)
            # plus short-lived shocks that partly reverse within days.
            ou = np.zeros(n)
            for t in range(1, n):
                ou[t] = 0.93 * ou[t - 1] + rng.normal(0, 0.009)
            shocks = rng.normal(0, 0.008, n) * (rng.random(n) < 0.08) * 4
            reversal = shocks - 0.5 * np.concatenate([[0, 0], shocks[:-2]])
            logp = np.log(p0) + common + ou + np.cumsum(reversal)
            frames[key] = _ohlc(np.exp(logp), np.full(n, 0.015), rng, 3e4)

    for df in frames.values():
        df.index = days
        df.index.name = "date"
    return frames


def write_csv(frames: dict[str, pd.DataFrame], out_dir: str | Path) -> list[Path]:
    out = Path(out_dir)
    out.mkdir(parents=True, exist_ok=True)
    paths = []
    for k, df in frames.items():
        p = out / f"{k}.csv"
        df.round(4).to_csv(p)
        paths.append(p)
    return paths
