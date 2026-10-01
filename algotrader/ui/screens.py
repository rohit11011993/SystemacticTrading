"""The ten screens of PRD s.12. Each screen has ``refresh(snapshot)``; actions go through the
main window, which owns confirmations and the bridge."""

from __future__ import annotations

import json
from datetime import date
from typing import TYPE_CHECKING, Any

from PySide6.QtCore import QDate, QSettings, Qt, QThread, Signal
from PySide6.QtWidgets import (QCheckBox, QComboBox, QDateEdit, QDoubleSpinBox, QFormLayout, QGridLayout,
                               QGroupBox, QHBoxLayout, QLabel, QLineEdit, QListWidget, QMessageBox,
                               QPlainTextEdit, QPushButton, QSplitter, QVBoxLayout, QWidget)

from .theme import ALIGNMENT, LADDER
from .widgets import (Col, DataTable, Gauge, KpiTile, LineChart, StateBadge, age, ask_text, hbox, inr, num,
                      pct, ts_short)

if TYPE_CHECKING:
    from .main_window import MainWindow


def _pnl_kind(row: dict, key: str = "pnl_net") -> str | None:
    v = row.get(key)
    return None if v is None else ("ok" if v > 0 else "bad" if v < 0 else None)


def _align_kind(row: dict) -> str | None:
    return ALIGNMENT.get(row.get("alignment", ""), (None, ""))[0]


def _align_text(v: Any) -> str:
    kind, label = ALIGNMENT.get(str(v), (None, str(v)))
    icon = {"ok": "●", "warn": "▲", "bad": "✖"}.get(kind or "", "○")
    return f"{icon} {label}"


class Screen(QWidget):
    title = "screen"

    def __init__(self, win: "MainWindow"):
        super().__init__()
        self.win = win
        self.bridge = win.bridge

    def refresh(self, snap: dict[str, Any]) -> None:  # pragma: no cover - overridden
        pass

    def tables(self) -> dict[str, DataTable]:
        """Tables whose header layout is saved between sessions (FR-12.5)."""
        return {}


