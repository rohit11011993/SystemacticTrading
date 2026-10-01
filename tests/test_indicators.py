import math

import numpy as np
import pandas as pd

from algotrader import indicators as ind


def _series(values):
    return pd.Series(values, index=pd.bdate_range("2024-01-01", periods=len(values)), dtype=float)


def test_efficiency_ratio_trend_and_noise():
    trend = _series(np.arange(100, 130))
    assert ind.efficiency_ratio(trend, 10).iloc[-1] == 1.0
    zigzag = _series([100, 101] * 15)
    assert ind.efficiency_ratio(zigzag, 10).iloc[-1] < 0.15
    flat = _series([100.0] * 20)
    assert ind.efficiency_ratio(flat, 10).iloc[-1] == 0.0
    assert ind.efficiency_ratio(trend, 10).iloc[:10].isna().all()   # no look-ahead / warm-up


def test_kama_tracks_trend_faster_than_noise():
    trend = _series(np.linspace(100, 150, 80))
    k = ind.kama(trend, 10, 2, 30)
    assert abs(k.iloc[-1] - 150) < 3            # follows a clean trend closely
    rng = np.random.default_rng(0)
    noise = _series(100 + rng.normal(0, 1, 200))
    kn = ind.kama(noise, 10, 2, 30)
    assert kn.diff().abs().mean() < noise.diff().abs().mean() / 5   # barely moves in noise


def test_atr_constant_range():
    idx = pd.bdate_range("2024-01-01", periods=40)
    df = pd.DataFrame({"open": 100.0, "high": 102.0, "low": 98.0, "close": 100.0}, index=idx)
    a = ind.atr(df, 20)
    assert a.iloc[:19].isna().all() and math.isclose(a.iloc[-1], 4.0)


def test_adf_and_half_life():
    rng = np.random.default_rng(1)
    n = 500
    ar = np.zeros(n)
    for t in range(1, n):
        ar[t] = 0.85 * ar[t - 1] + rng.normal()
    walk = np.cumsum(rng.normal(size=n))
    assert ind.adf_tstat(ar) < ind.ADF_CRITICAL[0.01]
    assert ind.adf_tstat(walk) > ind.ADF_CRITICAL[0.10]
    hl = ind.half_life(ar)
    assert 2.5 < hl < 7.0                       # theoretical -ln2/ln(0.85) = 4.3


def test_black_scholes_put_call_parity_and_delta():
    s, k, t, v, r = 25000, 25000, 30 / 365, 0.15, 0.065
    c = ind.bs_price(s, k, t, v, "CE", r)
    p = ind.bs_price(s, k, t, v, "PE", r)
    assert math.isclose(c - p, s - k * math.exp(-r * t), rel_tol=1e-6)
    assert 0.5 < ind.bs_delta(s, k, t, v, "CE", r) < 0.6
    assert -0.5 < ind.bs_delta(s, k, t, v, "PE", r) < -0.4


def test_percentile_rank_last_matches_series():
    s = _series(np.random.default_rng(3).normal(size=300))
    assert math.isclose(ind.percentile_rank(s, 252).iloc[-1], ind.percentile_rank_last(s, 252))
