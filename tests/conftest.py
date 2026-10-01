"""Shared fixtures: the real configuration folder plus in-memory synthetic data."""

from __future__ import annotations

import sys
from datetime import datetime
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from algotrader.core.audit import AuditLog  # noqa: E402
from algotrader.core.calendar import EventCalendar  # noqa: E402
from algotrader.core.config import load_config  # noqa: E402
from algotrader.core.costs import CostTable  # noqa: E402
from algotrader.core.instruments import InstrumentRegistry  # noqa: E402
from algotrader.core.models import Quote  # noqa: E402
from algotrader.data.provider import InMemoryDataProvider  # noqa: E402
from algotrader.data.synthetic import generate  # noqa: E402
from algotrader.portfolio.book import Book  # noqa: E402
from algotrader.regime import RegimeEngine  # noqa: E402
from algotrader.risk.gateway import RiskContext, RiskGateway  # noqa: E402
from algotrader.risk.killswitch import KillSwitchManager  # noqa: E402

CONFIG = ROOT / "config"


@pytest.fixture(scope="session")
def cfg():
    return load_config(CONFIG)


@pytest.fixture(scope="session")
def frames():
    return generate("2019-01-01", "2022-06-30", seed=11)


@pytest.fixture()
def data(frames):
    return InMemoryDataProvider(frames)


@pytest.fixture()
def registry():
    return InstrumentRegistry.from_yaml(CONFIG / "instruments.yaml")


@pytest.fixture()
def costs():
    return CostTable.from_yaml(CONFIG / "costs.yaml")


class Clock:
    def __init__(self, t: datetime):
        self.t = t

    def __call__(self) -> datetime:
        return self.t


@pytest.fixture()
def clock():
    return Clock(datetime(2026, 9, 1, 15, 30))


@pytest.fixture()
def gateway_env(cfg, registry, costs, clock):
    """A risk gateway over an empty book with fresh quotes and a permissive regime."""
    audit = AuditLog()
    book = Book()
    ks = KillSwitchManager(cfg.killswitch, cfg.portfolio, audit, clock)
    regime = RegimeEngine(cfg.portfolio.regime)
    regime.current = __import__("algotrader.regime", fromlist=["Regime"]).Regime.RANGE_CALM
    nav = 5_000_000.0
    rc = RiskContext(registry=registry, calendar=EventCalendar(), costs=costs, book=book, killswitch=ks,
                     regime=regime, portfolio=cfg.portfolio, now=clock, nav=lambda: nav)
    prices = {"NIFTY_FUT": 25000.0, "BANKNIFTY_FUT": 55000.0, "GOLD_FUT": 70000.0, "HDFCBANK_FUT": 1600.0,
              "ICICIBANK_FUT": 1200.0, "CRUDE_FUT": 6000.0}
    for k, v in prices.items():
        rc.quotes[k] = Quote(k, v, clock())
    gw = RiskGateway(rc, cfg.risk, audit, cfg.config_hash,
                     profiles={b.strategy: b.risk_profile for b in cfg.bindings.bindings})
    return gw, rc, ks, audit