# --------------------------------------------------------------------------------------------
class DashboardScreen(Screen):
    """NAV, PnL, drawdown state, margin, exposure, health, regime, alerts (PRD s.8 portfolio panel)."""

    title = "Dashboard"

    def __init__(self, win):
        super().__init__(win)
        lay = QVBoxLayout(self)
        grid = QGridLayout()
        self.t_nav, self.t_day = KpiTile("Net asset value"), KpiTile("Day PnL")
        self.t_week, self.t_month = KpiTile("Week PnL"), KpiTile("Month PnL")
        self.t_dd, self.t_open = KpiTile("Drawdown from peak"), KpiTile("Open trades")
        for i, t in enumerate((self.t_nav, self.t_day, self.t_week, self.t_month, self.t_dd, self.t_open)):
            grid.addWidget(t, 0, i)
        lay.addLayout(grid)

        mid = QHBoxLayout()
        limits = QGroupBox("Limit usage")
        ll = QVBoxLayout(limits)
        self.g_margin, self.g_gross = Gauge("Margin utilisation"), Gauge("Gross exposure")
        self.g_net, self.g_risk = Gauge("Beta-adjusted net exposure"), Gauge("Open risk vs book budget")
        for g in (self.g_margin, self.g_gross, self.g_net, self.g_risk):
            ll.addWidget(g)
        mid.addWidget(limits, 2)

        health = QGroupBox("Health")
        hl = QFormLayout(health)
        self.h = {k: StateBadge() for k in ("engine", "data", "broker", "session", "gateway", "reconciled",
                                            "ladder", "kill")}
        for k, label in (("engine", "Engine"), ("data", "Data feed"), ("broker", "Broker"),
                         ("session", "Broker session"), ("gateway", "Risk gateway"),
                         ("reconciled", "Reconciliation"), ("ladder", "Drawdown state"), ("kill", "Kill switches")):
            hl.addRow(label, self.h[k])
        mid.addWidget(health, 1)

        regime = QGroupBox("Market regime")
        rl = QVBoxLayout(regime)
        self.regime_badge = StateBadge()
        self.regime_info = QLabel()
        self.regime_info.setObjectName("Muted")
        self.perm = QLabel()
        self.perm.setWordWrap(True)
        rl.addWidget(self.regime_badge)
        rl.addWidget(self.regime_info)
        rl.addWidget(self.perm)
        rl.addStretch(1)
        mid.addWidget(regime, 1)
        lay.addLayout(mid)

        bottom = QSplitter(Qt.Horizontal)
        eq = QGroupBox("Equity")
        el = QVBoxLayout(eq)
        self.chart = LineChart()
        el.addWidget(self.chart)
        bottom.addWidget(eq)
        al = QGroupBox("Alerts")
        all_ = QVBoxLayout(al)
        self.alerts = QListWidget()
        all_.addWidget(self.alerts)
        bottom.addWidget(al)
        bottom.setSizes([700, 400])
        lay.addWidget(bottom, 1)

    def refresh(self, snap):
        cap = snap["capital"] or 1
        self.t_nav.set(inr(snap["nav"]), f"capital {inr(snap['capital'])}")
        for tile, key in ((self.t_day, "day_pnl"), (self.t_week, "week_pnl"), (self.t_month, "month_pnl")):
            v = snap[key]
            tile.set(inr(v), pct(v / cap * 100, 2) + " of capital", "ok" if v > 0 else "bad" if v < 0 else None)
        dd = snap["drawdown_pct"]
        self.t_dd.set(pct(dd, 2), f"peak {inr(snap['peak'])}", LADDER.get(snap["ladder"], "ok"))
        trades = snap["trades"]
        reds = sum(1 for t in trades if t.get("alignment") == "OFF_THESIS")
        self.t_open.set(str(len(trades)), f"{reds} off-thesis" if reds else "all in line or drifting",
                        "bad" if reds else None)
        nav = snap["nav"] or 1
        self.g_margin.set(snap["margin_util_pct"], snap["margin_cap_pct"],
                          f"{pct(snap['margin_util_pct'])} ({inr(snap['margin_used'])})")
        self.g_gross.set(snap["gross"], snap["gross_cap"] or 1, f"{inr(snap['gross'])} ({snap['gross'] / nav:.0%} NAV)")
        self.g_net.set(abs(snap["net_beta"]), snap["net_cap"] or 1, inr(snap["net_beta"]))
        self.g_risk.set(snap["book_risk"], snap["book_risk_budget"] or 1,
                        f"{inr(snap['book_risk'])} of {inr(snap['book_risk_budget'])}")

        alive, hb = self.bridge.engine_status()
        self.h["engine"].set_state("ok" if alive else "bad", f"running ({age(hb)})" if alive else
                                   f"offline (last {age(hb)})" if hb else "not running - click Start engine")
        hl = snap.get("health", {})
        for k, ok_t, bad_t in (("data", "connected", "DISCONNECTED"), ("broker", "connected", "DISCONNECTED"),
                               ("session", "valid", "NO SESSION"), ("gateway", "enforcing", "DOWN - fail closed"),
                               ("reconciled", "positions match", "MISMATCH - entries blocked")):
            v = hl.get(k)
            self.h[k].set_state("neutral" if v is None else "ok" if v else "bad", "unknown" if v is None
                                else ok_t if v else bad_t)
        self.h["ladder"].set_state(LADDER.get(snap["ladder"], "ok"), snap["ladder"])
        ks = snap["kill_switches"]
        blocking = [k for k in ks if k["action"] not in ("NONE", "WARN")]
        self.h["kill"].set_state("bad" if blocking else "warn" if ks else "ok",
                                 f"{len(blocking)} blocking, {len(ks) - len(blocking)} warning" if ks else "none active")

        rg = snap.get("regime", {})
        name = rg.get("name")
        self.regime_badge.set_state("neutral" if not name else "warn" if "STRESSED" in name else "ok",
                                    name or "not yet classified")
        self.regime_info.setText(f"Nifty ER(10) {num(rg.get('er'))}   India VIX pct {pct((rg.get('vix_pct') or 0) * 100, 0)}"
                                 + (f"\nPending: {rg['pending']}" if rg.get("pending") else ""))
        self.perm.setText("New-entry size multipliers: " + ", ".join(
            f"{k.split('_')[0]} {v:g}x" for k, v in (rg.get("permissions") or {}).items()))

        eq = self.bridge.equity()
        self.chart.set_series([d for d, _ in eq], [("equity", [e for _, e in eq], "accent")])
        self.alerts.clear()
        for a in reversed(snap.get("alerts", [])):
            self.alerts.addItem(f"⚠ {a}")
        for k in ks:
            self.alerts.addItem(f"✖ {k['level']}:{k['scope']} {k['action']} - {k['reason']}")
        if not snap.get("alerts") and not ks:
            self.alerts.addItem("No alerts")


