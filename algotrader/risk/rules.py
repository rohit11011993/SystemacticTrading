"""Risk-gateway rule catalogue (PRD s.7). All thresholds come from the effective limits.

Each rule returns a ``RuleResult`` with ALLOW, RESIZE (a scale factor in (0, 1)) or BLOCK.
Risk-reducing intents skip every rule except those with ``applies_to_reducing = True``
(price sanity and rate limits, FR-7.1).

Limit keys used (all configurable in ``config/risk.yaml``):

    max_risk_per_trade_pct, require_stop, max_order_lots, max_order_value,
    max_position_pct_nav, max_adv_pct, max_gross_exposure_pct_nav, max_net_exposure_pct_nav,
    max_sector_exposure_pct_nav, max_cluster_risk_pct_nav, max_margin_util_pct,
    margin_buffer_pct, price_collar_pct, max_quote_age_sec, max_orders_per_minute,
    duplicate_window_sec, rejection_storm_count, rejection_storm_window_sec,
    max_cost_to_atr_pct, max_cost_to_edge_pct, blocked_windows, options{...}
"""

from __future__ import annotations

import math
from abc import ABC, abstractmethod
from collections import deque
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import TYPE_CHECKING, Any

from ..core.models import OrderIntent, OrderSide, SignalAction, Verdict

if TYPE_CHECKING:
    from .gateway import RiskContext


@dataclass
class RuleResult:
    rule: str
    action: Verdict = Verdict.ALLOW
    detail: str = "ok"
    scale: float = 1.0
    limit: Any = None
    value: Any = None

    @property
    def passed(self) -> bool:
        return self.action is Verdict.ALLOW


class Rule(ABC):
    name: str = "rule"
    applies_to_reducing: bool = False

    @abstractmethod
    def check(self, intent: OrderIntent, limits: dict[str, Any], rc: "RiskContext") -> RuleResult: ...

    # helpers
    def ok(self, detail: str = "ok", **kw: Any) -> RuleResult:
        return RuleResult(self.name, Verdict.ALLOW, detail, **kw)

    def block(self, detail: str, **kw: Any) -> RuleResult:
        return RuleResult(self.name, Verdict.BLOCK, detail, **kw)

    def resize(self, scale: float, detail: str, **kw: Any) -> RuleResult:
        scale = max(0.0, min(1.0, scale))
        return RuleResult(self.name, Verdict.RESIZE, detail, scale=scale, **kw)


def _is_entry(intent: OrderIntent) -> bool:
    return intent.action is SignalAction.ENTER


# --------------------------------------------------------------------------------------------
class SystemStateRule(Rule):
    """Drawdown state and active kill switches; reconciliation / connectivity fail-closed."""

    name = "system_state"

    def check(self, intent, limits, rc):
        reasons: list[str] = []
        for leg in intent.legs:
            reasons += rc.killswitch.entry_block_reasons(intent.strategy_id, leg.instrument)
        if not rc.health.reconciled:
            reasons.append("unresolved reconciliation mismatch (FR-8.6)")
        if not rc.health.broker_connected:
            reasons.append("broker disconnected")
        if not rc.health.session_valid:
            reasons.append("no valid broker session")
        if reasons:
            return self.block("; ".join(sorted(set(reasons))))
        return self.ok()


class DataHealthRule(Rule):
    """Quote freshness, cross-check status and adapter connectivity (FR-6.4, FR-6.9)."""

    name = "data_health"

    def check(self, intent, limits, rc):
        if not rc.health.data_connected:
            return self.block("market-data feed disconnected")
        max_age = float(limits.get("max_quote_age_sec", 3600))
        now = rc.now()
        for leg in intent.legs:
            q = rc.quotes.get(leg.symbol)
            if q is None:
                return self.block(f"no quote for {leg.symbol}")
            age = (now - q.ts).total_seconds()
            if age > max_age:
                return self.block(f"{leg.symbol} quote is {age:.0f}s old (limit {max_age:.0f}s)",
                                  limit=max_age, value=age)
            if not q.cross_check_ok:
                return self.block(f"{leg.symbol} failed secondary-source cross-check")
        return self.ok()


