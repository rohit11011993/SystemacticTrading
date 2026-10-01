"""Plugin contract and end-to-end engine tests (backtest, paper restart, manual exit)."""

from datetime import date

import pytest
from pydantic import ValidationError

from algotrader.app import ConfigError, build_engine
from algotrader.backtest import run_backtest
from algotrader.core.models import AlignmentState, Expectation, Mode, Side, Trade, TradeLeg
from algotrader.monitoring.alignment import evaluate_alignment
from algotrader.monitoring.reconcile import reconcile
from algotrader.risk.killswitch import KSAction, KSLevel
from algotrader.strategy.loader import PluginError, check_source, load_plugin_file, load_plugins

from .conftest import CONFIG, ROOT


# -- plugin loader ----------------------------------------------------------------------------
def test_all_five_strategies_load():
    plugins = load_plugins(ROOT / "plugins")
    assert set(plugins) == {"S1_ADAPTIVE_TREND", "S2_CHANNEL_BREAKOUT", "S3_SHORT_TERM_REVERSAL",
                            "S4_STAT_PAIRS", "S5_RANGE_IRON_CONDOR"}
    assert all(len(p.sha256) == 64 for p in plugins.values())


@pytest.mark.parametrize("src", [
    "from algotrader.execution.oms import OrderManager",
    "from algotrader.risk import gateway",
    "import os",
    "import subprocess",
    "from algotrader import engine",
    "x = open('secrets.txt')",
    "eval('1+1')",
])
def test_forbidden_code_is_rejected(src):
    assert check_source(src)


def test_allowed_imports_pass():
    assert check_source("import numpy as np\nfrom algotrader.strategy.api import Strategy, Signal") == []


def test_plugin_without_supported_types_rejected(tmp_path):
    p = tmp_path / "bad.py"
    p.write_text(
        "from pydantic import BaseModel\n"
        "from algotrader.strategy.api import Strategy\n"
        "class P(BaseModel):\n    pass\n"
        "class Bad(Strategy):\n    id='BAD'\n    version='0'\n    params_model=P\n"
        "    def on_bar(self, ctx):\n        return []\n")
    with pytest.raises(PluginError):
        load_plugin_file(p)


def test_params_validated_against_test_ranges():
    plugins = load_plugins(ROOT / "plugins")
    cls = plugins["S1_ADAPTIVE_TREND"].classes["S1_ADAPTIVE_TREND"]
    with pytest.raises(ValidationError):
        cls({"er_min": 0.9})            # outside the documented 0.20-0.40 range


# -- alignment / reconciliation ---------------------------------------------------------------
def _trade():
    return Trade("t1", "S1", Side.LONG, {"X": TradeLeg("X", "X", 1, qty=10, avg_price=100)}, initial_risk=100)


def test_alignment_states():
    assert evaluate_alignment(_trade(), Expectation(), 0.5, 3).state is AlignmentState.IN_LINE
    assert evaluate_alignment(_trade(), Expectation(time_budget_days=2), 0.5, 3).state is AlignmentState.DRIFTING
    off = evaluate_alignment(_trade(), Expectation(invalidation_crossed=True, invalidation_level=95), -1.0, 3)
    assert off.state is AlignmentState.OFF_THESIS and off.failing
    both = evaluate_alignment(_trade(), Expectation(hold_condition=False, corridor=(0.0, None)), -0.5, 3)
    assert both.state is AlignmentState.OFF_THESIS     # hold fails together with a path breach


def test_reconcile_detects_mismatch():
    assert reconcile({"A": 10}, {"A": 10}).ok
    r = reconcile({"A": 10}, {"A": 5, "B": 1})
    assert not r.ok and set(r.mismatches) == {"A", "B"} and "MISMATCH" in r.banner()


# -- engine -----------------------------------------------------------------------------------
def test_binding_to_unsupported_type_refused(cfg, data):
    bad = cfg.model_copy(deep=True)
    bad.bindings.bindings[0].instruments = ["NIFTY_OPT"]
    with pytest.raises(ConfigError):
        build_engine(CONFIG, cfg=bad, data=data)