# --------------------------------------------------------------------------------------------
class TradesScreen(Screen):
    """Active-trade blotter with alignment indicator and exit controls (PRD s.8)."""

    title = "Active trades"

    def __init__(self, win):
        super().__init__(win)
        lay = QVBoxLayout(self)
        cols = [
            Col("strategy", "Strategy"), Col("instrument", "Instrument"), Col("side", "Side"),
            Col("qty", "Qty", numeric=True), Col("entry_time", "Entry time", ts_short),
            Col("entry", "Entry", num, numeric=True), Col("last", "Last", num, numeric=True),
            Col("stop", "Stop", num, numeric=True),
            Col("pnl_net", "PnL (net)", inr, _pnl_kind, True),
            Col("pnl_pct_nav", "% NAV", lambda v: pct(v, 2), _pnl_kind, True),
            Col("r_multiple", "R", num, lambda r: _pnl_kind(r, "r_multiple"), True),
            Col("initial_risk", "Initial risk", inr, numeric=True), Col("risk_now", "Risk now", inr, numeric=True),
            Col("dist_to_stop", "To stop", num, numeric=True), Col("mfe", "MFE", inr, numeric=True),
            Col("mae", "MAE", inr, numeric=True), Col("days", "Days", numeric=True),
            Col("alignment", "Alignment", _align_text, _align_kind),
            Col("data_age_s", "Data age", age, numeric=True), Col("status", "Status"),
        ]
        self.table = DataTable(cols, key="trade_id")
        self.table.selectionModel().selectionChanged.connect(lambda *_: self._show_detail())
        btns = []
        for text, fn in (("Exit at market", lambda: self.exit_selected(1.0)),
                         ("Exit 25%", lambda: self.exit_selected(0.25)),
                         ("Exit 50%", lambda: self.exit_selected(0.5)),
                         ("Partial (custom)...", self.exit_custom),
                         ("Tighten stop...", self.adjust_stop),
                         ("Exit strategy...", self.exit_strategy),
                         ("Exit instrument...", self.exit_instrument)):
            b = QPushButton(text)
            b.clicked.connect(fn)
            btns.append(b)
        btns[0].setObjectName("Danger")
        btns[0].setToolTip("Ctrl+E")
        self.filter = QLineEdit()
        self.filter.setPlaceholderText("Filter trades...")
        self.filter.textChanged.connect(self.table.set_filter)
        top = hbox(*btns)
        top.addWidget(self.filter)
        lay.addLayout(top)
        split = QSplitter(Qt.Vertical)
        split.addWidget(self.table)
        self.detail = QPlainTextEdit()
        self.detail.setReadOnly(True)
        split.addWidget(self.detail)
        split.setSizes([500, 180])
        lay.addWidget(split, 1)

    def tables(self):
        return {"trades": self.table}

    def refresh(self, snap):
        rows = []
        for r in snap["trades"]:
            rr = dict(r)
            rr["_tooltip"] = "\n".join([f"Why: {r.get('why') or '-'}", f"Entry reason: {r.get('reason', '')}"])
            rows.append(rr)
        self.table.set_rows(rows)
        self._show_detail()

    def _show_detail(self):
        r = self.table.selected_row()
        if not r:
            self.detail.setPlainText("Select a trade to see legs, alignment checks and history.")
            return
        lines = [f"{r['trade_id']}  {r['strategy']}  {r['side']}  status {r.get('status')}"
                 + ("   [exit order working]" if r.get("working_exit") else ""),
                 f"Entry reason: {r.get('reason', '')}",
                 f"Alignment: {_align_text(r.get('alignment'))}  {r.get('why') or ''}", "", "Legs:"]
        for leg in r.get("legs_detail", []):
            lines.append(f"  {leg['symbol']:<40} qty {leg['qty']:>8}  avg {num(leg['avg'])}  last {num(leg.get('last'))}")
        lines += ["", "Alignment history (latest last):"]
        for d, state, why in r.get("alignment_history", []):
            lines.append(f"  {d}  {_align_text(state):<16} {'; '.join(why)}")
        self.detail.setPlainText("\n".join(lines))

    # actions
    def exit_selected(self, fraction: float, reason: str | None = None):
        r = self.table.selected_row()
        if not r:
            QMessageBox.information(self, "Exit", "Select a trade first.")
            return
        self.win.confirm_exit([r], fraction, f"{r['strategy']} {r['instrument']} {r['side']}", reason)

    def exit_custom(self):
        r = self.table.selected_row()
        if not r:
            return
        from PySide6.QtWidgets import QInputDialog
        v, ok = QInputDialog.getDouble(self, "Partial exit", "Percent of the position to close:", 30, 1, 100, 0)
        if ok:
            self.exit_selected(v / 100.0)

    def adjust_stop(self):
        r = self.table.selected_row()
        if not r or r.get("stop") is None:
            QMessageBox.information(self, "Stop", "Select a single-leg trade with a stop.")
            return
        from PySide6.QtWidgets import QInputDialog
        v, ok = QInputDialog.getDouble(self, "Tighten stop",
                                       "New stop (can only move toward lower risk; wider is rejected):",
                                       float(r["stop"]), 0, 1e9, 2)
        if ok:
            self.bridge.adjust_stop(r["trade_id"], v)
            self.win.status(f"stop change for {r['trade_id']} queued")

    def exit_strategy(self):
        sids = sorted({r["strategy"] for r in self.table.model_.rows})
        if not sids:
            return
        from PySide6.QtWidgets import QInputDialog
        sid, ok = QInputDialog.getItem(self, "Exit all for strategy", "Strategy:", sids, 0, False)
        if ok:
            self.win.confirm_exit([r for r in self.table.model_.rows if r["strategy"] == sid], 1.0,
                                  f"all {sid} trades", strategy=sid)

    def exit_instrument(self):
        keys = sorted({r["instrument"] for r in self.table.model_.rows})
        if not keys:
            return
        from PySide6.QtWidgets import QInputDialog
        key, ok = QInputDialog.getItem(self, "Exit all for instrument", "Instrument:", keys, 0, False)
        if ok:
            self.win.confirm_exit([r for r in self.table.model_.rows if r["instrument"] == key], 1.0,
                                  f"all trades on {key}", instrument=key)