class MarketStateRule(Rule):
    """F&O ban, circuit, event blackout, expiry cut-off, session window and regime permission."""

    name = "market_state"

    def check(self, intent, limits, rc):
        today = rc.now().date()
        windows = set(limits.get("blocked_windows", []))
        if intent.action is SignalAction.ENTER and rc.regime_permission(intent.strategy_id) <= 0:
            return self.block(f"regime {rc.regime_name()} does not permit {intent.strategy_id}")
        for leg in intent.legs:
            inst = rc.registry.get(leg.instrument)
            if inst.flags.banned:
                return self.block(f"{leg.instrument} is in the F&O ban period")
            if inst.flags.circuit:
                return self.block(f"{leg.instrument} has hit a circuit")
            if "event_calendar" in windows:
                reason = rc.calendar.blackout(today, leg.instrument, inst.flags.expiries)
                if reason:
                    return self.block(f"{leg.instrument}: {reason}")
            expiry = leg.contract.expiry if leg.contract else (
                inst.next_expiry(today) if inst.flags.expiries else None)
            if expiry is not None:
                days = rc.calendar.trading_days_between(today, expiry)
                if days < inst.flags.open_cutoff_days:
                    return self.block(f"{leg.symbol} expires in {days} trading days (cut-off "
                                      f"{inst.flags.open_cutoff_days}, FR-10.5)")
        if rc.enforce_session and "last_10_min" in windows:
            close = rc.session_close(intent.legs[0].instrument)
            if close and (datetime.combine(today, close) - rc.now()) < timedelta(minutes=10) \
                    and rc.now().time() < close:
                return self.block("inside the last 10 minutes of the session")
        return self.ok()


class PriceSanityRule(Rule):
    """Collar versus last price, fat-finger size and value limits, tick-size validity."""

    name = "price_sanity"
    applies_to_reducing = True

    def check(self, intent, limits, rc):
        collar = float(limits.get("price_collar_pct", 3.0)) / 100.0
        max_lots = int(limits.get("max_order_lots", 10_000))
        max_value = float(limits.get("max_order_value", math.inf))
        today = rc.now().date()
        for leg in intent.legs:
            inst = rc.registry.get(leg.instrument)
            lots = abs(leg.lots or 0)
            if lots > max_lots:
                return self.block(f"{leg.symbol}: {lots} lots exceeds fat-finger limit {max_lots}")
            px = rc.price(leg.symbol)
            if px is None:
                return self.block(f"{leg.symbol}: no reference price")
            value = lots * inst.lot_size(today) * px
            if value > max_value:
                return self.block(f"{leg.symbol}: order value {value:,.0f} exceeds {max_value:,.0f}")
            limit_px = intent.limit_price if leg is intent.legs[0] else None
            if limit_px is not None:
                if abs(limit_px - px) / px > collar:
                    return self.block(f"{leg.symbol}: limit {limit_px} outside {collar:.1%} collar of {px}")
                tick = inst.tick_size(today)
                if abs(round(limit_px / tick) * tick - limit_px) > 1e-6:
                    return self.block(f"{leg.symbol}: limit {limit_px} not a multiple of tick {tick}")
        return self.ok()


