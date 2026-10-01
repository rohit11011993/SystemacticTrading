"""Market-data provider interface and the local file-replay adapter (PRD s.6, FR-6.10).

Historical data is stored locally (CSV, or Parquet when ``pyarrow`` is installed) so backtests
are reproducible. Each file holds one continuous, roll- and corporate-action-adjusted daily
series with columns ``date, open, high, low, close, volume``; the file name is the instrument's
``data_symbol`` (defaults to the registry key).
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from datetime import date
from pathlib import Path

import pandas as pd

REQUIRED = ["open", "high", "low", "close"]


class MarketDataProvider(ABC):
    @abstractmethod
    def history(self, feed: str) -> pd.DataFrame:
        """Full stored daily history for one feed (index: Timestamp)."""

    @abstractmethod
    def feeds(self) -> list[str]: ...

    def trading_days(self, feed: str, start: date | None = None, end: date | None = None) -> list[pd.Timestamp]:
        idx = self.history(feed).index
        if start:
            idx = idx[idx >= pd.Timestamp(start)]
        if end:
            idx = idx[idx <= pd.Timestamp(end)]
        return list(idx)


def validate_bars(df: pd.DataFrame, name: str) -> pd.DataFrame:
    """Basic integrity checks: required columns, sorted unique dates, sane OHLC."""
    missing = [c for c in REQUIRED if c not in df.columns]
    if missing:
        raise ValueError(f"{name}: missing columns {missing}")
    if "volume" not in df.columns:
        df["volume"] = 0.0
    df = df[~df.index.duplicated(keep="last")].sort_index()
    bad = (df["high"] < df[["open", "close"]].max(axis=1)) | (df["low"] > df[["open", "close"]].min(axis=1))
    if bad.any():
        raise ValueError(f"{name}: {int(bad.sum())} bars with inconsistent OHLC")
    return df[["open", "high", "low", "close", "volume"]].astype(float)


class FileDataProvider(MarketDataProvider):
    """Loads ``<data_dir>/<feed>.csv`` (or ``.parquet``) lazily and caches it."""

    def __init__(self, data_dir: str | Path):
        self.data_dir = Path(data_dir)
        self._cache: dict[str, pd.DataFrame] = {}

    def feeds(self) -> list[str]:
        return sorted({p.stem for p in self.data_dir.glob("*.csv")} |
                      {p.stem for p in self.data_dir.glob("*.parquet")})

    def history(self, feed: str) -> pd.DataFrame:
        if feed not in self._cache:
            pq = self.data_dir / f"{feed}.parquet"
            csv = self.data_dir / f"{feed}.csv"
            if pq.exists():
                df = pd.read_parquet(pq)
            elif csv.exists():
                df = pd.read_csv(csv)
            else:
                raise FileNotFoundError(f"no data file for feed '{feed}' in {self.data_dir}")
            df.columns = [c.lower() for c in df.columns]
            df["date"] = pd.to_datetime(df["date"])
            df = df.set_index("date")
            self._cache[feed] = validate_bars(df, feed)
        return self._cache[feed]


class InMemoryDataProvider(MarketDataProvider):
    """Provider backed by a dict of DataFrames (tests and synthetic runs)."""

    def __init__(self, frames: dict[str, pd.DataFrame]):
        self._frames = {k: validate_bars(v.copy(), k) for k, v in frames.items()}

    def feeds(self) -> list[str]:
        return sorted(self._frames)

    def history(self, feed: str) -> pd.DataFrame:
        return self._frames[feed]