# --------------------------------------------------------------------------------------------
class KillSwitchScreen(Screen):
    """Status of all four levels, manual triggers, reset workflow (PRD s.9)."""

    title = "Kill switches"

    def __init__(self, win):
        super().__init__(win)
        lay = QVBoxLayout(self)
        self.table = DataTable([
            Col("level", "Level"), Col("scope", "Scope"),
            Col("action", "Action", None, lambda r: "bad" if r["action"] in ("FLATTEN", "HALT", "BLOCK_ENTRIES")
                else "warn"),
            Col("reason", "Reason"), Col("ts", "Tripped", ts_short),
            Col("external_key", "External key", lambda v: "required" if v else "-"),
            Col("until", "Locked until", ts_short), Col("reset_requested", "Reset requested", ts_short),
        ], key="scope")
        lay.addWidget(QLabel("Active trips. Recovery is never automatic for drawdown, strategy hard-stop or "
                             "system trips; resets need a reason, a cooling-off delay and confirmation."))
        lay.addWidget(self.table, 1)

        trig = QGroupBox("Manual triggers")
        tl = QHBoxLayout(trig)
        self.level = QComboBox()
        self.level.addItems(["strategy", "instrument", "drawdown", "system"])
        self.scope = QComboBox()
        self.scope.setEditable(True)
        self.action = QComboBox()
        self.action.addItems(["BLOCK_ENTRIES", "REDUCE", "FLATTEN", "HALT"])
        self.level.currentTextChanged.connect(self._scopes)
        trip = QPushButton("Trip")
        trip.setObjectName("Danger")
        trip.clicked.connect(self.trip)
        for w in (QLabel("Level"), self.level, QLabel("Scope"), self.scope, QLabel("Action"), self.action, trip):
            tl.addWidget(w)
        tl.addStretch(1)
        lay.addWidget(trig)

        rs = QGroupBox("Reset selected trip")
        rl = QHBoxLayout(rs)
        req = QPushButton("1. Request reset (reason)...")
        req.clicked.connect(self.request_reset)
        conf = QPushButton("2. Confirm reset...")
        conf.clicked.connect(self.confirm_reset)
        rl.addWidget(req)
        rl.addWidget(conf)
        rl.addWidget(QLabel("RED/BLACK resets need a one-time code from the second key holder."))
        rl.addStretch(1)
        lay.addWidget(rs)
        drill = QLabel("Rehearse every level against the paper broker with:  algotrader drill")
        drill.setObjectName("Muted")
        lay.addWidget(drill)
        self._snap: dict[str, Any] = {}

    def tables(self):
        return {"killswitch": self.table}

    def _scopes(self, level: str):
        self.scope.clear()
        if level == "strategy":
            self.scope.addItems([s["id"] for s in self._snap.get("strategies", [])])
        elif level == "instrument":
            self.scope.addItems([i.key for i in self.bridge.registry.all() if i.tradable])
        elif level == "drawdown":
            self.scope.addItem("account")
        else:
            self.scope.addItem("system")

    def refresh(self, snap):
        first = not self._snap
        self._snap = snap
        if first:
            self._scopes(self.level.currentText())
        self.table.set_rows(snap["kill_switches"])

    def trip(self):
        level, scope, action = self.level.currentText(), self.scope.currentText().strip(), self.action.currentText()
        if not scope:
            return
        reason = ask_text(self, "Trip kill switch", f"Reason for {level}:{scope} {action}:")
        if reason is None:
            return
        if self.win.confirm_action("Trip kill switch", f"Trip {level}:{scope} with action {action}?",
                                   "FLATTEN/HALT will close affected positions at market."):
            self.bridge.trip(level, scope, action, reason)
            self.win.status(f"trip {level}:{scope} {action} queued")

    def request_reset(self):
        r = self.table.selected_row()
        if not r:
            return
        reason = ask_text(self, "Request reset", f"Written reason for resetting {r['level']}:{r['scope']}:")
        if reason:
            self.bridge.reset_request(r["level"], r["scope"], reason)
            self.win.status("reset requested; cooling-off started")

    def confirm_reset(self):
        r = self.table.selected_row()
        if not r:
            return
        code = None
        if r.get("external_key"):
            code = ask_text(self, "External key", "One-time code from the second key holder:")
            if code is None:
                return
        if self.win.confirm_action("Confirm reset", f"Reset {r['level']}:{r['scope']}?",
                                   "New entries in this scope will be allowed again.", danger=False):
            self.bridge.reset_confirm(r["level"], r["scope"], code)
            self.win.status("reset confirmation queued")


