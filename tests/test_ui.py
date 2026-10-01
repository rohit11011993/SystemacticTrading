"""Desktop UI tests.

The command path (UI -> state DB queue -> engine -> gateway) is tested without Qt. The Qt part
runs offscreen and is skipped when PySide6 or its system libraries are unavailable.
"""

from __future__ import annotations

import json
import os
from datetime import date

import pytest

from algotrader.app import build_engine
from algotrader.core.models import Mode
from algotrader.data.provider import InMemoryDataProvider
from algotrader.ui.bridge import UiBridge

from .conftest import CONFIG


@pytest.fixture(scope="module")
def live_engine(frames, tmp_path_factory):
    """A paper engine with at least one open single-leg trade, plus a bridge on its store."""
    data = InMemoryDataProvider(frames)
    state = str(tmp_path_factory.mktemp("ui") / "state.db")
    e = build_engine(CONFIG, data=data, mode=Mode.PAPER, state_path=state, only=["S1_ADAPTIVE_TREND"])
    days = data.trading_days("NIFTY_FUT", date(2020, 6, 1), date(2022, 6, 30))
    i = 0
    while not [t for t in e.book.open_trades(include_pending=False) if t.stop_price] and i < len(days) - 40:
        e.run_day(days[i])
        i += 1
    e._days, e._i = days, i
    return e, UiBridge(CONFIG, store=e.store)


def _last(bridge: UiBridge) -> dict:
    return bridge.commands(1)[0]


def test_snapshot_is_published_and_complete(live_engine):
    e, bridge = live_engine
    snap = bridge.snapshot()
    for key in ("nav", "day_pnl", "drawdown_pct", "ladder", "margin_util_pct", "health", "regime",
                "kill_switches", "trades", "strategies", "alerts", "instrument_flags"):
        assert key in snap
    assert snap["trades"] and snap["trades"][0]["alignment"]
    assert json.dumps(snap)                                   # plain JSON for any front end
    assert bridge.engine_status()[0]


def test_commands_go_through_engine_and_gateway(live_engine):
    e, bridge = live_engine
    trade = next(t for t in e.book.open_trades(include_pending=False) if t.stop_price)

    # A wider stop is refused: stops only move toward lower risk.
    wider = trade.stop_price - 1000 if trade.side.value > 0 else trade.stop_price + 1000
    bridge.adjust_stop(trade.trade_id, wider)
    e.process_commands()
    assert _last(bridge)["status"] == "FAILED"

    bridge.pause("S1_ADAPTIVE_TREND")
    e.process_commands()
    assert "S1_ADAPTIVE_TREND" in e.paused
    assert bridge.snapshot()["strategies"][0]["paused"]
    bridge.pause("S1_ADAPTIVE_TREND", paused=False)
    e.process_commands()
    assert not e.paused

    bridge.reset_request("system", "system", "")             # no such trip / no reason -> refused
    e.process_commands()
    assert _last(bridge)["status"] == "FAILED"

    bridge.send("no_such_command")
    e.process_commands()
    assert _last(bridge)["status"] == "FAILED"


def test_limit_changes_tighten_now_loosen_later(live_engine):
    e, bridge = live_engine
    bridge.propose_limit("global", None, "max_order_lots", 40, "tighten for test")
    bridge.propose_limit("global", None, "max_margin_util_pct", 60, "loosen for test")
    e.process_commands()
    assert e.cfg.risk.global_limits["max_order_lots"] == 40
    assert e.cfg.risk.global_limits["max_margin_util_pct"] == 40     # unchanged until cooled off
    pending = bridge.snapshot()["pending_limit_changes"]
    assert pending and pending[0]["key"] == "max_margin_util_pct" and not pending[0]["confirmed"]


def test_manual_exit_and_kill_from_ui(live_engine):
    e, bridge = live_engine
    trade = next(t for t in e.book.open_trades(include_pending=False) if t.stop_price)
    bridge.kill(flatten=False, reason="test")
    e.process_commands()
    assert e.ks.entry_block_reasons("S1_ADAPTIVE_TREND")           # entries blocked
    bridge.manual_exit(trade.trade_id, 1.0, "operator test")         # exits still allowed
    e.process_commands()
    assert _last(bridge)["status"] == "DONE"
    e.run_day(e._days[e._i])
    assert e.book.get(trade.trade_id).status == "CLOSED"
    assert any(r["event"] == "operator_command" for r in e.audit.records())
    assert bridge.estimate_exit_cost([{"legs_detail": [{"symbol": "NIFTY_FUT", "qty": 65, "avg": 25000,
                                                         "last": 25000}]}]) > 0


