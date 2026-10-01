"""Technical and statistical indicators used by the regime engine and strategies S1-S5.

All functions take pandas Series / DataFrames indexed by date and return aligned Series, with
NaN where the look-back is not yet filled. Nothing here looks ahead: every value at index t
uses data up to and including t only.
"""

from __future__ import annotations

import math

import numpy as np
import pandas as pd


# --------------------------------------------------------------------------------------------
# Efficiency ratio and KAMA (strategy doc s.5)
# --------------------------------------------------------------------------------------------
def efficiency_ratio(close: pd.Series, n: int = 10) -> pd.Series:
    """Kaufman efficiency ratio.

        ER = |C_t - C_{t-n}| / sum_{i=0}^{n-1} |C_{t-i} - C_{t-i-1}|

    Near 1 in a clean trend, near 0 in noise. A zero denominator (flat prices) gives 0.
    Implemented in NumPy: it is called for every instrument on every bar.
    """
    c = close.to_numpy(dtype=float)
    out = np.full(len(c), np.nan)
    if len(c) > n:
        change = np.abs(c[n:] - c[:-n])
        absdiff = np.abs(np.diff(c))
        csum = np.concatenate([[0.0], np.cumsum(absdiff)])
        vol = csum[n:] - csum[:-n]               # sum of the last n absolute changes
        with np.errstate(divide="ignore", invalid="ignore"):
            out[n:] = np.where(vol > 0, change / vol, 0.0)
    return pd.Series(out, index=close.index)


def kama(close: pd.Series, n: int = 10, fast: int = 2, slow: int = 30) -> pd.Series:
    """Kaufman Adaptive Moving Average, KAMA(n, fast, slow).

        SC     = [ER x (f - s) + s]^2,  f = 2/(fast+1), s = 2/(slow+1)
        KAMA_t = KAMA_{t-1} + SC x (C_t - KAMA_{t-1})

    Seeded with the close at the first bar where ER is defined. KAMA(10,2,30) is the fast
    line used for entries in S1; KAMA(10,5,30) is the slow trend filter.
    """
    er = efficiency_ratio(close, n).to_numpy()
    c = close.to_numpy(dtype=float)
    f, s = 2.0 / (fast + 1), 2.0 / (slow + 1)
    out = np.full(len(c), np.nan)
    if len(c) <= n:
        return pd.Series(out, index=close.index)
    out[n] = c[n]
    for t in range(n + 1, len(c)):
        e = er[t] if not np.isnan(er[t]) else 0.0
        sc = (e * (f - s) + s) ** 2
        out[t] = out[t - 1] + sc * (c[t] - out[t - 1])
    return pd.Series(out, index=close.index)


# --------------------------------------------------------------------------------------------
# Volatility and channels
# --------------------------------------------------------------------------------------------
def true_range(df: pd.DataFrame) -> pd.Series:
    h = df["high"].to_numpy(dtype=float)
    l = df["low"].to_numpy(dtype=float)
    c = df["close"].to_numpy(dtype=float)
    pc = np.concatenate([[np.nan], c[:-1]])
    tr = np.fmax(h - l, np.fmax(np.abs(h - pc), np.abs(l - pc)))
    return pd.Series(tr, index=df.index)


def atr(df: pd.DataFrame, n: int = 20) -> pd.Series:
    """Average true range with Wilder smoothing (alpha = 1/n), seeded after n bars."""
    tr = true_range(df).to_numpy()
    out = np.full(len(tr), np.nan)
    if len(tr) >= n:
        alpha = 1.0 / n
        val = tr[0]
        for t in range(len(tr)):                 # Wilder EMA, adjust=False, min_periods=n
            val = tr[t] if t == 0 else val + alpha * (tr[t] - val)
            if t >= n - 1:
                out[t] = val
    return pd.Series(out, index=df.index)


def highest_high(df: pd.DataFrame, n: int) -> pd.Series:
    """Highest high of the *previous* n bars (excludes today), i.e. the channel to break."""
    return df["high"].rolling(n).max().shift(1)


def lowest_low(df: pd.DataFrame, n: int) -> pd.Series:
    return df["low"].rolling(n).min().shift(1)


def realized_vol(close: pd.Series, n: int = 20, annualize: bool = False) -> pd.Series:
    """Standard deviation of daily log returns over n bars."""
    vol = np.log(close).diff().rolling(n).std()
    return vol * math.sqrt(252) if annualize else vol


def sma(series: pd.Series, n: int) -> pd.Series:
    return series.rolling(n).mean()


def zscore(series: pd.Series, n: int) -> pd.Series:
    mean = series.rolling(n).mean()
    std = series.rolling(n).std()
    return (series - mean) / std.replace(0.0, np.nan)


