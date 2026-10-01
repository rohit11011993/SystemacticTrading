"""The risk gateway (PRD s.7, R4): the only component that can approve an order.

Every order intent - from a strategy, the manual exit button, or a kill-switch flatten - passes
through ``RiskGateway.evaluate`` and gets ALLOW, RESIZE, BLOCK (or FLATTEN). Each decision is
logged with its inputs, every rule result, the limit values applied and the configuration hash
(FR-7.3), so any order can be explained afterwards.

Failing closed (NFR-1): if data is stale, the broker is disconnected or reconciliation has
failed, only risk-reducing orders pass. Risk-reducing orders still go through price sanity and
rate limits (FR-7.1).

Process model note: the PRD runs the gateway as its own process behind authenticated local
IPC. This module is transport-agnostic; ``RiskGateway`` can be hosted in-process (backtest,
paper) or behind an IPC server without changes to the rule chain.
"""

from __future__ import annotations

import copy
import math
import time as _time
from dataclasses import dataclass, field
from datetime import datetime, time, timedelta
from typing import Any, Callable

from ..core.audit import AuditLog
from ..core.calendar import EventCalendar
from ..core.config import PortfolioConfig, RiskConfig, is_loosening
from ..core.costs import CostTable
from ..core.instruments import InstrumentRegistry
from ..core.models import OrderIntent, Quote, Verdict
from ..portfolio.book import Book
from ..regime import RegimeEngine
from .killswitch import KillSwitchManager
from .rules import RateRule, Rule, RuleResult, default_rules, structure_max_loss


@dataclass
class HealthFlags:
    """Connectivity / integrity flags maintained by the engine, reconciler and watchdog."""

    data_connected: bool = True
    broker_connected: bool = True
    session_valid: bool = True
    reconciled: bool = True


@dataclass
class Decision:
    verdict: Verdict
    intent: OrderIntent                 # possibly resized copy
    results: list[RuleResult]
    limits: dict[str, Any]
    config_hash: str
    latency_ms: float
    would_block: bool = False           # dry-run: what the gateway would have done

    @property
    def approved(self) -> bool:
        return self.verdict in (Verdict.ALLOW, Verdict.RESIZE)

    @property
    def reasons(self) -> list[str]:
        return [f"{r.rule}: {r.detail}" for r in self.results if not r.passed]


@dataclass
class RiskContext:
    """Read access the rules need. Owned by the gateway, refreshed by the engine."""

    registry: InstrumentRegistry
    calendar: EventCalendar
    costs: CostTable
    book: Book
    killswitch: KillSwitchManager
    regime: RegimeEngine
    portfolio: PortfolioConfig
    now: Callable[[], datetime]
    nav: Callable[[], float]
    quotes: dict[str, Quote] = field(default_factory=dict)
    health: HealthFlags = field(default_factory=HealthFlags)
    enforce_session: bool = False
    sessions: dict[str, Any] = field(default_factory=dict)

    def price(self, symbol: str) -> float | None:
        q = self.quotes.get(symbol)
        return q.last if q else None

    def prices(self) -> dict[str, float]:
        return {s: q.last for s, q in self.quotes.items()}

    def regime_permission(self, strategy_id: str) -> float:
        return self.regime.permission(strategy_id)

    def regime_name(self) -> str:
        return self.regime.current.value if self.regime.current else "UNKNOWN"

    def session_close(self, instrument: str) -> time | None:
        sess = self.sessions.get(self.registry.get(instrument).session)
        return sess.close if sess else None

    # -- margin estimates ------------------------------------------------------------------
    def margin_used(self) -> float:
        """Approximate margin held by open trades (futures: % of notional; options: max loss)."""
        total = 0.0
        for t in self.book.open_trades():
            if any(l.contract for l in t.legs.values()):
                total += t.meta.get("margin", t.initial_risk)
                continue
            for leg in t.legs.values():
                qty = leg.qty or leg.target_qty
                inst = self.registry.get(leg.instrument)
                px = self.price(leg.symbol) or leg.avg_price
                total += abs(qty) * px * inst.margin_pct / 100.0
        return total

    def margin_for_intent(self, intent: OrderIntent) -> float:
        today = self.now().date()
        if any(l.contract for l in intent.legs):
            ml = structure_max_loss(intent, self, today)
            return ml if math.isfinite(ml) else math.inf
        total = 0.0
        for leg in intent.legs:
            inst = self.registry.get(leg.instrument)
            px = self.price(leg.symbol) or leg.ref_price or 0.0
            total += abs(leg.lots or 0) * inst.lot_size(today) * px * inst.margin_pct / 100.0
        return total