class RateRule(Rule):
    """Orders-per-minute burst cap, duplicate intents and rejection storms (REG-4, FR-10.7).

    The per-second ceiling itself is enforced by the order manager's token bucket so that
    bursts are paced rather than dropped.
    """

    name = "rate_and_duplicates"
    applies_to_reducing = True

    def __init__(self) -> None:
        self.sent: deque[datetime] = deque()
        self.recent: deque[tuple[datetime, tuple]] = deque()
        self.rejections: deque[tuple[datetime, str]] = deque()

    @staticmethod
    def fingerprint(intent: OrderIntent) -> tuple:
        return (intent.strategy_id, intent.action.value, intent.trade_id,
                tuple((l.symbol, l.side.value, l.lots) for l in intent.legs))

    def check(self, intent, limits, rc):
        now = rc.now()
        per_min = int(limits.get("max_orders_per_minute", 60))
        while self.sent and now - self.sent[0] > timedelta(minutes=1):
            self.sent.popleft()
        if len(self.sent) + len(intent.legs) > per_min:
            return self.block(f"order rate above {per_min}/min", limit=per_min, value=len(self.sent))
        window = timedelta(seconds=float(limits.get("duplicate_window_sec", 5)))
        while self.recent and now - self.recent[0][0] > window:
            self.recent.popleft()
        fp = self.fingerprint(intent)
        if any(f == fp for _, f in self.recent):
            return self.block("duplicate of an intent sent within the duplicate window")
        storm_n = int(limits.get("rejection_storm_count", 5))
        storm_w = timedelta(seconds=float(limits.get("rejection_storm_window_sec", 60)))
        while self.rejections and now - self.rejections[0][0] > storm_w:
            self.rejections.popleft()
        if len(self.rejections) >= storm_n:
            return self.block(f"rejection storm: {len(self.rejections)} rejections in {storm_w}")
        return self.ok()

    def commit(self, intent: OrderIntent, now: datetime) -> None:
        for _ in intent.legs:
            self.sent.append(now)
        self.recent.append((now, self.fingerprint(intent)))

    def on_reject(self, now: datetime, symbol: str) -> int:
        self.rejections.append((now, symbol))
        return len(self.rejections)


class OptionsStructureRule(Rule):
    """Defined risk only: no naked short, max loss, delta bounds (PRD s.7)."""

    name = "options_structure"

    def check(self, intent, limits, rc):
        opt_legs = [l for l in intent.legs if l.contract is not None]
        if not opt_legs:
            return self.ok("not an options intent")
        o = limits.get("options", {}) or {}
        today = rc.now().date()
        if not o.get("naked_short", False):
            for s in (l for l in opt_legs if l.side is OrderSide.SELL):
                c = s.contract
                cover = [b for b in opt_legs if b.side is OrderSide.BUY and b.contract.right == c.right
                         and b.contract.expiry == c.expiry and (b.lots or 0) >= (s.lots or 0)
                         and ((c.right == "CE" and b.contract.strike > c.strike)
                              or (c.right == "PE" and b.contract.strike < c.strike))]
                if not cover:
                    return self.block(f"naked short {c.symbol}: no protective long wing")
        max_loss = structure_max_loss(intent, rc, today)
        if math.isinf(max_loss):
            return self.block("structure has unlimited loss")
        cap_pct = float(o.get("max_loss_pct_nav", 0.5))
        cap = cap_pct / 100.0 * rc.nav()
        if max_loss > cap * 1.0001:
            return self.block(f"max loss {max_loss:,.0f} above {cap_pct}% of NAV ({cap:,.0f})",
                              limit=cap, value=max_loss)
        if "max_abs_delta" in o:
            net_delta = sum((l.meta.get("delta") or 0.0) * (l.lots or 0) * l.side.sign for l in opt_legs)
            if abs(net_delta) > float(o["max_abs_delta"]):
                return self.block(f"net delta {net_delta:.2f} lots-equivalent above {o['max_abs_delta']}")
        return self.ok(f"defined max loss {max_loss:,.0f}")