# -- Qt ---------------------------------------------------------------------------------------
@pytest.fixture(scope="module")
def qapp():
    os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
    try:
        from PySide6.QtWidgets import QApplication
    except ImportError as exc:      # PySide6 or libEGL missing
        pytest.skip(f"PySide6 unavailable: {exc}")
    app = QApplication.instance() or QApplication([])
    app.setOrganizationName("AlgoTraderTests")
    app.setApplicationName("ui-tests")
    from PySide6.QtCore import QSettings
    QSettings().clear()
    return app


def test_main_window_screens_and_controls(qapp, frames, tmp_path):
    from algotrader.ui import widgets
    from algotrader.ui.main_window import MainWindow
    from algotrader.ui.theme import apply_theme

    data = InMemoryDataProvider(frames)
    e = build_engine(CONFIG, data=data, mode=Mode.PAPER, state_path=str(tmp_path / "s.db"),
                     only=["S1_ADAPTIVE_TREND"])
    days = data.trading_days("NIFTY_FUT", date(2020, 6, 1), date(2022, 6, 30))
    for ts in days:
        e.run_day(ts)
        if e.book.open_trades(include_pending=False):
            break
    bridge = UiBridge(CONFIG, store=e.store)
    for theme in ("dark", "light"):
        widgets.CURRENT.update(apply_theme(qapp, theme))
        win = MainWindow(bridge, refresh_ms=60_000, interactive=False)
        for i, scr in enumerate(win.screens):           # every screen renders with real data
            win.nav.setCurrentRow(i)
            win.refresh()
            qapp.processEvents()
            assert win.stack.currentWidget() is scr
    assert len(win.screens) == 10
    assert win.nav_lbl.text().startswith("NAV")

    trades = win.screens[1]
    win.nav.setCurrentRow(1)
    win.refresh()
    assert trades.table.model_.rows, "the blotter should show the open trade"
    trades.table.selectRow(0)
    trades.exit_selected(0.5, reason="ui test")
    c = bridge.commands(1)[0]
    assert c["kind"] == "manual_exit" and c["body"]["fraction"] == 0.5
    assert win.exit_all_book()
    assert bridge.commands(1)[0]["kind"] == "exit_all"
    assert win.kill(flatten=False)
    assert bridge.commands(1)[0]["kind"] == "kill"
    assert e.store.get("external_kill")["handled"] is False
    e.process_commands()
    win.refresh()
    assert "kill" in win.statusBar().currentMessage().lower() or win.snap["kill_switches"]
    win.close()


def test_indian_number_format():
    from algotrader.ui.widgets import inr
    assert inr(12345678) == "₹1,23,45,678"
    assert inr(-1500.5, 2) == "-₹1,500.50"
    assert inr(None) == "-"


def test_engine_launcher_command_and_start(tmp_path, monkeypatch):
    """The UI's Start-engine button runs `algotrader engine --mode paper` (never live)."""
    import subprocess

    from algotrader.ui import launcher
    cmd = launcher.engine_command(CONFIG)
    assert cmd[-5:] == ["engine", "--mode", "paper", "--config", str(CONFIG.resolve())]
    seen = {}

    class FakePopen:
        def __init__(self, args, **kw):
            seen["args"], seen["kw"] = args, kw

        def poll(self):
            return None
    monkeypatch.setattr(subprocess, "Popen", FakePopen)
    launcher.start_engine(CONFIG)
    assert seen["args"] == cmd and seen["kw"]["cwd"] == str(CONFIG.resolve().parent)


def test_crash_guard_keeps_errors(tmp_path, monkeypatch):
    from algotrader.crashlog import run_guarded
    monkeypatch.chdir(tmp_path)

    def boom():
        raise RuntimeError("engine exploded")
    assert run_guarded(boom, console=True) == 1
    assert "engine exploded" in (tmp_path / "crash.log").read_text()
    assert run_guarded(lambda: 0) == 0
