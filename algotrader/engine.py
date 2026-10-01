"""Trading engine: the daily orchestration loop (PRD s.4, strategy doc s.3-s.4).

The same loop runs in backtest, paper and live modes (FR-5.6, NFR-6); only the clock, the data
source and the broker adapter differ. One call to ``run_day`` processes one session:

  1. Broker phase  - pending orders fill at the open; protective stops are (re)placed for
                     newly opened trades; intraday stops / limits are processed.
  2. Close         - quotes are updated with closing prices; the book is marked to market.
  3. Safety        - equity, drawdown ladder, strategy and instrument kill switches,
                     data-health checks, reconciliation with the broker.
  4. Kill actions  - due flatten / reduce actions are executed as risk-reducing orders.
  5. Regime        - the regime engine updates once per day after the close.
  6. Strategies    - each plugin gets a read-only Context and returns Signals, which are
                     sized, sent through the risk gateway and handed to the order manager.
                     Orders go in after the close for the next session.
  7. Monitoring    - alignment indicator for every open trade; optional auto-action on red.
  8. Persistence   - equity row, heartbeat, strategy state.
"""

from __future__ import annotations

import copy
import logging
import time as _time
from dataclasses import dataclass, field
from datetime import date, datetime, time
from typing import Any

import pandas as pd

from .core.audit import AuditLog
from .core.calendar import EventCalendar
from .core.config import AppConfig, Binding
from .core.costs import CostTable
from .core.instruments import InstrumentRegistry, InstrumentView
from .core.models import (Bar, InstrumentType, Leg, Mode, OptionContract, Order, OrderIntent,
                          OrderStatus, OrderType, Purpose, Quote, Side, Signal, SignalAction, Trade,
                          TradeLeg, new_id)
from .core.state import StateStore
from .data.options import ModelOptionChain
from .data.provider import MarketDataProvider
from .execution.broker import BrokerGateway
from .execution.oms import OrderManager, RateLimiter
from .execution.paper import PaperBroker
from .monitoring.alignment import evaluate_alignment
from .monitoring.drift import compute_stats
from .monitoring.reconcile import reconcile
from .portfolio.book import Book, new_trade
from .portfolio.sizing import VolTargetOverlay, lots_for_risk
from .regime import RegimeEngine
from .monitoring.blotter import blotter_rows
from .risk.gateway import LimitChangeManager, RiskContext, RiskGateway
from .risk.killswitch import KillSwitchManager, KSAction, KSLevel
from .strategy.base import Context, Strategy

log = logging.getLogger(__name__)

OPEN_TIME = time(9, 15)
CLOSE_TIME = time(15, 30)


@dataclass
class BoundStrategy:
    """A strategy instance plus its binding, plugin hash and persisted state."""

    strategy: Strategy
    binding: Binding
    plugin_hash: str = ""
    state: dict[str, Any] = field(default_factory=dict)

    @property
    def id(self) -> str:
        return self.strategy.id