def structure_max_loss(intent: OrderIntent, rc: "RiskContext", today) -> float:
    """Worst expiry payoff of an option structure, net of premium, in rupees.

    Evaluated on a grid spanning all strikes; a non-zero payoff slope at either end of the grid
    means the loss is unlimited.
    """
    legs = [l for l in intent.legs if l.contract is not None]
    strikes = sorted({l.contract.strike for l in legs})
    lo, hi = strikes[0] * 0.5, strikes[-1] * 1.5
    grid = [lo] + strikes + [hi]

    def payoff(s: float) -> float:
        total = 0.0
        for l in legs:
            units = (l.lots or 0) * rc.registry.get(l.instrument).lot_size(today)
            premium = l.ref_price if l.ref_price is not None else (rc.price(l.symbol) or 0.0)
            intrinsic = max(s - l.contract.strike, 0.0) if l.contract.right == "CE" else max(
                l.contract.strike - s, 0.0)
            total += l.side.sign * units * (intrinsic - premium)
        return total

    values = [payoff(s) for s in grid]
    slope_hi = payoff(hi * 2) - values[-1]
    slope_lo = payoff(lo / 2) - values[0]
    if slope_hi < -1e-6 or slope_lo < -1e-6:
        return math.inf
    return max(0.0, -min(values))


class RiskPerTradeRule(Rule):
    """Distance to stop x size as % of NAV; a stop or defined max loss is mandatory."""

    name = "risk_per_trade"

    def check(self, intent, limits, rc):
        if not _is_entry(intent):
            return self.ok("not an entry")
        if intent.risk_amount is None or intent.risk_amount <= 0:
            if limits.get("require_stop", True):
                return self.block("no protective stop or defined max loss")
            return self.ok("no stop (not required)")
        pct = float(limits.get("max_risk_per_trade_pct", 0.5))
        cap = pct / 100.0 * rc.nav()
        if intent.risk_amount > cap * 1.0001:
            return self.resize(cap / intent.risk_amount,
                               f"risk {intent.risk_amount:,.0f} above {pct}% of NAV ({cap:,.0f})",
                               limit=cap, value=intent.risk_amount)
        return self.ok(f"risk {intent.risk_amount:,.0f} <= {cap:,.0f}")


class AllocationRule(Rule):
    """Strategy risk share, family caps, concurrency and per-sector caps (strategy doc s.10)."""

    name = "allocation"

    def check(self, intent, limits, rc):
        if not _is_entry(intent):
            return self.ok("not an entry")
        pcfg = rc.portfolio
        alloc = pcfg.strategies.get(intent.strategy_id)
        if alloc is None:
            return self.block(f"no allocation configured for {intent.strategy_id}")
        open_trades = [t for t in rc.book.open_trades(intent.strategy_id) if t.meta.get("role") != "hedge"]
        if len(open_trades) >= alloc.max_concurrent:
            return self.block(f"{len(open_trades)} open trades = max concurrent {alloc.max_concurrent}")
        if alloc.max_per_sector is not None:
            sector = rc.registry.get(intent.legs[0].instrument).sector
            n = sum(1 for t in open_trades if rc.registry.get(t.primary.instrument).sector == sector)
            if n >= alloc.max_per_sector:
                return self.block(f"{n} open trades in sector '{sector}' = cap {alloc.max_per_sector}")
        new_risk = intent.risk_amount or 0.0
        if new_risk <= 0:
            return self.ok()
        nav = rc.nav()
        budget = pcfg.book_risk_budget_pct / 100.0 * nav
        prices = rc.prices()
        scale = 1.0
        detail = []
        used = rc.book.open_risk(prices, [intent.strategy_id])
        cap = alloc.risk_share * budget
        if used + new_risk > cap:
            scale = min(scale, max(0.0, cap - used) / new_risk)
            detail.append(f"strategy risk {used + new_risk:,.0f} > share cap {cap:,.0f}")
        fam = pcfg.families.get(alloc.family)
        if fam is not None:
            members = [s for s, a in pcfg.strategies.items() if a.family == alloc.family]
            fused = rc.book.open_risk(prices, members)
            fcap = fam.max_share * budget
            if fused + new_risk > fcap:
                scale = min(scale, max(0.0, fcap - fused) / new_risk)
                detail.append(f"family '{alloc.family}' risk {fused + new_risk:,.0f} > cap {fcap:,.0f}")
        if scale < 1.0:
            if scale <= 0:
                return self.block("; ".join(detail))
            return self.resize(scale, "; ".join(detail))
        return self.ok()