# --------------------------------------------------------------------------------------------
class StrategiesScreen(Screen):
    title = "Strategies"

    def __init__(self, win):
        super().__init__(win)
        lay = QVBoxLayout(self)
        self.table = DataTable([
            Col("id", "Strategy"), Col("version", "Ver"), Col("stage", "Stage"), Col("mode", "Mode"),
            Col("state", "State", None, lambda r: "bad" if r["blocked"] else "warn" if r["paused"] else "ok"),
            Col("pnl", "PnL", inr, lambda r: _pnl_kind(r, "pnl"), True), Col("open", "Open", numeric=True),
            Col("closed", "Closed", numeric=True), Col("hit_rate", "Hit rate", lambda v: pct(v * 100 if v is not None else None, 0), numeric=True),
            Col("risk_used", "Risk used", inr, numeric=True), Col("risk_cap", "Risk cap", inr, numeric=True),
            Col("risk_multiplier", "Risk mult", lambda v: num(v, "{:.2f}x"), numeric=True),
            Col("permission", "Regime mult", lambda v: num(v, "{:.2f}x"), numeric=True),
            Col("family", "Family"), Col("plugin_hash", "Plugin hash"),
        ], key="id")
        lay.addWidget(self.table, 1)
        row = QHBoxLayout()
        for text, fn in (("Pause", lambda: self._pause(True)), ("Resume", lambda: self._pause(False)),
                         ("Exit all for strategy", self._exit_all)):
            b = QPushButton(text)
            b.clicked.connect(fn)
            row.addWidget(b)
        row.addStretch(1)
        lay.addLayout(row)
        self.detail = QPlainTextEdit()
        self.detail.setReadOnly(True)
        self.detail.setMaximumHeight(170)
        lay.addWidget(self.detail)
        self.table.selectionModel().selectionChanged.connect(lambda *_: self._detail())

    def tables(self):
        return {"strategies": self.table}

    def refresh(self, snap):
        rows = []
        for s in snap["strategies"]:
            state = "BLOCKED" if s["blocked"] else "PAUSED" if s["paused"] else "active"
            rows.append({**s, "state": state, "_tooltip": "\n".join(s["blocked"]) or None})
        self.table.set_rows(rows)
        self._detail()

    def _detail(self):
        r = self.table.selected_row()
        if not r:
            self.detail.setPlainText("Select a strategy for parameters, instruments and block reasons.")
            return
        self.detail.setPlainText("\n".join([
            f"{r['id']} v{r['version']}  stage {r['stage']}  mode {r['mode']}",
            f"Instruments: {', '.join(r['instruments'])}",
            f"Parameters: {json.dumps(r['params'])}",
            "Blocked: " + ("; ".join(r["blocked"]) or "no")]))

    def _pause(self, paused: bool):
        r = self.table.selected_row()
        if r:
            self.bridge.pause(r["id"], paused)
            self.win.status(f"{'pause' if paused else 'resume'} {r['id']} queued")

    def _exit_all(self):
        r = self.table.selected_row()
        if r:
            trades = [t for t in self.win.snap["trades"] if t["strategy"] == r["id"]]
            self.win.confirm_exit(trades, 1.0, f"all {r['id']} trades", strategy=r["id"])


# --------------------------------------------------------------------------------------------
class InstrumentsScreen(Screen):
    title = "Instruments"

    def __init__(self, win):
        super().__init__(win)
        lay = QVBoxLayout(self)
        self.table = DataTable([
            Col("key", "Key"), Col("symbol", "Symbol"), Col("exchange", "Exch"), Col("type", "Type"),
            Col("lot_size", "Lot", numeric=True), Col("tick", "Tick", numeric=True),
            Col("margin_pct", "Margin %", numeric=True), Col("sector", "Sector"), Col("cluster", "Cluster"),
            Col("cost_profile", "Cost profile"),
            Col("banned", "F&O ban", lambda v: "BANNED" if v else "-", lambda r: "bad" if r["banned"] else None),
            Col("circuit", "Circuit", lambda v: "HIT" if v else "-", lambda r: "bad" if r["circuit"] else None),
            Col("last", "Last", num, numeric=True), Col("quote_ts", "Quote time", ts_short),
        ], key="key")
        lay.addWidget(QLabel("Instruments are configuration records (config/instruments.yaml). Daily flags "
                             "set here apply until the next master refresh."))
        lay.addWidget(self.table, 1)
        row = QHBoxLayout()
        for text, flag, val in (("Mark F&O ban", "banned", True), ("Clear ban", "banned", False),
                                ("Mark circuit", "circuit", True), ("Clear circuit", "circuit", False)):
            b = QPushButton(text)
            b.clicked.connect(lambda _=False, f=flag, v=val: self._flag(f, v))
            row.addWidget(b)
        row.addStretch(1)
        lay.addLayout(row)
        self.specs = QLabel()
        self.specs.setObjectName("Muted")
        lay.addWidget(self.specs)
        self.table.selectionModel().selectionChanged.connect(self._sel)

    def tables(self):
        return {"instruments": self.table}

    def refresh(self, snap):
        self.table.set_rows(self.bridge.instruments())

    def _sel(self, *_):
        r = self.table.selected_row()
        self.specs.setText("Contract specs: " + " | ".join(r["specs"]) if r else "")

    def _flag(self, flag: str, value: bool):
        r = self.table.selected_row()
        if r:
            self.bridge.set_instrument_flag(r["key"], **{flag: value})
            self.win.status(f"{r['key']} {flag}={value} queued")


