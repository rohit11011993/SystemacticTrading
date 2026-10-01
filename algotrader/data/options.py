"""Option chains for S5.

``ModelOptionChain`` builds an end-of-day chain from the underlying close and India VIX with
Black-Scholes. It exists so S5 can run in backtest and paper mode before a historical option
chain source is chosen (strategy doc s.12, open question 3). It is a model, not market data:

* implied volatility = VIX / 100 x ``iv_multiplier`` with a mild smile;
* bid / ask = model mid -/+ a half-spread of max(min_half_spread, spread_pct x mid);
* the strategy doc requires S5 slippage to be stressed at twice the base estimate, which the
  backtest does through the paper broker's slippage multiplier.

Live trading must replace this with the broker's quotes.
"""

from __future__ import annotations

import math
from datetime import date

import pandas as pd

from ..core.instruments import InstrumentRegistry
from ..indicators import bs_delta, bs_price
from .provider import MarketDataProvider


class ModelOptionChain:
    def __init__(self, data: MarketDataProvider, registry: InstrumentRegistry, vix_feed: str = "INDIAVIX",
                 iv_multiplier: dict[str, float] | None = None, width_pct: float = 0.25,
                 spread_pct: float = 0.01, min_half_spread: float = 0.5, smile: float = 1.5):
        self.data = data
        self.registry = registry
        self.vix_feed = vix_feed
        self.iv_multiplier = iv_multiplier or {}
        # Strikes span +/- width_pct of spot, so the wings of an open structure stay in the chain
        # after the index moves (otherwise exits that need marks would silently stop working).
        self.width_pct = width_pct
        self.spread_pct = spread_pct
        self.min_half_spread = min_half_spread
        self.smile = smile

    def _spot_iv(self, option_key: str, day: date) -> tuple[float, float]:
        inst = self.registry.get(option_key)
        under = self.registry.get(inst.underlying or "")
        ts = pd.Timestamp(day)
        spot = float(self.data.history(under.feed)["close"].loc[:ts].iloc[-1])
        vix = float(self.data.history(self.vix_feed)["close"].loc[:ts].iloc[-1])
        return spot, vix / 100.0 * self.iv_multiplier.get(option_key, 1.0)

    def _iv(self, base_iv: float, spot: float, strike: float) -> float:
        return base_iv * (1.0 + self.smile * abs(math.log(strike / spot)))

    def chain(self, option_key: str, expiry: date, day: date) -> pd.DataFrame:
        inst = self.registry.get(option_key)
        step = inst.strike_step or 50.0
        spot, base_iv = self._spot_iv(option_key, day)
        t = max((expiry - day).days, 0) / 365.0
        atm = round(spot / step) * step
        n = int(math.ceil(self.width_pct * spot / step))
        rows = []
        for i in range(-n, n + 1):
            k = atm + i * step
            if k <= 0:
                continue
            iv = self._iv(base_iv, spot, k)
            for right in ("CE", "PE"):
                mid = bs_price(spot, k, t, iv, right)
                half = max(self.min_half_spread, self.spread_pct * mid)
                rows.append({"strike": k, "right": right, "mid": mid, "bid": max(0.05, mid - half),
                             "ask": mid + half, "iv": iv, "delta": bs_delta(spot, k, t, iv, right),
                             "spot": spot})
        return pd.DataFrame(rows)

    def quote(self, option_key: str, expiry: date, strike: float, right: str, day: date) -> dict[str, float]:
        spot, base_iv = self._spot_iv(option_key, day)
        t = max((expiry - day).days, 0) / 365.0
        iv = self._iv(base_iv, spot, strike)
        mid = bs_price(spot, strike, t, iv, right)
        half = max(self.min_half_spread, self.spread_pct * mid)
        return {"mid": mid, "bid": max(0.05, mid - half), "ask": mid + half, "iv": iv,
                "delta": bs_delta(spot, strike, t, iv, right), "spot": spot}