def test_live_mode_requires_approved_plugins(cfg, data):
    live = cfg.model_copy(deep=True)
    for b in live.bindings.bindings:
        b.mode, b.stage = Mode.LIVE, "live"
    with pytest.raises(ConfigError, match="re-approve"):
        build_engine(CONFIG, cfg=live, data=data, mode=Mode.LIVE)


@pytest.fixture(scope="module")
def backtest_run(frames):
    from algotrader.data.provider import InMemoryDataProvider
    engine = build_engine(CONFIG, data=InMemoryDataProvider(frames))
    res = run_backtest(engine, date(2020, 6, 1), date(2022, 6, 30))
    return engine, res


def test_backtest_runs_and_trades(backtest_run):
    engine, res = backtest_run
    assert len(res.equity) > 400
    assert res.summary["trades_closed"] > 10
    assert engine.rc.health.reconciled                       # book always matches the broker
    assert res.trades["charges"].sum() > 0                   # results are net of costs


def test_every_order_is_tagged_and_gated(backtest_run):
    engine, _ = backtest_run
    assert engine.oms.orders
    for o in engine.oms.orders.values():
        assert o.algo_tag.startswith("ALGO-")                # REG-3
    for t in engine.book.closed_trades():
        if t.stop_price is not None and t.meta.get("role") != "hedge" and len(t.legs) == 1:
            # Stops only ever moved in the direction of lower risk.
            assert (t.stop_price >= t.initial_stop) if t.side is Side.LONG else (t.stop_price <= t.initial_stop)


def test_risk_per_trade_never_exceeded(backtest_run):
    engine, _ = backtest_run
    for t in engine.book.trades.values():
        if t.meta.get("role") == "hedge" or t.status == "CANCELLED":
            continue
        # The stop distance is kept when the stop is re-anchored to the fill, so the initial
        # risk never exceeds 0.5% of the NAV at the time of the decision.
        assert t.initial_risk <= 0.005 * max(e for _, e in engine.equity_curve) * 1.0001


def test_restart_restores_state_and_reconciles(frames, tmp_path):
    from algotrader.data.provider import InMemoryDataProvider
    data = InMemoryDataProvider(frames)
    state = str(tmp_path / "state.db")
    e1 = build_engine(CONFIG, data=data, mode=Mode.PAPER, state_path=state, only=["S1_ADAPTIVE_TREND"])
    days = data.trading_days("NIFTY_FUT", date(2021, 1, 1), date(2021, 12, 31))
    for ts in days[:-5]:
        e1.run_day(ts)
    open_before = {t.trade_id for t in e1.book.open_trades()}
    realized_before = sum(t.realized() - t.charges for t in e1.book.trades.values())
    e2 = build_engine(CONFIG, data=data, mode=Mode.PAPER, state_path=state, only=["S1_ADAPTIVE_TREND"])
    e2.restore_state()
    assert {t.trade_id for t in e2.book.open_trades()} == open_before
    assert sum(t.realized() - t.charges for t in e2.book.trades.values()) == pytest.approx(realized_before)
    assert e2._last_day == days[-6].date()
    for ts in days[-5:]:
        e2.run_day(ts)
    assert e2.rc.health.reconciled


def test_manual_exit_works_while_entries_blocked(frames):
    from algotrader.data.provider import InMemoryDataProvider
    data = InMemoryDataProvider(frames)
    e = build_engine(CONFIG, data=data, only=["S1_ADAPTIVE_TREND"])
    days = data.trading_days("NIFTY_FUT", date(2020, 6, 1), date(2021, 12, 31))
    i = 0
    while not e.book.open_trades(include_pending=False) and i < len(days) - 2:
        e.run_day(days[i])
        i += 1
    trade = e.book.open_trades(include_pending=False)[0]
    e.ks.manual_kill(flatten=False)                          # entries blocked
    assert e.manual_exit(trade.trade_id, reason="operator test")
    e.run_day(days[i])
    assert e.book.get(trade.trade_id).status == "CLOSED"
    assert e.ks.trips[(KSLevel.SYSTEM, "system")].action is KSAction.BLOCK_ENTRIES