# --------------------------------------------------------------------------------------------
class RiskScreen(Screen):
    """Profiles, effective limits per binding, usage gauges and change history (FR-7.4)."""

    title = "Risk & limits"

    def __init__(self, win):
        super().__init__(win)
        lay = QVBoxLayout(self)
        g = QHBoxLayout()
        self.gauges = {k: Gauge(t) for k, t in (("margin", "Margin"), ("gross", "Gross exposure"),
                                                 ("net", "Net exposure"), ("risk", "Book open risk"))}
        for w in self.gauges.values():
            g.addWidget(w)
        lay.addLayout(g)
        lay.addWidget(QLabel("Effective limits (strictest of global, asset class, instrument, strategy and "
                             "binding profile):"))
        self.table = DataTable([Col(k, t) for k, t in (
            ("strategy", "Strategy"), ("instrument", "Instrument"), ("profile", "Profile"),
            ("max_risk_per_trade_pct", "Risk/trade %"), ("max_position_pct_nav", "Position % NAV"),
            ("max_margin_util_pct", "Margin %"), ("max_adv_pct", "% ADV"), ("max_order_lots", "Max lots"),
            ("price_collar_pct", "Collar %"), ("max_cluster_risk_pct_nav", "Cluster risk %"),
            ("max_cost_to_atr_pct", "Cost/ATR %"), ("require_stop", "Stop req."),
            ("blocked_windows", "Blocked windows"), ("options", "Options"))])
        lay.addWidget(self.table, 1)

        box = QGroupBox("Propose a limit change (tightening applies now; loosening needs reason, "
                        "confirmation and a cooling-off)")
        bl = QHBoxLayout(box)
        self.lvl = QComboBox()
        self.lvl.addItems(["global", "strategies", "instruments", "asset_class", "profiles"])
        self.sc = QLineEdit()
        self.sc.setPlaceholderText("scope (e.g. S1_ADAPTIVE_TREND)")
        self.key = QComboBox()
        self.key.setEditable(True)
        self.key.addItems(sorted(k for k, v in self.bridge.cfg.risk.global_limits.items()
                                 if isinstance(v, (int, float)) and not isinstance(v, bool)))
        self.val = QDoubleSpinBox()
        self.val.setRange(0, 1e12)
        self.val.setDecimals(4)
        self.key.currentTextChanged.connect(self._prefill)
        self._prefill(self.key.currentText())
        b = QPushButton("Propose...")
        b.clicked.connect(self.propose)
        for w in (self.lvl, self.sc, self.key, self.val, b):
            bl.addWidget(w)
        lay.addWidget(box)
        self.pending = DataTable([Col("id", "#"), Col("level", "Level"), Col("scope", "Scope"), Col("key", "Key"),
                                  Col("old", "Old"), Col("new", "New"), Col("reason", "Reason"),
                                  Col("effective", "Effective", ts_short),
                                  Col("confirmed", "Confirmed", lambda v: "yes" if v else "NO")], key="id")
        self.pending.setMaximumHeight(130)
        lay.addWidget(QLabel("Pending loosening changes:"))
        lay.addWidget(self.pending)
        cb = QPushButton("Confirm selected change")
        cb.clicked.connect(self.confirm_change)
        lay.addLayout(hbox(cb))
        self._loaded = False

    def tables(self):
        return {"limits": self.table}

    def refresh(self, snap):
        nav = snap["nav"] or 1
        self.gauges["margin"].set(snap["margin_util_pct"], snap["margin_cap_pct"], pct(snap["margin_util_pct"]))
        self.gauges["gross"].set(snap["gross"], snap["gross_cap"] or 1, f"{snap['gross'] / nav:.0%} NAV")
        self.gauges["net"].set(abs(snap["net_beta"]), snap["net_cap"] or 1, f"{snap['net_beta'] / nav:.0%} NAV")
        self.gauges["risk"].set(snap["book_risk"], snap["book_risk_budget"] or 1, inr(snap["book_risk"]))
        if not self._loaded:
            self.table.set_rows(self.bridge.effective_limits())
            self._loaded = True
        self.pending.set_rows(snap.get("pending_limit_changes", []))

    def _prefill(self, key: str) -> None:
        """Start from the current global value so a change is a deliberate edit."""
        v = self.bridge.cfg.risk.global_limits.get(key)
        if isinstance(v, (int, float)) and not isinstance(v, bool):
            self.val.setValue(float(v))

    def propose(self):
        level, scope, key, value = self.lvl.currentText(), self.sc.text().strip() or None, \
            self.key.currentText().strip(), self.val.value()
        if not key or (level != "global" and not scope):
            QMessageBox.information(self, "Limit change", "Key and scope are required.")
            return
        reason = ask_text(self, "Limit change", f"Reason for {level}/{scope or '-'} {key} = {value}:")
        if reason is None:
            return
        self.bridge.propose_limit(level, scope, key, value, reason)
        self.win.status(f"limit change {key}={value} queued")

    def confirm_change(self):
        r = self.pending.selected_row()
        if r and self.win.confirm_action("Confirm loosening", f"Confirm loosening #{r['id']} {r['key']} "
                                         f"{r['old']} -> {r['new']}?",
                                         f"It takes effect at {ts_short(r['effective'])}."):
            self.bridge.confirm_limit(r["id"])


# --------------------------------------------------------------------------------------------
class OrdersScreen(Screen):
    title = "Orders & fills"

    def __init__(self, win):
        super().__init__(win)
        lay = QVBoxLayout(self)
        self.filter = QLineEdit()
        self.filter.setPlaceholderText("Filter orders (symbol, status, strategy, reason)...")
        lay.addWidget(self.filter)
        bad = {"REJECTED", "EXPIRED"}
        self.table = DataTable([
            Col("updated", "Updated", ts_short), Col("strategy_id", "Strategy"), Col("algo_tag", "Algo tag"),
            Col("symbol", "Symbol"), Col("side", "Side"), Col("purpose", "Purpose"), Col("order_type", "Type"),
            Col("qty", "Qty", numeric=True), Col("filled_qty", "Filled", numeric=True),
            Col("avg_fill_price", "Avg price", num, numeric=True),
            Col("limit_price", "Limit", num, numeric=True), Col("trigger_price", "Trigger", num, numeric=True),
            Col("status", "Status", None, lambda r: "bad" if r["status"] in bad else
                "ok" if r["status"] == "FILLED" else None),
            Col("slippage", "Slippage", inr, lambda r: "bad" if (r.get("slippage") or 0) > 0 else None, True),
            Col("reject_reason", "Rejection"), Col("client_ref", "Client ref"),
            Col("broker_order_id", "Broker id")], key="client_ref")
        self.filter.textChanged.connect(self.table.set_filter)
        lay.addWidget(self.table, 1)
        lay.addWidget(QLabel("Operator commands:"))
        self.cmds = DataTable([Col("id", "#", numeric=True), Col("ts", "Time", ts_short), Col("actor", "Actor"),
                               Col("kind", "Command"), Col("status", "Status", None,
                                                           lambda r: "bad" if r["status"] == "FAILED" else
                                                           "warn" if r["status"] == "PENDING" else "ok"),
                               Col("result", "Result")], key="id")
        self.cmds.setMaximumHeight(170)
        lay.addWidget(self.cmds)

    def tables(self):
        return {"orders": self.table}

    def refresh(self, snap):
        self.table.set_rows(self.bridge.orders())
        self.cmds.set_rows(self.bridge.commands(50))


