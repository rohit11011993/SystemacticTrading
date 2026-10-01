"""Instrument registry (PRD s.5, FR-5.2, FR-6.2).

Each instrument is a configuration record, not code. Contract specifications (lot size, tick
size) are versioned by effective date because the exchanges change them often. Adding an
instrument is a YAML edit in ``config/instruments.yaml``.
"""

from __future__ import annotations

import calendar as _cal
from datetime import date, timedelta
from pathlib import Path
from typing import Any, Iterable

import yaml
from pydantic import BaseModel, ConfigDict, Field, model_validator

from .models import InstrumentType


class ContractSpec(BaseModel):
    """Contract specification in force from ``effective`` onward."""

    model_config = ConfigDict(frozen=True)
    effective: date
    lot_size: int = Field(gt=0)
    tick_size: float = Field(gt=0)


class Liquidity(BaseModel):
    adv_lots: float = 1e9          # average daily volume in lots (refreshed from data)
    max_adv_pct: float = 1.0       # maximum order size as % of ADV


class InstrumentFlags(BaseModel):
    """Mutable daily flags. Updated by the master refresh / ban-list download."""

    banned: bool = False                       # F&O ban period
    circuit: bool = False                      # circuit hit today
    expiries: list[date] = Field(default_factory=list)
    roll_days_before_expiry: int = 3           # strategy doc s.4 "Contract rolling"
    open_cutoff_days: int = 2                  # FR-10.5: no new positions this close to expiry


class ExpiryRule(BaseModel):
    """Fallback rule used only when no explicit expiry list is configured.

    Exchanges have changed expiry weekdays more than once (strategy doc s.9), so the explicit
    list from the daily master refresh always wins over this rule.
    """

    kind: str = "last_weekday"    # last <weekday> of each month
    weekday: int = 1              # Monday=0 ... Tuesday=1


class Instrument(BaseModel):
    """One registry record (field list from PRD s.5)."""

    key: str
    symbol: str
    exchange: str
    segment: str
    type: InstrumentType
    specs: list[ContractSpec]
    price_band_pct: float = 20.0
    margin_pct: float = 15.0        # approximate initial margin as % of notional
    session: str = "NSE"
    cost_profile: str
    sector: str = "none"
    cluster: str | None = None      # correlated-cluster key for exposure caps
    beta: float = 1.0
    liquidity: Liquidity = Field(default_factory=Liquidity)
    flags: InstrumentFlags = Field(default_factory=InstrumentFlags)
    expiry_rule: ExpiryRule | None = None
    underlying: str | None = None   # options: registry key of the underlying future
    strike_step: float | None = None
    wing_width: float | None = None  # S5 default wing width in index points
    data_source: str = "csv"
    data_symbol: str | None = None  # file / feed name; defaults to the key
    broker_token: str | None = None
    tradable: bool = True

    @model_validator(mode="after")
    def _sort_specs(self) -> "Instrument":
        self.specs.sort(key=lambda s: s.effective)
        return self

    # -- contract specification ------------------------------------------------------------
    def spec(self, on: date | None = None) -> ContractSpec:
        """Specification in force on ``on`` (latest spec if ``on`` is None)."""
        if on is None:
            return self.specs[-1]
        current = self.specs[0]
        for s in self.specs:
            if s.effective <= on:
                current = s
        return current

    def lot_size(self, on: date | None = None) -> int:
        return self.spec(on).lot_size

    def tick_size(self, on: date | None = None) -> float:
        return self.spec(on).tick_size

    @property
    def feed(self) -> str:
        return self.data_symbol or self.key

    # -- expiries --------------------------------------------------------------------------
    def expiries(self, start: date, end: date) -> list[date]:
        """Expiry dates within [start, end] from the explicit list, else from the rule."""
        if self.flags.expiries:
            return [d for d in self.flags.expiries if start <= d <= end]
        if self.expiry_rule is None:
            return []
        out: list[date] = []
        y, m = start.year, start.month
        while date(y, m, 1) <= end:
            last_day = date(y, m, _cal.monthrange(y, m)[1])
            d = last_day - timedelta(days=(last_day.weekday() - self.expiry_rule.weekday) % 7)
            if start <= d <= end:
                out.append(d)
            m += 1
            if m > 12:
                y, m = y + 1, 1
        return out

    def next_expiry(self, on: date) -> date | None:
        upcoming = self.expiries(on, on + timedelta(days=70))
        return upcoming[0] if upcoming else None


class InstrumentRegistry:
    """In-memory registry loaded from YAML. Strategies receive a read-only view."""

    def __init__(self, instruments: Iterable[Instrument]):
        self._items: dict[str, Instrument] = {i.key: i for i in instruments}

    @classmethod
    def from_yaml(cls, path: str | Path) -> "InstrumentRegistry":
        raw = yaml.safe_load(Path(path).read_text(encoding="utf-8")) or {}
        items = [Instrument(key=k, **v) for k, v in (raw.get("instruments") or {}).items()]
        return cls(items)

    def get(self, key: str) -> Instrument:
        try:
            return self._items[key]
        except KeyError as exc:
            raise KeyError(f"instrument '{key}' is not in the registry") from exc

    def __contains__(self, key: str) -> bool:
        return key in self._items

    def keys(self) -> list[str]:
        return list(self._items)

    def all(self) -> list[Instrument]:
        return list(self._items.values())

    def set_flag(self, key: str, **flags: Any) -> None:
        """Update daily flags (ban list, circuit) without touching the core."""
        inst = self.get(key)
        inst.flags = inst.flags.model_copy(update=flags)

    def apply_master_refresh(self, records: dict[str, dict[str, Any]], on: date) -> list[str]:
        """Merge a daily instrument-master download (FR-6.2).

        ``records`` maps registry key -> {"lot_size", "tick_size", "expiries", "broker_token"}.
        A changed contract spec is appended as a new effective-dated spec and reported so the
        operator can review it.
        """
        changes: list[str] = []
        for key, rec in records.items():
            if key not in self._items:
                continue
            inst = self._items[key]
            cur = inst.spec(on)
            lot = int(rec.get("lot_size", cur.lot_size))
            tick = float(rec.get("tick_size", cur.tick_size))
            if lot != cur.lot_size or tick != cur.tick_size:
                inst.specs.append(ContractSpec(effective=on, lot_size=lot, tick_size=tick))
                inst.specs.sort(key=lambda s: s.effective)
                changes.append(f"{key}: lot {cur.lot_size}->{lot}, tick {cur.tick_size}->{tick}")
            if "expiries" in rec:
                inst.flags = inst.flags.model_copy(update={"expiries": sorted(rec["expiries"])})
            if rec.get("broker_token") and rec["broker_token"] != inst.broker_token:
                inst.broker_token = str(rec["broker_token"])
        return changes


class InstrumentView:
    """Read-only facade handed to strategies; returns copies so plugins cannot mutate state."""

    def __init__(self, registry: InstrumentRegistry):
        self._reg = registry

    def get(self, key: str) -> Instrument:
        return self._reg.get(key).model_copy(deep=True)

    def lot_size(self, key: str, on: date | None = None) -> int:
        return self._reg.get(key).lot_size(on)

    def type(self, key: str) -> InstrumentType:
        return self._reg.get(key).type

    def sector(self, key: str) -> str:
        return self._reg.get(key).sector

    def beta(self, key: str) -> float:
        return self._reg.get(key).beta