class PositionSizeRule(Rule):
    """Order lots and combined instrument notional versus caps (S1/S2 share instrument caps)."""

    name = "position_size"

    def check(self, intent, limits, rc):
        if intent.action not in (SignalAction.ENTER, SignalAction.HEDGE):
            return self.ok()
        today = rc.now().date()
        max_pct = float(limits.get("max_position_pct_nav", 100.0)) / 100.0
        nav = rc.nav()
        scale = 1.0
        for leg in intent.legs:
            if leg.contract is not None or not leg.lots:
                continue
            inst = rc.registry.get(leg.instrument)
            px = rc.price(leg.symbol) or 0.0
            unit_notional = inst.lot_size(today) * px
            held = sum(abs(l.qty) for _, l in rc.book.legs() if l.symbol == leg.symbol) * px
            new = abs(leg.lots) * unit_notional
            cap = max_pct * nav
            if held + new > cap:
                allowed_lots = max(0.0, (cap - held) / unit_notional) if unit_notional else 0.0
                scale = min(scale, math.floor(allowed_lots) / abs(leg.lots))
        if scale < 1.0:
            if scale <= 0:
                return self.block(f"instrument notional cap {max_pct:.0%} of NAV reached")
            return self.resize(scale, f"instrument notional above {max_pct:.0%} of NAV")
        return self.ok()


class LiquidityRule(Rule):
    """Order size as a percentage of average daily volume."""

    name = "liquidity"

    def check(self, intent, limits, rc):
        if not _is_entry(intent):
            return self.ok()
        default_pct = float(limits.get("max_adv_pct", 1.0))
        scale = 1.0
        for leg in intent.legs:
            inst = rc.registry.get(leg.instrument)
            pct = min(default_pct, inst.liquidity.max_adv_pct)
            cap_lots = inst.liquidity.adv_lots * pct / 100.0
            if leg.lots and abs(leg.lots) > cap_lots:
                scale = min(scale, math.floor(cap_lots) / abs(leg.lots))
        if scale < 1.0:
            if scale <= 0:
                return self.block("order larger than liquidity cap")
            return self.resize(scale, "order above % of ADV cap")
        return self.ok()


class ExposureRule(Rule):
    """Gross, beta-adjusted net, sector and correlated-cluster exposure."""

    name = "exposure"

    def check(self, intent, limits, rc):
        if intent.action not in (SignalAction.ENTER, SignalAction.HEDGE):
            return self.ok()
        today = rc.now().date()
        nav = rc.nav()
        gross = net = 0.0
        sector: dict[str, float] = {}
        for _, leg in rc.book.legs():
            if leg.contract is not None or not leg.qty:
                continue
            inst = rc.registry.get(leg.instrument)
            notional = leg.qty * (rc.price(leg.symbol) or leg.avg_price)
            gross += abs(notional)
            net += notional * inst.beta
            sector[inst.sector] = sector.get(inst.sector, 0.0) + notional
        new_gross = gross
        new_net = net
        new_sector = dict(sector)
        for leg in intent.legs:
            if leg.contract is not None or not leg.lots:
                continue
            inst = rc.registry.get(leg.instrument)
            notional = leg.side.sign * abs(leg.lots) * inst.lot_size(today) * (rc.price(leg.symbol) or 0.0)
            new_gross += abs(notional)
            new_net += notional * inst.beta
            new_sector[inst.sector] = new_sector.get(inst.sector, 0.0) + notional
        g_cap = float(limits.get("max_gross_exposure_pct_nav", math.inf)) / 100.0 * nav
        n_cap = float(limits.get("max_net_exposure_pct_nav", math.inf)) / 100.0 * nav
        s_cap = float(limits.get("max_sector_exposure_pct_nav", math.inf)) / 100.0 * nav
        if new_gross > g_cap and new_gross > gross:
            return self.block(f"gross exposure {new_gross:,.0f} > {g_cap:,.0f}")
        if abs(new_net) > n_cap and abs(new_net) > abs(net):
            return self.block(f"beta-adjusted net exposure {new_net:,.0f} > {n_cap:,.0f}")
        for sec, v in new_sector.items():
            if sec not in ("index", "none") and abs(v) > s_cap and abs(v) > abs(sector.get(sec, 0.0)):
                return self.block(f"sector '{sec}' exposure {v:,.0f} > {s_cap:,.0f}")
        # Correlated cluster: same-direction open risk within this strategy's cluster.
        if _is_entry(intent) and "max_cluster_risk_pct_nav" in limits and intent.risk_amount:
            cluster = rc.registry.get(intent.legs[0].instrument).cluster
            if cluster:
                prices = rc.prices()
                used = sum(rc.book.trade_risk(t, prices) for t in rc.book.open_trades(intent.strategy_id)
                           if rc.registry.get(t.primary.instrument).cluster == cluster
                           and t.side == intent.side)
                cap = float(limits["max_cluster_risk_pct_nav"]) / 100.0 * nav
                if used + intent.risk_amount > cap * 1.0001:
                    return self.block(f"cluster '{cluster}' same-direction risk "
                                      f"{used + intent.risk_amount:,.0f} > {cap:,.0f}")
        return self.ok()


