from datetime import date

import pandas as pd
import pytest

from algotrader.core.config import is_loosening, merge_strictest, validate_config
from algotrader.core.costs import CostTable, cost_viable
from algotrader.core.models import Mode, OrderSide
from algotrader.regime import Regime, RegimeEngine


# -- layered limits ---------------------------------------------------------------------------
def test_strictest_value_wins():
    merged = merge_strictest(
        {"max_risk_per_trade_pct": 0.5, "require_stop": False, "blocked_windows": ["a"],
         "options": {"naked_short": True, "max_loss_pct_nav": 1.0}},
        {"max_risk_per_trade_pct": 0.8, "require_stop": True, "blocked_windows": ["b"],
         "options": {"naked_short": False, "max_loss_pct_nav": 0.5}},
        {"max_risk_per_trade_pct": 0.25})
    assert merged["max_risk_per_trade_pct"] == 0.25
    assert merged["require_stop"] is True
    assert merged["blocked_windows"] == ["a", "b"]
    assert merged["options"] == {"naked_short": False, "max_loss_pct_nav": 0.5}


def test_loosening_detection():
    assert is_loosening("max_margin_util_pct", 40, 50)
    assert not is_loosening("max_margin_util_pct", 40, 30)
    assert is_loosening("require_stop", True, False)


def test_profile_inheritance_can_only_tighten(cfg):
    prof = cfg.risk.resolve_profile("defined_risk_only")
    assert prof["require_stop"] is True and prof["options"]["naked_short"] is False
    assert prof["max_margin_util_pct"] == 35


def test_shipped_config_is_valid(cfg, registry):
    assert validate_config(cfg, registry.keys()) == []


def test_validator_rejects_incoherent_config(cfg, registry):
    bad = cfg.model_copy(deep=True)
    bad.portfolio.strategies["S1_ADAPTIVE_TREND"].per_trade_risk_pct = 2.0
    bad.bindings.bindings[0].mode = Mode.LIVE          # stage is still 'backtest'
    errors = validate_config(bad, registry.keys())
    assert any("above global" in e for e in errors)
    assert any("promotion gates" in e for e in errors)


# -- costs ------------------------------------------------------------------------------------
def test_stt_example_from_strategy_doc(costs):
    """STT on a futures sale is 0.05% of notional: Rs 10 lakh -> Rs 500 (strategy doc s.4)."""
    p = costs.profile("equity_futures", date(2026, 9, 1))
    c = CostTable.order_charges(p, OrderSide.SELL, 1_000_000, 1)
    assert c.stt == pytest.approx(500.0)
    old = costs.profile("equity_futures", date(2025, 9, 1))
    assert CostTable.order_charges(old, OrderSide.SELL, 1_000_000, 1).stt == pytest.approx(200.0)
    assert CostTable.order_charges(p, OrderSide.BUY, 1_000_000, 1).stt == 0.0


def test_pinned_and_scaled_costs(costs):
    pinned = costs.pinned(None)
    assert pinned.profile("equity_futures", date(2020, 1, 1)).stt_sell_pct == 0.05
    stressed = costs.scaled(1.5)
    base = costs.round_trip("equity_futures", 25000, 65, 0.1, date(2026, 9, 1))
    assert stressed.round_trip("equity_futures", 25000, 65, 0.1, date(2026, 9, 1)) == pytest.approx(base * 1.5)


def test_option_exercise_stt(costs):
    assert costs.exercise_stt("index_options", 100, 65, date(2026, 9, 1)) == pytest.approx(9.75)


def test_cost_viability_gate():
    assert cost_viable(100, 2000, 1000)[0]
    assert not cost_viable(300, 2000, 1000)[0]      # > 10% of avg gross profit
    assert not cost_viable(200, None, 1000)[0]      # > 15% of one ATR


# -- regime -----------------------------------------------------------------------------------
def test_regime_hysteresis_and_fail_closed(cfg):
    eng = RegimeEngine(cfg.portfolio.regime)
    assert eng.permission("S1_ADAPTIVE_TREND") == 0.0           # unknown regime -> no entries
    idx = pd.bdate_range("2024-01-01", periods=60)
    trend = pd.Series(range(100, 160), index=idx, dtype=float)
    chop = pd.Series([100, 101] * 30, index=idx, dtype=float)
    vix = pd.Series([15.0] * 60, index=idx)
    assert eng.update(idx[-1].date(), trend, vix).regime is Regime.TREND_CALM
    snap = eng.update(idx[-1].date(), chop, vix)                 # day 1 of new condition
    assert snap.regime is Regime.TREND_CALM and snap.pending is Regime.RANGE_CALM
    snap = eng.update(idx[-1].date(), chop, vix)                 # day 2: change takes effect
    assert snap.regime is Regime.RANGE_CALM and snap.changed
    assert eng.permission("S5_RANGE_IRON_CONDOR") == 1.0
