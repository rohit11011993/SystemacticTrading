"""Main window (PRD s.12).

* The kill-switch and exit-all controls are on screen from every page (FR-12.2) and reachable
  by keyboard (FR-8.5, FR-9.4, FR-12.5).
* Destructive actions show the consequence in rupees before confirming; whole-book actions need
  a second, typed confirmation (FR-12.3, PRD s.8).
* The blotter refreshes every second and shows data age (FR-8.1).
* Alerts reach the operator as desktop notifications where the OS supports them (FR-12.6).
* Theme, window geometry, current screen and table layouts are saved (FR-12.5).

Closing the window never stops the engine: the UI only reads published state and queues
commands.
"""

from __future__ import annotations

import sys
from typing import Any

from PySide6.QtCore import QByteArray, QSettings, Qt, QTimer
from PySide6.QtGui import QAction, QKeySequence
from PySide6.QtWidgets import (QApplication, QFrame, QHBoxLayout, QLabel, QListWidget, QMainWindow, QPushButton,
                               QStackedWidget, QStyle, QSystemTrayIcon, QVBoxLayout, QWidget)

from . import widgets
from .bridge import UiBridge
from .screens import (AuditScreen, DashboardScreen, InstrumentsScreen, KillSwitchScreen, OrdersScreen,
                      ResearchScreen, RiskScreen, Screen, SettingsScreen, StrategiesScreen, TradesScreen)
from .theme import LADDER, apply_theme
from .widgets import StateBadge, ask_text, confirm, inr, typed_confirm

SCREENS = (DashboardScreen, TradesScreen, KillSwitchScreen, StrategiesScreen, InstrumentsScreen, RiskScreen,
           OrdersScreen, ResearchScreen, AuditScreen, SettingsScreen)