class RiskGateway:
    def __init__(self, rc: RiskContext, risk_cfg: RiskConfig, audit: AuditLog, config_hash: str,
                 profiles: dict[str, str | None] | None = None, rules: list[Rule] | None = None,
                 dry_run: bool = False):
        self.rc = rc
        self.risk_cfg = risk_cfg
        self.audit = audit
        self.config_hash = config_hash
        self.profiles = profiles or {}      # strategy id -> binding risk profile
        self.rules = rules or default_rules()
        self.dry_run = dry_run
        self.rate_rule = next((r for r in self.rules if isinstance(r, RateRule)), None)

    # -- main entry point ------------------------------------------------------------------
    def limits_for(self, intent: OrderIntent, trade_overrides: dict[str, Any] | None = None) -> dict[str, Any]:
        inst = self.rc.registry.get(intent.legs[0].instrument)
        return self.risk_cfg.effective_limits(intent.strategy_id, inst.key, inst.type.value,
                                              self.profiles.get(intent.strategy_id), trade_overrides)

    def evaluate(self, intent: OrderIntent, commit: bool = True,
                 trade_overrides: dict[str, Any] | None = None) -> Decision:
        """Run the rule chain. ``commit=False`` is the pre-trade what-if query (FR-7.8)."""
        t0 = _time.perf_counter()
        if not intent.legs:
            raise ValueError("intent has no legs")
        work = copy.deepcopy(intent)
        limits = self.limits_for(work, trade_overrides)
        results: list[RuleResult] = []
        verdict = Verdict.ALLOW
        for rule in self.rules:
            if work.reducing and not rule.applies_to_reducing:
                continue
            res = rule.check(work, limits, self.rc)
            results.append(res)
            if res.action is Verdict.BLOCK:
                verdict = Verdict.BLOCK
            elif res.action is Verdict.RESIZE and verdict is not Verdict.BLOCK:
                if not self._apply_scale(work, res.scale):
                    res.action, res.detail = Verdict.BLOCK, res.detail + " (resized to zero)"
                    verdict = Verdict.BLOCK
                else:
                    verdict = Verdict.RESIZE

        # A system-level FLATTEN/HALT trip turns a blocked entry into a FLATTEN instruction.
        if verdict is Verdict.BLOCK and not work.reducing and any(
                "FLATTEN" in r.detail or "HALT" in r.detail for r in results if r.rule == "system_state"):
            verdict = Verdict.FLATTEN

        would_block = False
        if self.dry_run and verdict in (Verdict.BLOCK, Verdict.FLATTEN):
            would_block, verdict, work = True, Verdict.ALLOW, copy.deepcopy(intent)

        decision = Decision(verdict, work, results, limits, self.config_hash,
                            (_time.perf_counter() - t0) * 1000.0, would_block)
        if commit:
            if decision.approved and self.rate_rule:
                self.rate_rule.commit(work, self.rc.now())
            self.audit.record("gateway_decision", work.source, {
                "intent_id": work.intent_id, "strategy": work.strategy_id, "action": work.action.value,
                "reducing": work.reducing, "reason": work.reason,
                "legs_requested": [(l.symbol, l.side.value, l.lots) for l in intent.legs],
                "legs_approved": [(l.symbol, l.side.value, l.lots) for l in work.legs],
                "risk_amount": work.risk_amount, "verdict": decision.verdict.value,
                "would_block": would_block,
                "rules": [(r.rule, r.action.value, r.detail) for r in results],
                "limits": limits, "config_hash": self.config_hash,
                "latency_ms": round(decision.latency_ms, 3)}, ts=self.rc.now())
        return decision

    def what_if(self, intent: OrderIntent) -> Decision:
        """Resulting size and reasons, without side effects (FR-7.8)."""
        return self.evaluate(intent, commit=False)

    def on_reject(self, symbol: str, strategy_id: str) -> None:
        """Called by the order manager on a broker rejection; storms trip kill switches."""
        if not self.rate_rule:
            return
        n = self.rate_rule.on_reject(self.rc.now(), symbol)
        storm = int(self.risk_cfg.global_limits.get("rejection_storm_count", 5))
        if n >= storm:
            from .killswitch import KSAction, KSLevel
            ks = self.rc.killswitch
            ks.trip(KSLevel.INSTRUMENT, symbol, KSAction.BLOCK_ENTRIES, "rejection storm",
                    strategy_id=strategy_id)
            ks.trip(KSLevel.STRATEGY, strategy_id, KSAction.BLOCK_ENTRIES, "rejection storm")

    @staticmethod
    def _apply_scale(intent: OrderIntent, scale: float) -> bool:
        """Scale every leg's lots by ``scale`` (floor). Returns False if any leg hits zero."""
        old_primary = abs(intent.legs[0].lots or 0)
        for leg in intent.legs:
            if leg.lots:
                leg.lots = int(math.floor(abs(leg.lots) * scale + 1e-9))
                if leg.lots == 0:
                    return False
        if intent.risk_amount and old_primary:
            intent.risk_amount *= abs(intent.legs[0].lots or 0) / old_primary
        if intent.expected_edge and old_primary:
            intent.expected_edge *= abs(intent.legs[0].lots or 0) / old_primary
        return True