def percentile_rank(series: pd.Series, window: int = 252) -> pd.Series:
    """Fraction of the trailing window (including today) at or below today's value.

    Used for India VIX against its own one-year range (regime engine, S3, S5).
    """
    a = series.to_numpy(dtype=float)
    out = np.full(len(a), np.nan)
    min_p = max(20, window // 4)
    for t in range(min_p - 1, len(a)):
        w = a[max(0, t - window + 1):t + 1]
        w = w[~np.isnan(w)]
        if len(w) >= min_p:
            out[t] = float((w <= a[t]).mean())
    return pd.Series(out, index=series.index)


def percentile_rank_last(series: pd.Series, window: int = 252) -> float:
    """Percentile rank of the latest value only (fast path for daily decisions)."""
    a = series.to_numpy(dtype=float)[-window:]
    a = a[~np.isnan(a)]
    if len(a) < max(20, window // 4):
        return float("nan")
    return float((a <= a[-1]).mean())


# --------------------------------------------------------------------------------------------
# Regression helpers (S3 residual returns, S4 hedge ratio)
# --------------------------------------------------------------------------------------------
def ols_beta(y: np.ndarray, x: np.ndarray) -> tuple[float, float]:
    """OLS of y on x with intercept. Returns (alpha, beta)."""
    x = np.asarray(x, dtype=float)
    y = np.asarray(y, dtype=float)
    mask = ~(np.isnan(x) | np.isnan(y))
    x, y = x[mask], y[mask]
    if len(x) < 3 or np.var(x) == 0:
        return 0.0, 0.0
    beta = float(np.cov(x, y, ddof=1)[0, 1] / np.var(x, ddof=1))
    alpha = float(y.mean() - beta * x.mean())
    return alpha, beta


def adf_tstat(series: np.ndarray, lags: int = 1) -> float:
    """Augmented Dickey-Fuller t-statistic (constant, no trend).

        dy_t = a + b y_{t-1} + sum_{i=1..lags} c_i dy_{t-i} + e_t

    Returns the t-statistic of b. Compare with ``ADF_CRITICAL`` (MacKinnon asymptotic values);
    more negative = more evidence of stationarity. Implemented with NumPy so the frozen build
    does not need statsmodels.
    """
    y = np.asarray(series, dtype=float)
    y = y[~np.isnan(y)]
    if len(y) < lags + 20:
        return 0.0
    dy = np.diff(y)
    rows = len(dy) - lags
    X = [np.ones(rows), y[lags:-1]]
    for i in range(1, lags + 1):
        X.append(dy[lags - i:-i])
    X = np.column_stack(X)
    target = dy[lags:]
    coef, *_ = np.linalg.lstsq(X, target, rcond=None)
    resid = target - X @ coef
    dof = rows - X.shape[1]
    if dof <= 0:
        return 0.0
    sigma2 = resid @ resid / dof
    try:
        cov = sigma2 * np.linalg.inv(X.T @ X)
    except np.linalg.LinAlgError:
        return 0.0
    se = math.sqrt(max(cov[1, 1], 1e-18))
    return float(coef[1] / se)


# MacKinnon (1996) asymptotic critical values, constant only.
ADF_CRITICAL = {0.01: -3.43, 0.05: -2.86, 0.10: -2.57}


def half_life(spread: np.ndarray) -> float:
    """Half-life of mean reversion from an AR(1) fit: ds_t = a + lambda s_{t-1}.

    half-life = -ln(2) / lambda; returns inf when the spread does not mean-revert.
    """
    s = np.asarray(spread, dtype=float)
    s = s[~np.isnan(s)]
    if len(s) < 10:
        return math.inf
    _, lam = ols_beta(np.diff(s), s[:-1])
    if lam >= 0:
        return math.inf
    return float(-math.log(2) / lam)


# --------------------------------------------------------------------------------------------
# Options (S5): Black-Scholes on futures/index, used for strike selection and paper chains
# --------------------------------------------------------------------------------------------
def _norm_cdf(x: float) -> float:
    return 0.5 * (1.0 + math.erf(x / math.sqrt(2.0)))


def bs_price(spot: float, strike: float, t_years: float, vol: float, right: str,
             rate: float = 0.065) -> float:
    """Black-Scholes price of a European option (index options are European, cash-settled)."""
    if t_years <= 0 or vol <= 0:
        intrinsic = spot - strike if right == "CE" else strike - spot
        return max(intrinsic, 0.0)
    d1 = (math.log(spot / strike) + (rate + 0.5 * vol * vol) * t_years) / (vol * math.sqrt(t_years))
    d2 = d1 - vol * math.sqrt(t_years)
    disc = math.exp(-rate * t_years)
    if right == "CE":
        return spot * _norm_cdf(d1) - strike * disc * _norm_cdf(d2)
    return strike * disc * _norm_cdf(-d2) - spot * _norm_cdf(-d1)


def bs_delta(spot: float, strike: float, t_years: float, vol: float, right: str,
             rate: float = 0.065) -> float:
    """Option delta: positive for calls, negative for puts."""
    if t_years <= 0 or vol <= 0:
        if right == "CE":
            return 1.0 if spot > strike else 0.0
        return -1.0 if spot < strike else 0.0
    d1 = (math.log(spot / strike) + (rate + 0.5 * vol * vol) * t_years) / (vol * math.sqrt(t_years))
    return _norm_cdf(d1) if right == "CE" else _norm_cdf(d1) - 1.0