# --------------------------------------------------------------------------------------------
class BacktestWorker(QThread):
    done = Signal(dict)
    failed = Signal(str)

    def __init__(self, config_root: str, kwargs: dict[str, Any], start: date, end: date):
        super().__init__()
        self.config_root, self.kwargs, self.start_, self.end_ = config_root, kwargs, start, end

    def run(self):  # pragma: no cover - exercised manually / long-running
        try:
            from ..app import build_engine
            from ..backtest import format_summary, run_backtest
            engine = build_engine(self.config_root, **self.kwargs)
            res = run_backtest(engine, self.start_, self.end_)
            self.done.emit({"summary": format_summary(res.summary),
                            "labels": [str(d.date()) for d in res.equity.index],
                            "equity": [float(v) for v in res.equity.values],
                            "trades": res.trades.fillna("").to_dict("records")})
        except Exception as exc:  # noqa: BLE001
            self.failed.emit(str(exc))


class ResearchScreen(Screen):
    """Run backtests from the UI and compare them with the live/paper equity curve."""

    title = "Research"

    def __init__(self, win):
        super().__init__(win)
        lay = QVBoxLayout(self)
        form = QHBoxLayout()
        self.start = QDateEdit(QDate(2020, 1, 1))
        self.end = QDateEdit(QDate.currentDate())
        for d in (self.start, self.end):
            d.setCalendarPopup(True)
            d.setDisplayFormat("yyyy-MM-dd")
        self.strats = QLineEdit()
        self.strats.setPlaceholderText("strategy ids, comma-separated (blank = all bindings)")
        self.today = QCheckBox("Today's cost rates")
        self.mult = QDoubleSpinBox()
        self.mult.setRange(0.5, 5)
        self.mult.setValue(1.0)
        self.mult.setPrefix("cost x")
        self.run_btn = QPushButton("Run backtest")
        self.run_btn.clicked.connect(self.run)
        for w in (QLabel("From"), self.start, QLabel("to"), self.end, self.strats, self.today, self.mult, self.run_btn):
            form.addWidget(w)
        lay.addLayout(form)
        split = QSplitter(Qt.Horizontal)
        self.summary = QPlainTextEdit()
        self.summary.setReadOnly(True)
        self.summary.setPlainText("Backtests use the same engine, strategies, gateway and kill switches as paper "
                                  "and live trading. Results are net of costs; check them at today's rates and "
                                  "with costs x1.5 before believing them.")
        split.addWidget(self.summary)
        self.chart = LineChart()
        split.addWidget(self.chart)
        split.setSizes([450, 650])
        lay.addWidget(split, 1)
        self.trades = DataTable([Col("strategy", "Strategy"), Col("instruments", "Instruments"), Col("side", "Side"),
                                 Col("entry_ts", "Entry", lambda v: str(v)[:10]),
                                 Col("exit_ts", "Exit", lambda v: str(v)[:10]),
                                 Col("pnl_net", "PnL net", inr, _pnl_kind, True),
                                 Col("r_multiple", "R", num, numeric=True), Col("exit_reason", "Exit reason")])
        lay.addWidget(self.trades, 1)
        self.worker: BacktestWorker | None = None

    def run(self):
        if self.worker and self.worker.isRunning():
            return
        only = [s.strip() for s in self.strats.text().split(",") if s.strip()] or None
        kwargs = {"pin_costs_today": self.today.isChecked(), "cost_multiplier": self.mult.value(), "only": only}
        self.worker = BacktestWorker(str(self.bridge.root), kwargs, self.start.date().toPython(),
                                     self.end.date().toPython())
        self.worker.done.connect(self._done)
        self.worker.failed.connect(lambda e: (self.summary.setPlainText(f"Backtest failed: {e}"),
                                              self.run_btn.setEnabled(True)))
        self.run_btn.setEnabled(False)
        self.summary.setPlainText("Running...")
        self.worker.start()

    def _done(self, res: dict):
        self.run_btn.setEnabled(True)
        self.summary.setPlainText(res["summary"])
        series = [("backtest", res["equity"], "accent")]
        live = self.bridge.equity()
        if live:
            series.append(("paper/live", [e for _, e in live], "warn"))
        self.chart.set_series(res["labels"], series)
        self.trades.set_rows(res["trades"])