# --------------------------------------------------------------------------------------------
# Limit changes: tighten immediately, loosen only after reason + confirmation + cooling-off
# --------------------------------------------------------------------------------------------
@dataclass
class LimitChange:
    change_id: int
    level: str             # global | asset_class | instruments | strategies | profiles
    scope: str | None      # e.g. strategy id; None for global
    key: str
    old: Any
    new: Any
    reason: str
    actor: str
    requested: datetime
    effective: datetime
    confirmed: bool = False
    applied: bool = False


class LimitChangeManager:
    """FR-7.4: tightening takes effect immediately; loosening needs a written reason, a
    confirmation step and a cooling-off delay (default 24 hours)."""

    def __init__(self, risk_cfg: RiskConfig, audit: AuditLog, clock: Callable[[], datetime],
                 cooloff_hours: float = 24.0):
        self.cfg = risk_cfg
        self.audit = audit
        self.clock = clock
        self.cooloff = timedelta(hours=cooloff_hours)
        self.pending: dict[int, LimitChange] = {}
        self._next = 1

    def _target(self, level: str, scope: str | None) -> dict[str, Any]:
        if level == "global":
            return self.cfg.global_limits
        container = getattr(self.cfg, level)
        return container.setdefault(scope, {})

    def propose(self, level: str, scope: str | None, key: str, value: Any, reason: str,
                actor: str) -> LimitChange:
        target = self._target(level, scope)
        old = target.get(key)
        now = self.clock()
        loosen = is_loosening(key, old, value)
        if loosen and not reason.strip():
            raise ValueError("loosening a limit requires a written reason")
        ch = LimitChange(self._next, level, scope, key, old, value, reason, actor, now,
                         now + self.cooloff if loosen else now, confirmed=not loosen)
        self._next += 1
        self.audit.record("limit_change_proposed", actor, {
            "level": level, "scope": scope, "key": key, "old": old, "new": value,
            "loosening": loosen, "reason": reason, "effective": ch.effective}, ts=now)
        if not loosen:
            self._apply(ch)
        else:
            self.pending[ch.change_id] = ch
        return ch

    def confirm(self, change_id: int, actor: str) -> None:
        ch = self.pending[change_id]
        ch.confirmed = True
        self.audit.record("limit_change_confirmed", actor, {"change_id": change_id}, ts=self.clock())

    def apply_due(self) -> list[LimitChange]:
        now = self.clock()
        done = []
        for cid, ch in list(self.pending.items()):
            if ch.confirmed and now >= ch.effective:
                self._apply(ch)
                done.append(ch)
                del self.pending[cid]
        return done

    def _apply(self, ch: LimitChange) -> None:
        self._target(ch.level, ch.scope)[ch.key] = ch.new
        ch.applied = True
        self.audit.record("limit_change_applied", ch.actor, {
            "level": ch.level, "scope": ch.scope, "key": ch.key, "old": ch.old, "new": ch.new},
            ts=self.clock())