class MainWindow(QMainWindow):
    def __init__(self, bridge: UiBridge, refresh_ms: int = 1000, interactive: bool = True):
        super().__init__()
        self.bridge = bridge
        self.interactive = interactive        # tests run without modal dialogs
        self.snap: dict[str, Any] = bridge.snapshot()
        self._seen_alerts: set[str] = set()
        self._engine_proc = None          # engine started from this window, if any
        self._last_cmd_id = max((c["id"] for c in bridge.commands(1)), default=0)
        self.setWindowTitle("AlgoTrader")
        self.resize(1400, 860)

        central = QWidget()
        root = QVBoxLayout(central)
        root.setContentsMargins(0, 0, 0, 0)
        root.setSpacing(0)
        root.addWidget(self._top_bar())
        body = QHBoxLayout()
        body.setContentsMargins(0, 0, 0, 0)
        self.nav = QListWidget()
        self.nav.setObjectName("Nav")
        self.nav.setFixedWidth(170)
        self.stack = QStackedWidget()
        self.screens: list[Screen] = []
        for i, cls in enumerate(SCREENS):
            scr = cls(self)
            self.screens.append(scr)
            self.stack.addWidget(scr)
            self.nav.addItem(f"{scr.title}")
            self.nav.item(i).setToolTip(f"Ctrl+{(i + 1) % 10}")
        self.nav.currentRowChanged.connect(self._goto)
        body.addWidget(self.nav)
        inner = QWidget()
        il = QVBoxLayout(inner)
        il.setContentsMargins(10, 10, 10, 10)
        il.addWidget(self.stack)
        body.addWidget(inner, 1)
        root.addLayout(body, 1)
        self.setCentralWidget(central)
        self.statusBar().showMessage("Attached to the engine's state database. Closing this window does not stop "
                                     "the engine or its risk controls.")

        self._shortcuts()
        self.tray = None
        if QSystemTrayIcon.isSystemTrayAvailable():
            self.tray = QSystemTrayIcon(self.style().standardIcon(QStyle.SP_MessageBoxWarning), self)
            self.tray.show()
        self._restore_settings()
        self.timer = QTimer(self)
        self.timer.timeout.connect(self.refresh)
        self.timer.start(refresh_ms)
        self.refresh()

    # -- layout ----------------------------------------------------------------------------
    def _top_bar(self) -> QWidget:
        bar = QFrame()
        bar.setObjectName("TopBar")
        lay = QHBoxLayout(bar)
        lay.setContentsMargins(12, 6, 12, 6)
        title = QLabel("<b>AlgoTrader</b>")
        self.mode = StateBadge()
        self.engine = StateBadge()
        self.ladder = StateBadge()
        self.nav_lbl = QLabel()
        self.day_lbl = QLabel()
        self.age_lbl = QLabel()
        self.age_lbl.setObjectName("Muted")
        self.start_btn = QPushButton("\u25b6 Start engine")
        self.start_btn.setToolTip("Start the paper-trading engine in its own window. Live trading is "
                                  "started from the command line, never from here.")
        self.start_btn.clicked.connect(self.start_engine)
        self.start_btn.setVisible(False)
        for w in (title, self.mode, self.engine, self.start_btn, self.ladder, self.nav_lbl, self.day_lbl,
                  self.age_lbl):
            lay.addWidget(w)
        lay.addStretch(1)
        self.exit_all_btn = QPushButton("EXIT ALL")
        self.exit_all_btn.setObjectName("Danger")
        self.exit_all_btn.setToolTip("Close every open trade at market (Ctrl+Shift+X)")
        self.exit_all_btn.clicked.connect(self.exit_all_book)
        self.flatten_btn = QPushButton("FLATTEN + HALT")
        self.flatten_btn.setObjectName("Danger")
        self.flatten_btn.setToolTip("System kill switch with flatten (Ctrl+Shift+F)")
        self.flatten_btn.clicked.connect(lambda: self.kill(flatten=True))
        self.kill_btn = QPushButton("KILL")
        self.kill_btn.setObjectName("Kill")
        self.kill_btn.setToolTip("System kill switch: block all new orders (Ctrl+K). Exits stay allowed.")
        self.kill_btn.clicked.connect(lambda: self.kill(flatten=False))
        for b in (self.exit_all_btn, self.flatten_btn, self.kill_btn):
            lay.addWidget(b)
        return bar

    def _shortcuts(self) -> None:
        def add(seq: str, fn) -> None:
            act = QAction(self)
            act.setShortcut(QKeySequence(seq))
            act.setShortcutContext(Qt.ApplicationShortcut)
            act.triggered.connect(fn)
            self.addAction(act)
        add("Ctrl+K", lambda: self.kill(False))
        add("Ctrl+Shift+F", lambda: self.kill(True))
        add("Ctrl+Shift+X", self.exit_all_book)
        add("Ctrl+E", self._exit_selected)
        add("F5", self.refresh)
        add("Ctrl+T", lambda: self.set_theme("dark" if QSettings().value("theme", "light") == "light" else "light"))
        for i in range(len(SCREENS)):
            add(f"Ctrl+{(i + 1) % 10}", lambda i=i: self.nav.setCurrentRow(i))

    def _goto(self, row: int) -> None:
        self.stack.setCurrentIndex(row)
        self.screens[row].refresh(self.snap)

    # -- refresh loop ----------------------------------------------------------------------
    def refresh(self) -> None:
        try:
            self.snap = self.bridge.snapshot()
        except Exception as exc:  # noqa: BLE001 - never let a read error kill the UI
            self.status(f"cannot read engine state: {exc}")
            return
        s = self.snap
        self.mode.set_state("warn" if s["mode"] == "live" else "neutral", str(s["mode"]).upper())
        alive, hb = self.bridge.engine_status()
        starting = self._engine_proc is not None and self._engine_proc.poll() is None and not alive
        if self._engine_proc is not None and self._engine_proc.poll() not in (None, 0):
            code = self._engine_proc.returncode
            self._engine_proc = None
            self.status(f"the engine stopped with exit code {code}; see crash.log in {self.bridge.base}")
        self.engine.set_state("ok" if alive else "warn" if starting else "bad",
                              "ENGINE OK" if alive else "ENGINE STARTING..." if starting else "ENGINE OFFLINE")
        self.engine.setToolTip("Commands are queued until the engine runs" if not alive else "")
        self.start_btn.setVisible(not alive and not starting)
        self.ladder.set_state(LADDER.get(s["ladder"], "ok"), f"DD {s['ladder']}")
        self.nav_lbl.setText(f"NAV <b>{inr(s['nav'])}</b>")
        dp = s["day_pnl"]
        colour = widgets.CURRENT["ok" if dp > 0 else "bad" if dp < 0 else "text"]
        self.day_lbl.setText(f"Day <b style='color:{colour}'>{inr(dp)}</b>")
        self.age_lbl.setText(f"engine heartbeat {widgets.age(hb)} | state as of {widgets.ts_short(s.get('ts'))}")
        current = self.stack.currentWidget()
        if isinstance(current, Screen):
            current.refresh(s)
        self._notify(s, alive)
        self._command_results()

    def _notify(self, s: dict[str, Any], alive: bool) -> None:
        st = QSettings()
        on = lambda k: str(st.value(k, "true")).lower() == "true"  # noqa: E731
        events: list[tuple[str, str]] = []
        if on("alert_kill"):
            events += [(f"ks:{k['level']}:{k['scope']}:{k['action']}",
                        f"Kill switch {k['level']}:{k['scope']} {k['action']} - {k['reason']}") for k in s["kill_switches"]]
        if on("alert_red"):
            events += [(f"red:{t['trade_id']}", f"OFF-THESIS: {t['strategy']} {t['instrument']} - {t.get('why')}")
                       for t in s["trades"] if t.get("alignment") == "OFF_THESIS"]
        if on("alert_recon") and s.get("health", {}).get("reconciled") is False:
            events.append(("recon", "Position mismatch with the broker: new entries blocked"))
        if on("alert_limit") and s["margin_cap_pct"] and s["margin_util_pct"] >= 0.8 * s["margin_cap_pct"]:
            events.append(("limit:margin", f"Margin utilisation {s['margin_util_pct']:.1f}% near the cap"))
        if on("alert_engine") and not alive and self.bridge.engine_status()[1] is not None:
            events.append(("engine", "Engine heartbeat lost"))
        events += [(f"alert:{a}", a) for a in s.get("alerts", [])]
        for key, msg in events:
            if key not in self._seen_alerts:
                self._seen_alerts.add(key)
                self.status(msg)
                if self.tray:
                    self.tray.showMessage("AlgoTrader", msg, QSystemTrayIcon.Warning, 8000)

    def _command_results(self) -> None:
        for c in reversed(self.bridge.commands(20)):
            if c["id"] > self._last_cmd_id and c["status"] != "PENDING":
                self._last_cmd_id = c["id"]
                self.status(f"{c['kind']}: {c['status']} - {c['result']}")

    def status(self, msg: str) -> None:
        self.statusBar().showMessage(msg, 15000)

    # -- actions ---------------------------------------------------------------------------
    def start_engine(self) -> bool:
        """Start the paper engine (FR-13.6 normally runs it as a service; this is the manual path)."""
        if not self.confirm_action("Start engine", "Start the paper-trading engine?",
                                   "It opens in its own window. Keep that window open while you use AlgoTrader; "
                                   "closing it stops the engine (open trades keep their stops).", danger=False):
            return False
        try:
            from .launcher import start_engine
            self._engine_proc = start_engine(self.bridge.root)
        except OSError as exc:
            self.status(f"could not start the engine: {exc}")
            return False
        self.status("engine starting... the badge turns green within a few seconds")
        self.refresh()
        return True

    def confirm_action(self, title: str, text: str, detail: str = "", danger: bool = True) -> bool:
        return True if not self.interactive else confirm(self, title, text, detail, danger)

    def confirm_exit(self, trades: list[dict[str, Any]], fraction: float, label: str, reason: str | None = None,
                     strategy: str | None = None, instrument: str | None = None) -> bool:
        """Exit with a confirmation that shows estimated charges and slippage (PRD s.8)."""
        if not trades:
            self.status("nothing to exit")
            return False
        cost = self.bridge.estimate_exit_cost(trades, fraction)
        pnl = sum(t.get("pnl_net") or 0.0 for t in trades) * fraction
        text = f"Exit {fraction:.0%} of {label} ({len(trades)} trade(s)) at market with protection?"
        detail = (f"PnL realised now: about {inr(pnl)}\nEstimated charges + slippage: {inr(cost)}\n"
                  "Exits go through the risk gateway as risk-reducing orders and work while entries are blocked.")
        if not self.confirm_action("Confirm exit", text, detail):
            return False
        if reason is None:
            reason = "" if not self.interactive else (ask_text(self, "Exit reason", "Optional reason:", False) or "")
        if strategy or instrument:
            self.bridge.exit_all(strategy, instrument, reason)
        else:
            for t in trades:
                self.bridge.manual_exit(t["trade_id"], fraction, reason)
        self.status(f"exit queued: {label}")
        return True

    def exit_all_book(self) -> bool:
        trades = self.snap["trades"]
        if not trades:
            self.status("no open trades")
            return False
        cost = self.bridge.estimate_exit_cost(trades)
        pnl = sum(t.get("pnl_net") or 0.0 for t in trades)
        if not self.confirm_action("EXIT ALL", f"Close ALL {len(trades)} open trades at market?",
                                   f"PnL realised: about {inr(pnl)}\nEstimated charges + slippage: {inr(cost)}"):
            return False
        if self.interactive and not typed_confirm(self, "EXIT ALL", "Second confirmation for a whole-book exit.",
                                                  "EXIT ALL"):
            return False
        self.bridge.exit_all(reason="operator whole-book exit")
        self.status("whole-book exit queued")
        return True

    def kill(self, flatten: bool = False) -> bool:
        trades = self.snap["trades"]
        if flatten:
            cost = self.bridge.estimate_exit_cost(trades)
            text = "Trip the SYSTEM kill switch and FLATTEN every position?"
            detail = (f"{len(trades)} open trades will be closed at market. Estimated charges + slippage: {inr(cost)}."
                      "\nThe system stays locked until a manual reset.")
        else:
            text = "Trip the SYSTEM kill switch?"
            detail = "All new orders are blocked within a second. Exits and hedges stay allowed. Reset is manual."
        if not self.confirm_action("Kill switch", text, detail):
            return False
        if flatten and self.interactive and not typed_confirm(self, "FLATTEN", "Second confirmation.", "FLATTEN"):
            return False
        self.bridge.kill(flatten=flatten, reason="UI kill button" + (" (flatten)" if flatten else ""))
        self.status("SYSTEM KILL SWITCH requested")
        return True

    def _exit_selected(self) -> None:
        self.nav.setCurrentRow(1)
        trades = self.screens[1]
        if isinstance(trades, TradesScreen):
            trades.exit_selected(1.0)

    # -- settings --------------------------------------------------------------------------
    def set_theme(self, name: str) -> None:
        QSettings().setValue("theme", name)
        widgets.CURRENT.clear()
        widgets.CURRENT.update(apply_theme(QApplication.instance(), name))
        self.refresh()

    def _restore_settings(self) -> None:
        s = QSettings()
        if (g := s.value("geometry")) is not None:
            self.restoreGeometry(g if isinstance(g, QByteArray) else QByteArray(g))
        for scr in self.screens:
            for name, table in scr.tables().items():
                st = s.value(f"table/{name}")
                if st is not None:
                    table.sized = True
                    table.horizontalHeader().restoreState(st if isinstance(st, QByteArray) else QByteArray(st))
        self.nav.setCurrentRow(int(s.value("screen", 0) or 0))

    def closeEvent(self, event) -> None:  # noqa: N802 - Qt naming
        s = QSettings()
        s.setValue("geometry", self.saveGeometry())
        s.setValue("screen", self.nav.currentRow())
        for scr in self.screens:
            for name, table in scr.tables().items():
                s.setValue(f"table/{name}", table.horizontalHeader().saveState())
        super().closeEvent(event)


def run_ui(config_root: str, refresh_ms: int = 1000) -> int:  # pragma: no cover - interactive
    app = QApplication.instance() or QApplication(sys.argv)
    app.setOrganizationName("AlgoTrader")
    app.setApplicationName("AlgoTrader")
    theme = str(QSettings().value("theme", "light"))
    widgets.CURRENT.update(apply_theme(app, theme))
    win = MainWindow(UiBridge(config_root), refresh_ms)
    win.show()
    return app.exec()