# --------------------------------------------------------------------------------------------
class AuditScreen(Screen):
    title = "Audit & logs"

    def __init__(self, win):
        super().__init__(win)
        lay = QVBoxLayout(self)
        self.search = QLineEdit()
        self.search.setPlaceholderText("Search the audit trail (event, actor, strategy, symbol, reason...)")
        self.search.returnPressed.connect(lambda: self.refresh(self.win.snap, force=True))
        verify = QPushButton("Verify hash chain")
        verify.clicked.connect(self.verify)
        self.state = StateBadge()
        top = QHBoxLayout()
        top.addWidget(self.search, 1)
        top.addWidget(verify)
        top.addWidget(self.state)
        lay.addLayout(top)
        self.table = DataTable([Col("ts", "Time", ts_short), Col("event", "Event"), Col("actor", "Actor"),
                                Col("summary", "Details")])
        lay.addWidget(self.table, 1)
        self.detail = QPlainTextEdit()
        self.detail.setReadOnly(True)
        self.detail.setMaximumHeight(200)
        lay.addWidget(self.detail)
        self.table.selectionModel().selectionChanged.connect(self._sel)
        self._last = 0.0

    def tables(self):
        return {"audit": self.table}

    def refresh(self, snap, force: bool = False):
        import time
        if not force and time.time() - self._last < 5:      # the log can be large: refresh every 5 s
            return
        self._last = time.time()
        rows = []
        for r in self.bridge.audit(1500, self.search.text().strip() or None):
            p = r.get("payload", {})
            summary = p.get("reason") or p.get("verdict") or p.get("kind") or ""
            if r["event"] == "gateway_decision":
                summary = f"{p.get('verdict')} {p.get('strategy')} {p.get('action')} {p.get('legs_approved')}"
            rows.append({"ts": r["ts"], "event": r["event"], "actor": r["actor"], "summary": str(summary),
                         "_raw": r})
        self.table.set_rows(rows)

    def _sel(self, *_):
        r = self.table.selected_row()
        self.detail.setPlainText(json.dumps(r["_raw"], indent=2, default=str) if r else "")

    def verify(self):
        ok, bad = self.bridge.verify_audit()
        self.state.set_state("ok" if ok else "bad", "chain intact" if ok else f"BROKEN at record {bad}")


# --------------------------------------------------------------------------------------------
class SettingsScreen(Screen):
    """Paths, broker session, alerts, theme, About (FR-13.8)."""

    title = "Settings"

    def __init__(self, win):
        super().__init__(win)
        # Settings is long: put it in a scroll area so it fits small screens.
        from PySide6.QtWidgets import QScrollArea
        outer = QVBoxLayout(self)
        outer.setContentsMargins(0, 0, 0, 0)
        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        body = QWidget()
        scroll.setWidget(body)
        outer.addWidget(scroll)
        lay = QVBoxLayout(body)
        cfg = self.bridge.cfg
        paths = QGroupBox("Paths and configuration")
        pl = QFormLayout(paths)
        for label, val in (("Config folder", str(self.bridge.root)), ("State database", cfg.system.state_db),
                           ("Audit log", str(self.bridge.audit_path)), ("Plugins", cfg.system.plugins_dir),
                           ("Data", cfg.system.data_dir), ("Mode", cfg.system.mode.value),
                           ("Config hash", cfg.config_hash[:16] + "...")):
            pl.addRow(label, QLabel(val))
        lay.addWidget(paths)

        from .broker_panel import BrokerPanel
        self.broker = BrokerPanel(win)
        lay.addWidget(self.broker)

        ui = QGroupBox("Display and alerts")
        ul = QFormLayout(ui)
        self.theme = QComboBox()
        self.theme.addItems(["light", "dark"])
        s = QSettings()
        self.theme.setCurrentText(str(s.value("theme", "light")))
        self.theme.currentTextChanged.connect(win.set_theme)
        ul.addRow("Theme (Ctrl+T)", self.theme)
        self.alert_boxes = {}
        for key, label in (("alert_red", "Alignment turns red"), ("alert_kill", "Kill-switch events"),
                           ("alert_limit", "Limit usage above 80%"), ("alert_recon", "Reconciliation mismatch"),
                           ("alert_engine", "Engine heartbeat lost")):
            cb = QCheckBox(label)
            cb.setChecked(str(s.value(key, "true")).lower() == "true")
            cb.toggled.connect(lambda v, k=key: QSettings().setValue(k, "true" if v else "false"))
            self.alert_boxes[key] = cb
            ul.addRow(cb)
        lay.addWidget(ui)

        about = QGroupBox("About")
        al = QFormLayout(about)
        from .. import __version__
        al.addRow("Version", QLabel(__version__))
        self.plugins = QLabel("-")
        al.addRow("Plugin hashes", self.plugins)
        lay.addWidget(about)
        keys = QLabel("Shortcuts: Ctrl+K kill (block entries) | Ctrl+Shift+F flatten all | Ctrl+Shift+X exit all |"
                      " Ctrl+E exit selected trade | Ctrl+1..0 screens | F5 refresh | Ctrl+T theme")
        keys.setObjectName("Muted")
        keys.setWordWrap(True)
        lay.addWidget(keys)
        lay.addStretch(1)

    def refresh(self, snap):
        self.plugins.setText("  ".join(f"{s['id'].split('_')[0]}:{s['plugin_hash']}" for s in snap["strategies"]))