class TradingEngine:
    def __init__(self, cfg: AppConfig, registry: InstrumentRegistry, costs: CostTable,
                 calendar: EventCalendar, data: MarketDataProvider, broker: BrokerGateway,
                 strategies: list[BoundStrategy], store: StateStore, audit: AuditLog,
                 mode: Mode = Mode.BACKTEST, option_chain: ModelOptionChain | None = None):
        self.cfg = cfg
        self.registry = registry
        self.costs = costs
        self.calendar = calendar
        self.data = data
        self.broker = broker
        self.strategies = strategies
        self.store = store
        self.audit = audit
        self.mode = mode
        self.option_chain = option_chain
        self.capital = cfg.system.capital

        self._now = datetime.combine(date.today(), CLOSE_TIME)
        self.book = Book()
        self.regime = RegimeEngine(cfg.portfolio.regime)
        self.ks = KillSwitchManager(cfg.killswitch, cfg.portfolio, audit, self.now,
                                    persist=lambda rows: store.set("killswitch", rows))
        self.rc = RiskContext(registry=registry, calendar=calendar, costs=costs, book=self.book,
                              killswitch=self.ks, regime=self.regime, portfolio=cfg.portfolio,
                              now=self.now, nav=self.nav, enforce_session=mode is Mode.LIVE,
                              sessions=cfg.system.sessions)
        self.gateway = RiskGateway(self.rc, cfg.risk, audit, cfg.config_hash,
                                   profiles={b.binding.strategy: b.binding.risk_profile for b in strategies},
                                   dry_run=cfg.system.gateway_dry_run)
        self.oms = OrderManager(broker, store, audit, self.now, cfg.system.algo_tags,
                                RateLimiter(cfg.system.orders_per_second, virtual=mode is Mode.BACKTEST),
                                on_reject=self._on_reject)
        self.overlay = VolTargetOverlay(cfg.system.vol_target_annual, cfg.system.vol_overlay_min_scale)

        self.equity_curve: list[tuple[date, float]] = []
        self.strategy_peaks: dict[str, float] = {}
        self.strategy_pnl_hist: dict[str, list[float]] = {b.id: [] for b in strategies}
        self.rejections: dict[str, int] = {}
        self.stop_orders: dict[str, str] = {}          # trade id -> client ref of resting stop
        self.alerts: list[str] = []
        self.day_start_equity = self.capital
        self.week_start_equity = self.capital
        self._last_day: date | None = None
        self._nav_cache: float | None = None
        self._frames: dict[str, pd.DataFrame] = {}
        self._overlay_scale = 1.0
        self.regime_log: list[dict[str, Any]] = []
        self.paused: set[str] = set()                  # strategies paused by the operator
        self.limits = LimitChangeManager(cfg.risk, audit, self.now, cfg.killswitch.loosen_cooloff_hours)

    # ------------------------------------------------------------------------------------
    # Clock, prices, NAV
    # ------------------------------------------------------------------------------------
    def now(self) -> datetime:
        return self._now

    def prices(self) -> dict[str, float]:
        return self.rc.prices()

    def nav(self) -> float:
        if self._nav_cache is None:
            self._nav_cache = self.capital + self.book.total_pnl(self.prices())
        return self._nav_cache

    def _invalidate_nav(self) -> None:
        self._nav_cache = None

    def _frame(self, key: str) -> pd.DataFrame | None:
        if key not in self._frames:
            try:
                self._frames[key] = self.data.history(self.registry.get(key).feed if key in self.registry else key)
            except (FileNotFoundError, KeyError):
                return None
        return self._frames[key]

    def _bars_until(self, key: str, ts: pd.Timestamp, lookback: int) -> pd.DataFrame | None:
        """History up to and including ``ts`` (no look-ahead), limited to ``lookback`` bars."""
        df = self._frame(key)
        if df is None:
            return None
        end = df.index.searchsorted(ts, side="right")
        return df.iloc[max(0, end - lookback):end]

    def _bar_on(self, key: str, ts: pd.Timestamp) -> Bar | None:
        df = self._frame(key)
        if df is None or ts not in df.index:
            return None
        r = df.loc[ts]
        return Bar(ts.to_pydatetime(), float(r["open"]), float(r["high"]), float(r["low"]),
                   float(r["close"]), float(r["volume"]))

    def _tradable_keys(self) -> list[str]:
        keys: set[str] = set()
        for bs in self.strategies:
            for k in bs.binding.instruments:
                inst = self.registry.get(k)
                if inst.type is InstrumentType.INDEX_OPTION:
                    if inst.underlying:
                        keys.add(inst.underlying)
                elif inst.tradable:
                    keys.add(k)
            for k in bs.strategy.data_needs.extra_series:   # e.g. S3's Nifty hedge future
                if k in self.registry and self.registry.get(k).tradable:
                    keys.add(k)
        return sorted(keys)

    # ------------------------------------------------------------------------------------
    # One session
    # ------------------------------------------------------------------------------------
    def run_day(self, ts: pd.Timestamp) -> dict[str, Any]:
        day = ts.date()
        self._new_day(day)
        if self.mode is not Mode.BACKTEST:
            self.check_external_commands()
            self.process_commands()
            self.limits.apply_due()
        keys = self._tradable_keys()
        bars = {k: b for k in keys if (b := self._bar_on(k, ts)) is not None}

        # 1. Broker phase: opening fills, stops for newly opened trades, then intraday.
        self._now = datetime.combine(day, OPEN_TIME)
        if isinstance(self.broker, PaperBroker):
            for k, b in bars.items():
                self.broker.process_open(k, b)
            self._process_fills()
            for k, b in bars.items():
                self.broker.process_intraday(k, b)
            self._now = datetime.combine(day, CLOSE_TIME)
            self._option_quotes(day, feed_broker=True)
        self._process_fills()

        # 2. Close: quotes and marks.
        self._now = datetime.combine(day, CLOSE_TIME)
        for k, b in bars.items():
            self.rc.quotes[k] = Quote(k, b.close, self._now)
        self._option_quotes(day, feed_broker=False)
        self._invalidate_nav()
        closes = {k: b.close for k, b in bars.items()}
        self.book.mark(self.prices(), closes)
        for t in self.book.open_trades(include_pending=False):
            if t.entry_ts and t.entry_ts.date() < day:
                t.bars_held += 1

        # 3. Safety checks.
        equity = self.nav()
        self._safety_checks(day, keys, bars, equity)

        # 4. Kill-switch actions.
        self._execute_killswitch_actions()

        # 5. Regime.
        rcfg = self.cfg.portfolio.regime
        mkt = self._bars_until(rcfg.market_series, ts, 300)
        vix = self._bars_until(rcfg.vix_series, ts, 300)
        if mkt is not None and len(mkt):
            snap = self.regime.update(day, mkt["close"], vix["close"] if vix is not None else None)
            if snap.changed:
                self.audit.record("regime_change", "regime", {"regime": snap.regime, "er": snap.er,
                                  "vix_pct": snap.vix_pct}, ts=self._now)
            self.regime_log.append({"day": day, "regime": snap.regime.value if snap.regime else None,
                                    "er": snap.er, "vix_pct": snap.vix_pct})

        # 6. Strategies.
        self._overlay_scale = self.overlay.scale([e for _, e in self.equity_curve] + [equity])
        for bs in self.strategies:
            if not bs.binding.enabled or bs.id in self.paused:
                continue
            ctx = self._context(bs, ts)
            try:
                signals = bs.strategy.on_bar(ctx) or []
            except Exception as exc:  # noqa: BLE001 - a plugin crash trips its kill switch
                log.exception("strategy %s failed", bs.id)
                self.ks.trip(KSLevel.STRATEGY, bs.id, KSAction.BLOCK_ENTRIES, f"strategy exception: {exc}")
                continue
            for sig in signals:
                self._handle_signal(bs, sig, ctx)

        # 7. Alignment.
        self._update_alignment(ts)

        # 8. Persistence.
        self._invalidate_nav()
        equity = self.nav()
        self.equity_curve.append((day, equity))
        row = {"day": day, "equity": equity, "ladder": self.ks.ladder_state,
               "regime": self.regime.current.value if self.regime.current else None,
               "open_trades": len(self.book.open_trades()),
               "pnl": {bs.id: self.book.strategy_pnl(bs.id, self.prices()) for bs in self.strategies}}
        self.store.save_equity(day, row)
        if self.mode is not Mode.BACKTEST:
            self.save_state()
            self.publish_snapshot()
        self.store.heartbeat("engine", _time.time())
        return row

    # ------------------------------------------------------------------------------------
    # Persistence and restart (FR-11.1, NFR-5)
    # ------------------------------------------------------------------------------------
    def save_state(self) -> None:
        for t in self.book.trades.values():
            if t.status in ("OPEN", "PENDING") or t.exit_ts and t.exit_ts.date() == self._now.date():
                self.store.save_trade(t)
        self.store.set("strategy_state", {bs.id: bs.state for bs in self.strategies})
        self.store.set("regime", self.regime.to_dict())
        self.store.set("ladder", {"state": self.ks.ladder_state, "peak": self.ks.ladder_peak})
        self.store.set("stop_orders", self.stop_orders)
        self.store.set("paused_strategies", sorted(self.paused))
        self.store.set("quotes", {s: [q.last, q.ts.isoformat(), q.bid, q.ask] for s, q in self.rc.quotes.items()})
        self.store.set("engine", {"strategy_peaks": self.strategy_peaks, "last_day": self._last_day,
                                  "equity_curve": [(d.isoformat(), e) for d, e in self.equity_curve[-300:]]})

    def restore_state(self) -> int:
        """Rebuild state after a restart, then reconcile before new entries (FR-11.1).

        Returns the number of open trades restored. With the paper broker, positions and the
        protective stops are re-created; with a live broker the resting stops are re-linked and
        the order book is re-read.
        """
        for d in self.store.load_trades():
            t = trade_from_dict(d)
            self.book.add(t)
        st = self.store.get("strategy_state", {}) or {}
        for bs in self.strategies:
            bs.state.update(st.get(bs.id, {}))
        if (rg := self.store.get("regime")):
            self.regime.load(rg)
        ladder = self.store.get("ladder", {}) or {}
        self.ks.load(self.store.get("killswitch", []) or [], ladder.get("state", "GREEN"), ladder.get("peak"))
        eng = self.store.get("engine", {}) or {}
        self.strategy_peaks.update(eng.get("strategy_peaks", {}))
        self.equity_curve = [(date.fromisoformat(d), e) for d, e in eng.get("equity_curve", [])]
        if eng.get("last_day"):
            self._last_day = date.fromisoformat(eng["last_day"])
            self._now = datetime.combine(self._last_day, CLOSE_TIME)
        self.paused = set(self.store.get("paused_strategies", []) or [])
        for sym, (last, ts, bid, ask) in (self.store.get("quotes", {}) or {}).items():
            self.rc.quotes[sym] = Quote(sym, last, datetime.fromisoformat(ts), bid, ask)
        self._invalidate_nav()
        open_trades = self.book.open_trades()
        if isinstance(self.broker, PaperBroker):
            self.broker.restore_positions(self.book.net_positions())
            for t in open_trades:
                if t.is_open and t.stop_price is not None and len(t.legs) == 1:
                    self._place_stop(t)
        else:
            self.stop_orders = dict(self.store.get("stop_orders", {}) or {})
            self.oms.recover()
        self.audit.record("state_restored", "engine", {"open_trades": len(open_trades),
                          "kill_switches": len(self.ks.trips)}, ts=self._now)
        return len(open_trades)

    def check_external_commands(self) -> None:
        """Pick up the operator's kill button / watchdog trips written by other processes."""
        cmd = self.store.get("external_kill")
        if cmd and not cmd.get("handled"):
            self.ks.trip(KSLevel.SYSTEM, "system",
                         KSAction.FLATTEN if cmd.get("flatten") else KSAction.BLOCK_ENTRIES,
                         cmd.get("reason", "external kill"), actor=cmd.get("actor", "operator"))
            cmd["handled"] = True
            self.store.set("external_kill", cmd)

    # ------------------------------------------------------------------------------------
    # Operator commands (desktop UI / CLI -> engine) and the published UI snapshot
    # ------------------------------------------------------------------------------------
    def process_commands(self) -> int:
        """Apply queued operator commands. Every action still goes through the risk gateway
        (exits as risk-reducing orders) and is written to the audit log with its outcome."""
        n = 0
        for cmd in self.store.pending_commands():
            kind, body, actor = cmd["kind"], cmd["body"], cmd["actor"]
            try:
                ok, result = self._apply_command(kind, body, actor)
            except Exception as exc:  # noqa: BLE001 - a bad command must not stop the engine
                log.exception("command %s failed", kind)
                ok, result = False, f"error: {exc}"
            self.store.complete_command(cmd["id"], "DONE" if ok else "FAILED", result)
            self.audit.record("operator_command", actor, {"kind": kind, "body": body, "ok": ok,
                              "result": result}, ts=self._now)
            n += 1
        if n and self.mode is not Mode.BACKTEST:
            self.save_state()
            self.publish_snapshot()
        return n

    def _apply_command(self, kind: str, b: dict[str, Any], actor: str) -> tuple[bool, str]:
        reason = (b.get("reason") or "").strip()
        if kind == "manual_exit":
            ok = self.manual_exit(b["trade_id"], float(b.get("fraction", 1.0)), reason)
            return ok, "exit order sent" if ok else "exit not sent (closed, already exiting or blocked)"
        if kind == "exit_all":
            n = self.exit_all(b.get("strategy"), b.get("instrument"), reason)
            return True, f"exit orders sent for {n} trade(s)"
        if kind == "adjust_stop":
            t = self.book.get(b["trade_id"])
            before = t.stop_price if t else None
            self._adjust_stop(b["trade_id"], float(b["stop"]))
            after = t.stop_price if t else None
            return after != before, f"stop {before} -> {after}" if after != before else \
                "rejected: a stop can only move in the direction of lower risk"
        if kind == "kill":
            self.ks.trip(KSLevel.SYSTEM, "system", KSAction.FLATTEN if b.get("flatten") else KSAction.BLOCK_ENTRIES,
                         reason or "manual system kill", actor=actor)
            self._execute_killswitch_actions()
            return True, "system kill switch tripped"
        if kind == "trip":
            level = KSLevel(b["level"])
            action = KSAction[b.get("action", "BLOCK_ENTRIES")]
            self.ks.trip(level, b["scope"], action, reason or f"manual {level.value} trip", actor=actor)
            self._execute_killswitch_actions()
            return True, f"{level.value}:{b['scope']} {action.name}"
        if kind == "reset_request":
            msg = self.ks.request_reset(KSLevel(b["level"]), b["scope"], reason, actor)
            return "cooling-off" in msg, msg
        if kind == "reset_confirm":
            return self.ks.confirm_reset(KSLevel(b["level"]), b["scope"], actor, b.get("code"), equity=self.nav())
        if kind == "pause_strategy":
            self.paused.add(b["strategy"])
            return True, f"{b['strategy']} paused: no new signals (open trades keep their exits)"
        if kind == "resume_strategy":
            self.paused.discard(b["strategy"])
            return True, f"{b['strategy']} resumed"
        if kind == "instrument_flag":
            flags = {k: bool(v) for k, v in b.items() if k in ("banned", "circuit")}
            self.registry.set_flag(b["instrument"], **flags)
            return True, f"{b['instrument']} flags {flags}"
        if kind == "propose_limit":
            ch = self.limits.propose(b["level"], b.get("scope"), b["key"], b["value"], reason, actor)
            return True, ("applied immediately (tightening)" if ch.applied else
                          f"loosening #{ch.change_id}: needs confirmation, effective {ch.effective:%Y-%m-%d %H:%M}")
        if kind == "confirm_limit":
            self.limits.confirm(int(b["change_id"]), actor)
            return True, f"change #{b['change_id']} confirmed; applies after the cooling-off"
        return False, f"unknown command '{kind}'"

    def snapshot(self) -> dict[str, Any]:
        """Everything the desktop UI shows, as plain JSON-able data (PRD s.8 and s.12)."""
        prices = self.prices()
        nav = self.nav()
        eq = [e for _, e in self.equity_curve] or [self.capital]
        peak = max(eq + [nav])
        rows = blotter_rows(self.book.open_trades(), prices, nav, self._now)
        for r in rows:
            t = self.book.get(r["trade_id"])
            q = self.rc.quotes.get(t.primary.symbol) if t else None
            r["data_age_s"] = (self._now - q.ts).total_seconds() if q else None
            r["risk_now"] = self.book.trade_risk(t, prices) if t else None
            r["status"] = t.status if t else None
            r["legs_detail"] = [{"symbol": s, "qty": l.qty, "avg": l.avg_price, "last": prices.get(s)}
                                for s, l in t.legs.items()] if t else []
            r["alignment_history"] = (t.alignment[-15:] if t else [])
            r["reason"] = t.reason if t else ""
            r["working_exit"] = bool(t and self.oms.open_orders(t.trade_id, Purpose.EXIT))
        gross = net = 0.0
        for _, leg in self.book.legs():
            if leg.contract is None and leg.qty:
                notional = leg.qty * (prices.get(leg.symbol) or leg.avg_price)
                gross += abs(notional)
                net += notional * self.registry.get(leg.instrument).beta
        margin = self.rc.margin_used()
        g = self.cfg.risk.global_limits
        budget = self.cfg.portfolio.book_risk_budget_pct / 100 * nav
        strategies = []
        for bs in self.strategies:
            alloc = self.cfg.portfolio.strategies[bs.id]
            closed = self.book.closed_trades(bs.id)
            wins = sum(1 for t in closed if t.realized() - t.charges > 0)
            strategies.append({
                "id": bs.id, "version": bs.strategy.version, "stage": bs.binding.stage,
                "mode": bs.binding.mode.value, "enabled": bs.binding.enabled, "paused": bs.id in self.paused,
                "instruments": bs.binding.instruments, "pnl": self.book.strategy_pnl(bs.id, prices),
                "open": len([t for t in self.book.open_trades(bs.id) if t.meta.get("role") != "hedge"]),
                "closed": len(closed), "hit_rate": wins / len(closed) if closed else None,
                "risk_used": self.book.open_risk(prices, [bs.id]), "risk_cap": alloc.risk_share * budget,
                "risk_multiplier": self.ks.risk_multiplier(bs.id), "blocked": self.ks.entry_block_reasons(bs.id),
                "family": alloc.family, "plugin_hash": bs.plugin_hash[:12],
                "permission": self.regime.permission(bs.id), "params": bs.strategy.params.model_dump()})
        h = self.rc.health
        last = self.equity_curve[-1][1] if self.equity_curve else self.capital
        return {
            "ts": self._now.isoformat(), "published": datetime.now().isoformat(), "mode": self.mode.value,
            "capital": self.capital, "nav": nav, "day_pnl": nav - self.day_start_equity,
            "week_pnl": nav - self.week_start_equity,
            "month_pnl": nav - next((e for d, e in self.equity_curve if d.month == self._now.month
                                     and d.year == self._now.year), last),
            "peak": peak, "drawdown_pct": (nav / peak - 1) * 100 if peak else 0.0,
            "ladder": self.ks.ladder_state, "margin_used": margin,
            "margin_util_pct": margin / nav * 100 if nav else 0.0,
            "margin_cap_pct": g.get("max_margin_util_pct", 40), "gross": gross, "net_beta": net,
            "gross_cap": g.get("max_gross_exposure_pct_nav", 0) / 100 * nav,
            "net_cap": g.get("max_net_exposure_pct_nav", 0) / 100 * nav,
            "book_risk": self.book.open_risk(prices), "book_risk_budget": budget,
            "health": {"data": h.data_connected, "broker": h.broker_connected, "session": h.session_valid,
                       "reconciled": h.reconciled, "gateway": True},
            "regime": {"name": self.regime.current.value if self.regime.current else None,
                       "er": self.regime.last.er, "vix_pct": self.regime.last.vix_pct,
                       "pending": self.regime.pending.value if self.regime.pending else None,
                       "permissions": self.regime.permission_table()},
            "kill_switches": [{"level": t.level.value, "scope": t.scope, "action": t.action.name,
                               "reason": t.reason, "ts": t.ts.isoformat(), "external_key": t.external_key,
                               "manual_reset": t.manual_reset, "until": t.until.isoformat() if t.until else None,
                               "reset_requested": t.reset_requested_at.isoformat() if t.reset_requested_at else None}
                              for t in self.ks.trips.values()],
            "trades": rows, "strategies": strategies,
            "prices": {s: {"last": q.last, "ts": q.ts.isoformat()} for s, q in self.rc.quotes.items()},
            "alerts": list(dict.fromkeys(self.alerts))[-50:],
            "pending_limit_changes": [{"id": c.change_id, "level": c.level, "scope": c.scope, "key": c.key,
                                       "old": c.old, "new": c.new, "reason": c.reason,
                                       "effective": c.effective.isoformat(), "confirmed": c.confirmed}
                                      for c in self.limits.pending.values()],
            "instrument_flags": {i.key: {"banned": i.flags.banned, "circuit": i.flags.circuit}
                                 for i in self.registry.all()},
            "recent_alignment_red": sum(1 for r in rows if r["alignment"] == "OFF_THESIS"),
            "config_hash": self.cfg.config_hash,
        }

    def reload_data(self) -> None:
        """Drop cached bars so a long-running engine sees newly downloaded sessions."""
        self._frames.clear()
        cache = getattr(self.data, "_cache", None)
        if isinstance(cache, dict):
            cache.clear()

    def publish_snapshot(self) -> None:
        self.store.set("ui_snapshot", self.snapshot())

    def _new_day(self, day: date) -> None:
        if self._last_day is not None:
            prev = self.equity_curve[-1][1] if self.equity_curve else self.capital
            self.day_start_equity = prev
            if day.isocalendar()[1] != self._last_day.isocalendar()[1]:
                self.week_start_equity = prev
        self._last_day = day
        self._invalidate_nav()

    # ------------------------------------------------------------------------------------
    # Context for strategies
    # ------------------------------------------------------------------------------------
    def _context(self, bs: BoundStrategy, ts: pd.Timestamp) -> Context:
        sid = bs.id
        alloc = self.cfg.portfolio.strategies[sid]
        lookback = bs.strategy.data_needs.lookback
        keys = list(bs.binding.instruments) + list(bs.strategy.data_needs.extra_series)
        for k in bs.binding.instruments:
            inst = self.registry.get(k)
            if inst.underlying:
                keys.append(inst.underlying)
        bars = {}
        for k in dict.fromkeys(keys):
            df = self._bars_until(k, ts, lookback)
            if df is not None:
                bars[k] = df
        flags = {k: self.registry.get(k).flags.model_dump() for k in keys if k in self.registry}
        day = ts.date()
        chain = (lambda key, expiry: self.option_chain.chain(key, expiry, day)) if self.option_chain else None
        return Context(
            now=self._now, strategy_id=sid, instruments=list(bs.binding.instruments), nav=self.nav(),
            risk_pct=alloc.per_trade_risk_pct / 100.0, permission=self.regime.permission(sid),
            regime=self.regime.last, view=InstrumentView(self.registry), calendar=self.calendar,
            trades=copy.deepcopy(self.book.open_trades(sid)), state=bs.state,
            entries_blocked=bool(self.ks.entry_block_reasons(sid)),
            risk_multiplier=self.ks.risk_multiplier(sid) * self._overlay_scale,
            risk_pct_stock=(alloc.per_trade_risk_pct_stock / 100.0
                            if alloc.per_trade_risk_pct_stock is not None else None),
            _bars=bars, _flags=flags, _option_chain=chain)

    # ------------------------------------------------------------------------------------
    # Signals -> intents -> gateway -> orders
    # ------------------------------------------------------------------------------------
    def _handle_signal(self, bs: BoundStrategy, sig: Signal, ctx: Context) -> None:
        if sig.strategy_id != bs.id:
            log.error("strategy %s emitted a signal for %s; ignored", bs.id, sig.strategy_id)
            return
        if sig.action is SignalAction.ENTER:
            self._enter(bs, sig)
            return
        if sig.action is SignalAction.HEDGE:
            self._hedge(bs, sig)
            return
        trade = self.book.get(sig.trade_id)
        if trade is None or trade.strategy_id != bs.id or trade.status not in ("OPEN", "PENDING"):
            return
        if sig.action is SignalAction.EXIT:
            self.exit_trade(trade.trade_id, sig.reason, 1.0)
        elif sig.action is SignalAction.REDUCE:
            self.exit_trade(trade.trade_id, sig.reason, sig.reduce_fraction)
        elif sig.action is SignalAction.ADJUST_STOP and sig.stop_price is not None:
            self._adjust_stop(trade.trade_id, sig.stop_price)

    def _ensure_option_quote(self, leg: Leg) -> None:
        if leg.contract and leg.symbol not in self.rc.quotes and self.option_chain:
            q = self.option_chain.quote(leg.instrument, leg.contract.expiry, leg.contract.strike,
                                        leg.contract.right, self._now.date())
            self.rc.quotes[leg.symbol] = Quote(leg.symbol, q["mid"], self._now, q["bid"], q["ask"])

    def _enter(self, bs: BoundStrategy, sig: Signal) -> None:
        today = self._now.date()
        legs = [copy.deepcopy(l) for l in sig.legs]
        for leg in legs:
            self._ensure_option_quote(leg)
        risk_amount = sig.max_loss
        if len(legs) == 1 and legs[0].lots is None:
            # Shared sizing rule (strategy doc s.4).
            if sig.stop_price is None or sig.ref_price is None:
                log.warning("%s entry without stop ignored", bs.id)
                return
            lot = self.registry.get(legs[0].instrument).lot_size(today)
            ctx_mult = self.regime.permission(bs.id) * self.ks.risk_multiplier(bs.id) * self._overlay_scale
            alloc = self.cfg.portfolio.strategies[bs.id]
            r = sig.risk_pct if sig.risk_pct is not None else alloc.per_trade_risk_pct / 100.0
            lots = lots_for_risk(self.nav(), r, sig.ref_price, sig.stop_price, lot, ctx_mult)
            if lots <= 0:
                self.audit.record("entry_skipped", bs.id, {"instrument": legs[0].instrument,
                                  "reason": "N rounds to zero: instrument too large for the account"},
                                  ts=self._now)
                return
            legs[0].lots = lots
            risk_amount = abs(sig.ref_price - sig.stop_price) * lots * lot
        if any(not l.lots for l in legs):
            return
        if risk_amount is None and len(legs) == 1 and sig.stop_price is not None and sig.ref_price is not None:
            # Pre-sized single-leg entry (e.g. S3, which sizes before computing its hedge).
            lot = self.registry.get(legs[0].instrument).lot_size(today)
            risk_amount = abs(sig.ref_price - sig.stop_price) * (legs[0].lots or 0) * lot
        intent = OrderIntent(strategy_id=bs.id, action=SignalAction.ENTER, legs=legs, reason=sig.reason,
                             reducing=False, side=sig.side, stop_price=sig.stop_price,
                             ref_price=sig.ref_price, atr=sig.atr, risk_amount=risk_amount,
                             expected_edge=sig.expected_edge, order_type=sig.order_type,
                             limit_price=sig.limit_price, meta=dict(sig.meta))
        decision = self.gateway.evaluate(intent)
        if not decision.approved:
            return
        ai = decision.intent
        trade_id = new_id("t-")
        tlegs = []
        for leg in ai.legs:
            lot = self.registry.get(leg.instrument).lot_size(today)
            tlegs.append(TradeLeg(instrument=leg.instrument, symbol=leg.symbol, lot_size=lot,
                                  contract=leg.contract, target_qty=leg.side.sign * (leg.lots or 0) * lot))
        meta = dict(sig.meta)
        if sig.stop_price is not None and sig.ref_price is not None:
            meta["stop_distance"] = abs(sig.ref_price - sig.stop_price)
        if sig.max_loss is not None:
            meta["max_loss"] = ai.risk_amount
            meta["margin"] = self.rc.margin_for_intent(ai)
        p0 = self.registry.get(ai.legs[0].instrument)
        prof = self.costs.profile(p0.cost_profile, today)
        meta["assumed_slippage"] = prof.slippage_ticks * p0.tick_size(today) * sum(
            abs(t.target_qty) for t in tlegs)
        trade = new_trade(trade_id, bs.id, sig.side or Side.LONG, tlegs, stop_price=sig.stop_price,
                          initial_stop=sig.stop_price, initial_risk=ai.risk_amount or 0.0,
                          entry_atr=sig.atr, reason=sig.reason, meta=meta)
        self.book.add(trade)
        orders = [self.oms.new_order(strategy_id=bs.id, symbol=leg.symbol, instrument=leg.instrument,
                                     side=leg.side, lots=leg.lots or 0,
                                     lot_size=self.registry.get(leg.instrument).lot_size(today),
                                     purpose=Purpose.ENTRY, order_type=ai.order_type, trade_id=trade_id,
                                     limit_price=ai.limit_price if leg is ai.legs[0] else None,
                                     contract=leg.contract, intent_id=ai.intent_id,
                                     decision_price=leg.ref_price or self.rc.price(leg.symbol))
                  for leg in ai.legs]
        if len(orders) > 1:
            self.oms.submit_group(orders)
        else:
            self.oms.submit(orders[0])
        if all(o.status is OrderStatus.REJECTED for o in orders):
            self.book.cancel_pending(trade_id, "entry rejected")
        self.store.save_trade(trade)

    def exit_trade(self, trade_id: str, reason: str, fraction: float = 1.0, source: str = "strategy") -> bool:
        """Close all or part of a trade with risk-reducing orders through the gateway.

        Also used by the manual exit (PRD s.8) and kill-switch flatten; works while entries are
        blocked because exits are risk-reducing (FR-7.1).
        """
        trade = self.book.get(trade_id)
        if trade is None or trade.status not in ("OPEN", "PENDING"):
            return False
        today = self._now.date()
        if self.oms.open_orders(trade_id, Purpose.EXIT):
            return False          # an exit is already working
        for o in self.oms.open_orders(trade_id, Purpose.ENTRY):
            self.oms.cancel(o.client_ref)
        if trade.is_flat:
            self.book.cancel_pending(trade_id, reason)
            return True
        legs = []
        for leg in trade.legs.values():
            if not leg.qty:
                continue
            lots_held = abs(leg.qty) // leg.lot_size
            lots = lots_held if fraction >= 1.0 else max(1, int(round(lots_held * fraction)))
            side = Side.LONG if leg.qty > 0 else Side.SHORT
            leg_obj = Leg(instrument=leg.instrument, side=side.exit_order_side(), lots=lots,
                          contract=leg.contract)
            self._ensure_option_quote(leg_obj)
            legs.append(leg_obj)
        if not legs:
            return False
        intent = OrderIntent(strategy_id=trade.strategy_id,
                             action=SignalAction.EXIT if fraction >= 1.0 else SignalAction.REDUCE,
                             legs=legs, reason=reason, reducing=True, trade_id=trade_id, source=source)
        decision = self.gateway.evaluate(intent)
        if not decision.approved:
            self.alerts.append(f"exit for {trade_id} not approved: {decision.reasons}")
            return False
        # Cancel the resting stop; it is re-placed for any remaining quantity after the fill.
        stop_ref = self.stop_orders.pop(trade_id, None)
        if stop_ref:
            self.oms.cancel(stop_ref)
        trade.exit_reason = reason
        orders = [self.oms.new_order(strategy_id=trade.strategy_id, symbol=l.symbol, instrument=l.instrument,
                                     side=l.side, lots=l.lots or 0,
                                     lot_size=self.registry.get(l.instrument).lot_size(today),
                                     purpose=Purpose.EXIT, trade_id=trade_id, contract=l.contract,
                                     decision_price=self.rc.price(l.symbol))
                  for l in decision.intent.legs]
        if len(orders) > 1:
            # Close short option legs before long wings so the structure is never naked.
            self.oms.submit_group(orders)
        else:
            self.oms.submit(orders[0])
        return True

    def _adjust_stop(self, trade_id: str, new_stop: float) -> None:
        """Move a stop only in the direction of lower risk (strategy doc s.4)."""
        trade = self.book.get(trade_id)
        if trade is None or trade.stop_price is None or not trade.is_open:
            return
        tighter = new_stop > trade.stop_price if trade.side is Side.LONG else new_stop < trade.stop_price
        if not tighter:
            return
        inst = self.registry.get(trade.primary.instrument)
        tick = inst.tick_size(self._now.date())
        new_stop = round(new_stop / tick) * tick
        ref = self.stop_orders.get(trade_id)
        leg = trade.primary
        intent = OrderIntent(strategy_id=trade.strategy_id, action=SignalAction.ADJUST_STOP,
                             legs=[Leg(leg.instrument, trade.side.exit_order_side(),
                                       abs(leg.qty) // leg.lot_size)],
                             reason=f"trail stop to {new_stop}", reducing=True, trade_id=trade_id,
                             stop_price=new_stop)
        if not self.gateway.evaluate(intent).approved:
            return
        trade.stop_price = new_stop
        if ref:
            self.oms.modify_stop(ref, new_stop)

    def _hedge(self, bs: BoundStrategy, sig: Signal) -> None:
        """Set the target size of a strategy's hedge trade (S3 Nifty futures hedge)."""
        today = self._now.date()
        target = int(sig.meta.get("target_lots", 0))
        key = sig.legs[0].instrument
        lot = self.registry.get(key).lot_size(today)
        hedge = next((t for t in self.book.open_trades(bs.id) if t.meta.get("role") == "hedge"), None)
        current = (hedge.primary.qty // lot) if hedge else 0
        pending = self.oms.open_orders(hedge.trade_id, Purpose.HEDGE) if hedge else []
        diff = target - current
        if diff == 0 or pending:
            return
        side = Side.LONG if diff > 0 else Side.SHORT
        reducing = abs(target) < abs(current) and (target == 0 or (target > 0) == (current > 0))
        intent = OrderIntent(strategy_id=bs.id, action=SignalAction.HEDGE,
                             legs=[Leg(key, side.entry_order_side(), abs(diff))], reason=sig.reason,
                             reducing=reducing, trade_id=hedge.trade_id if hedge else None)
        decision = self.gateway.evaluate(intent)
        if not decision.approved:
            return
        approved = decision.intent.legs[0].lots or 0
        if hedge is None:
            hedge = new_trade(new_id("h-"), bs.id, Side.LONG if target > 0 else Side.SHORT,
                              [TradeLeg(key, key, lot)], meta={"role": "hedge"}, reason=sig.reason)
            self.book.add(hedge)
        hedge.primary.target_qty = (current + side.value * approved) * lot
        order = self.oms.new_order(strategy_id=bs.id, symbol=key, instrument=key, side=side.entry_order_side(),
                                   lots=approved, lot_size=lot, purpose=Purpose.HEDGE, trade_id=hedge.trade_id,
                                   decision_price=self.rc.price(key))
        self.oms.submit(order)

    # ------------------------------------------------------------------------------------
    # Fills
    # ------------------------------------------------------------------------------------
    def _process_fills(self) -> None:
        for order, fill in self.oms.poll():
            trade = self.book.apply_fill(order, fill)
            self._invalidate_nav()
            if trade is None:
                continue
            if order.purpose is Purpose.HEDGE and trade.primary.qty:
                trade.side = Side.LONG if trade.primary.qty > 0 else Side.SHORT
            bs = next((b for b in self.strategies if b.id == trade.strategy_id), None)
            if bs is not None:
                try:
                    bs.strategy.on_fill(None, fill)  # type: ignore[arg-type]
                except Exception:  # noqa: BLE001
                    log.exception("on_fill failed for %s", bs.id)
            if order.purpose is Purpose.ENTRY and trade.status == "OPEN" and trade.stop_price is not None \
                    and len(trade.legs) == 1 and "stop_anchored" not in trade.meta:
                # Re-anchor the stop to the actual fill, keeping the planned distance.
                dist = trade.meta.get("stop_distance", abs((trade.entry_price or 0) - trade.stop_price))
                tick = self.registry.get(trade.primary.instrument).tick_size(fill.ts.date())
                stop = (trade.entry_price or fill.price) - trade.side.value * dist
                trade.stop_price = trade.initial_stop = round(stop / tick) * tick
                trade.initial_risk = dist * abs(trade.primary.qty)
                trade.meta["stop_anchored"] = True
            if trade.status == "OPEN" and trade.stop_price is not None and len(trade.legs) == 1 \
                    and trade.trade_id not in self.stop_orders and not self.oms.open_orders(trade.trade_id, Purpose.EXIT):
                self._place_stop(trade)
            if trade.status == "CLOSED":
                ref = self.stop_orders.pop(trade.trade_id, None)
                if ref:
                    self.oms.cancel(ref)
                if order.purpose is Purpose.STOP:
                    trade.exit_reason = "protective stop hit"
                self.store.save_trade(trade)
        # Entries that can no longer fill (rejected / expired limit) are cancelled.
        for t in self.book.open_trades():
            if t.status == "PENDING" and t.is_flat and not self.oms.open_orders(t.trade_id):
                self.book.cancel_pending(t.trade_id, "entry not filled")
            elif t.status == "PENDING" and not t.is_flat and not self.oms.open_orders(t.trade_id, Purpose.ENTRY) \
                    and t.meta.get("role") != "hedge":
                # Partially established and nothing working: treat what we have as the trade.
                t.status = "OPEN"
                t.entry_price = t.primary.avg_price or None
                t.entry_ts = t.entry_ts or self._now

    def _place_stop(self, trade) -> None:
        """Protective stop resting with the broker so protection survives an outage."""
        leg = trade.primary
        lots = abs(leg.qty) // leg.lot_size
        if lots <= 0:
            return
        intent = OrderIntent(strategy_id=trade.strategy_id, action=SignalAction.ADJUST_STOP,
                             legs=[Leg(leg.instrument, trade.side.exit_order_side(), lots)],
                             reason="protective stop", reducing=True, trade_id=trade.trade_id,
                             stop_price=trade.stop_price, source="engine")
        if not self.gateway.evaluate(intent).approved:
            self.alerts.append(f"protective stop for {trade.trade_id} not approved")
            return
        o = self.oms.new_order(strategy_id=trade.strategy_id, symbol=leg.symbol, instrument=leg.instrument,
                               side=trade.side.exit_order_side(), lots=lots, lot_size=leg.lot_size,
                               purpose=Purpose.STOP, order_type=OrderType.SL_M, trade_id=trade.trade_id,
                               trigger_price=trade.stop_price, decision_price=trade.stop_price)
        self.oms.submit(o)
        if o.status is not OrderStatus.REJECTED:
            self.stop_orders[trade.trade_id] = o.client_ref

    def _option_quotes(self, day: date, feed_broker: bool) -> None:
        """Refresh quotes for option contracts held or working (model chain in paper/backtest)."""
        if not self.option_chain:
            return
        contracts = {}
        for t in self.book.open_trades():
            for leg in t.legs.values():
                if leg.contract:
                    contracts[leg.symbol] = (leg.instrument, leg.contract)
        for sym, (inst, c) in contracts.items():
            if c.expiry < day:
                continue
            q = self.option_chain.quote(inst, c.expiry, c.strike, c.right, day)
            self.rc.quotes[sym] = Quote(sym, q["mid"], self._now, q["bid"], q["ask"])
            if feed_broker and isinstance(self.broker, PaperBroker):
                self.broker.on_quote(sym, q["mid"], q["bid"], q["ask"])

    def _on_reject(self, order: Order, reason: str) -> None:
        self.rejections[order.strategy_id] = self.rejections.get(order.strategy_id, 0) + 1
        self.gateway.on_reject(order.symbol, order.strategy_id)
        if order.purpose is Purpose.STOP:
            self.alerts.append(f"protective stop rejected for {order.trade_id}: {reason}")

    # ------------------------------------------------------------------------------------
    # Safety
    # ------------------------------------------------------------------------------------
    def _safety_checks(self, day: date, keys: list[str], bars: dict[str, Bar], equity: float) -> None:
        if self.mode is Mode.BACKTEST:
            for t in self.ks.simulate_operator_resets(equity):
                if t.level is KSLevel.STRATEGY:
                    # A reset strategy is measured from a new peak, as after a real review.
                    self.strategy_peaks[t.scope] = self.book.strategy_pnl(t.scope, self.prices())
        self.ks.evaluate_account(equity, self.day_start_equity, self.week_start_equity)
        prices = self.prices()
        nav = equity
        for bs in self.strategies:
            pnl = self.book.strategy_pnl(bs.id, prices)
            peak = max(self.strategy_peaks.get(bs.id, 0.0), pnl)
            self.strategy_peaks[bs.id] = peak
            self.strategy_pnl_hist[bs.id].append(pnl)
            closed = self.book.closed_trades(bs.id)
            assumed = sum(t.meta.get("assumed_slippage", 0.0) for t in closed) or None
            stats = compute_stats(closed, self.book.open_trades(bs.id, include_pending=False),
                                  assumed_slippage=assumed if self.mode is not Mode.BACKTEST else None,
                                  rejections=self.rejections.get(bs.id, 0))
            self.ks.evaluate_strategy(bs.id, pnl, peak, nav, stats)
        # Data health per instrument: missing bar for an instrument we hold -> stale.
        held = {l.instrument for _, l in self.book.legs() if l.qty and l.contract is None}
        for k in keys:
            if k in bars:
                self.ks.instrument_data_clean(k)
            elif k in held and self.calendar.is_trading_day(day):
                self.ks.instrument_data_fault(k, f"no bar for {k} on {day}")
        # Reconciliation: broker is the truth (FR-11.3).
        res = reconcile(self.book.net_positions(), self.broker.positions())
        if res.ok != self.rc.health.reconciled:
            self.audit.record("reconciliation", "reconciler", {"ok": res.ok, "detail": res.banner()}, ts=self._now)
        self.rc.health.reconciled = res.ok
        if not res.ok:
            self.alerts.append(res.banner())
        self.rc.health.broker_connected = self.broker.connected()

    def _execute_killswitch_actions(self) -> None:
        for trip in self.ks.actions_due():
            if trip.level in (KSLevel.SYSTEM, KSLevel.DRAWDOWN):
                trades = self.book.open_trades()
            elif trip.level is KSLevel.STRATEGY:
                trades = self.book.open_trades(trip.scope)
            else:
                trades = [t for t in self.book.open_trades() if any(l.instrument == trip.scope
                                                                     for l in t.legs.values())]
            fraction = self.cfg.killswitch.reduce_fraction if trip.action is KSAction.REDUCE else 1.0
            unresolved = []
            for t in trades:
                if t.legs and any(self.registry.get(l.instrument).flags.banned for l in t.legs.values()):
                    # FR-9.5: exits may be restricted; propose a substitute hedge for confirmation.
                    self.alerts.append(f"{t.trade_id}: instrument in ban/circuit - propose index hedge")
                if not self.exit_trade(t.trade_id, f"kill switch {trip.level.value}:{trip.scope}",
                                       fraction, source="killswitch") and t.status == "OPEN":
                    unresolved.append(t.trade_id)
            if unresolved:
                # FR-9.8: unresolved-exposure list shown until empty.
                self.alerts.append(f"unresolved exposure after {trip.level.value} trip: {unresolved}")
            self.ks.mark_executed(trip)

    # ------------------------------------------------------------------------------------
    # Monitoring
    # ------------------------------------------------------------------------------------
    def _update_alignment(self, ts: pd.Timestamp) -> None:
        prices = self.prices()
        for bs in self.strategies:
            trades = self.book.open_trades(bs.id, include_pending=False)
            if not trades:
                continue
            ctx = self._context(bs, ts)
            alloc = self.cfg.portfolio.strategies[bs.id]
            for t in trades:
                if t.meta.get("role") == "hedge":
                    continue
                try:
                    exp = bs.strategy.expected_path(ctx, copy.deepcopy(t))
                except Exception:  # noqa: BLE001
                    log.exception("expected_path failed for %s", bs.id)
                    continue
                res = evaluate_alignment(t, exp, t.r_multiple(prices), t.bars_held)
                t.alignment.append((ts.date().isoformat(), res.state.value, res.failing))
                if res.state.value == "OFF_THESIS":
                    if alloc.on_red == "exit":
                        self.exit_trade(t.trade_id, "alignment red: " + "; ".join(res.failing))
                    elif alloc.on_red == "reduce" and not t.meta.get("red_reduced"):
                        if self.exit_trade(t.trade_id, "alignment red: reduce", self.cfg.killswitch.reduce_fraction):
                            t.meta["red_reduced"] = True

    # ------------------------------------------------------------------------------------
    # Operator actions (PRD s.8, s.9)
    # ------------------------------------------------------------------------------------
    def manual_exit(self, trade_id: str, fraction: float = 1.0, reason: str = "") -> bool:
        self.audit.record("manual_exit", "operator", {"trade_id": trade_id, "fraction": fraction,
                          "reason": reason}, ts=self._now)
        return self.exit_trade(trade_id, f"manual exit {reason}".strip(), fraction, source="manual")

    def exit_all(self, strategy_id: str | None = None, instrument: str | None = None, reason: str = "") -> int:
        n = 0
        for t in self.book.open_trades(strategy_id):
            if instrument and not any(l.instrument == instrument for l in t.legs.values()):
                continue
            n += int(self.manual_exit(t.trade_id, 1.0, reason))
        return n



def trade_from_dict(d: dict[str, Any]) -> Trade:
    """Rebuild a ``Trade`` from its JSON form in the state database."""
    legs = {}
    for sym, l in d["legs"].items():
        c = l.get("contract")
        contract = OptionContract(c["underlying"], date.fromisoformat(c["expiry"]), float(c["strike"]),
                                  c["right"]) if c else None
        legs[sym] = TradeLeg(**{**l, "contract": contract})
    out = dict(d)
    out["legs"] = legs
    out["side"] = Side(d["side"])
    for k in ("entry_ts", "exit_ts"):
        if out.get(k):
            out[k] = datetime.fromisoformat(out[k])
    out["alignment"] = [tuple(a) for a in d.get("alignment", [])]
    return Trade(**out)