class MarginRule(Rule):
    """Post-trade margin utilisation plus a buffer for margin spikes."""

    name = "margin"

    def check(self, intent, limits, rc):
        if intent.action not in (SignalAction.ENTER, SignalAction.HEDGE):
            return self.ok()
        cap_pct = float(limits.get("max_margin_util_pct", 40.0)) - float(limits.get("margin_buffer_pct", 0.0))
        used = rc.margin_used()
        new = rc.margin_for_intent(intent)
        nav = rc.nav()
        util = (used + new) / nav * 100.0 if nav > 0 else math.inf
        if util > cap_pct:
            return self.block(f"post-trade margin {util:.1f}% > {cap_pct:.1f}%", limit=cap_pct, value=util)
        return self.ok(f"post-trade margin {util:.1f}%")


class CostRule(Rule):
    """Expected edge versus estimated round-trip cost (STT, brokerage, slippage)."""

    name = "cost"

    def check(self, intent, limits, rc):
        if not _is_entry(intent):
            return self.ok()
        today = rc.now().date()
        cost = 0.0
        atr_rupees = 0.0
        for leg in intent.legs:
            inst = rc.registry.get(leg.instrument)
            units = abs(leg.lots or 0) * inst.lot_size(today)
            px = leg.ref_price or rc.price(leg.symbol) or 0.0
            cost += rc.costs.round_trip(inst.cost_profile, px, units, inst.tick_size(today), today)
            if leg is intent.legs[0] and intent.atr:
                atr_rupees = intent.atr * units
        if intent.expected_edge:
            pct = float(limits.get("max_cost_to_edge_pct", 25.0))
            if cost > pct / 100.0 * intent.expected_edge:
                return self.block(f"round-trip cost {cost:,.0f} > {pct}% of expected edge "
                                  f"{intent.expected_edge:,.0f}", value=cost)
        elif atr_rupees > 0:
            pct = float(limits.get("max_cost_to_atr_pct", 15.0))
            if cost > pct / 100.0 * atr_rupees:
                return self.block(f"round-trip cost {cost:,.0f} > {pct}% of one ATR ({atr_rupees:,.0f})",
                                  value=cost)
        return self.ok(f"round-trip cost {cost:,.0f}")


def default_rules() -> list[Rule]:
    """Evaluation order: cheap fail-closed checks first, resizing rules before blocking ones."""
    return [SystemStateRule(), DataHealthRule(), MarketStateRule(), PriceSanityRule(), RateRule(),
            OptionsStructureRule(), RiskPerTradeRule(), AllocationRule(), PositionSizeRule(),
            LiquidityRule(), ExposureRule(), MarginRule(), CostRule()]

