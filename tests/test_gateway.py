"""Gateway limit tests: for every rule a breach is blocked and a compliant order passes
(PRD s.16 release gate), plus a randomised order stream with zero escapes."""

from datetime import date, timedelta

import numpy as np

from algotrader.core.models import (Leg, OptionContract, OrderIntent, OrderSide, OrderType, Side,
                                    SignalAction, Verdict)
from algotrader.regime import Regime
from algotrader.risk.killswitch import KSAction, KSLevel

S1 = "S1_ADAPTIVE_TREND"
S5 = "S5_RANGE_IRON_CONDOR"


def entry(key="NIFTY_FUT", lots=1, stop_dist=200.0, px=25000.0, sid=S1, side=Side.LONG, **kw):
    risk = stop_dist * lots * 65 if key == "NIFTY_FUT" else kw.pop("risk", None)
    return OrderIntent(strategy_id=sid, action=SignalAction.ENTER,
                       legs=[Leg(key, side.entry_order_side(), lots, ref_price=px)], reason="test",
                       reducing=False, side=side, stop_price=px - side.value * stop_dist, ref_price=px,
                       risk_amount=kw.pop("risk_amount", risk), **kw)


def test_compliant_entry_passes(gateway_env):
    gw, *_ = gateway_env
    d = gw.evaluate(entry())
    assert d.verdict is Verdict.ALLOW, d.reasons


def test_missing_stop_blocked(gateway_env):
    gw, *_ = gateway_env
    d = gw.evaluate(entry(risk_amount=None))
    assert d.verdict is Verdict.BLOCK and any("stop" in r for r in d.reasons)


def test_oversized_risk_is_resized(gateway_env):
    gw, *_ = gateway_env
    # 0.5% of 50 lakh = 25,000. 4 lots x 200 x 65 = 52,000 -> resized to 1 lot.
    d = gw.evaluate(entry(lots=4))
    assert d.verdict is Verdict.RESIZE and d.intent.legs[0].lots == 1
    assert d.intent.risk_amount <= 25_000


def test_killswitch_blocks_entries_but_not_exits(gateway_env):
    gw, rc, ks, _ = gateway_env
    ks.trip(KSLevel.STRATEGY, S1, KSAction.BLOCK_ENTRIES, "test")
    assert gw.evaluate(entry()).verdict is Verdict.BLOCK
    exit_intent = OrderIntent(strategy_id=S1, action=SignalAction.EXIT,
                              legs=[Leg("NIFTY_FUT", OrderSide.SELL, 1)], reason="exit", reducing=True)
    assert gw.evaluate(exit_intent).approved


def test_system_flatten_returns_flatten(gateway_env):
    gw, rc, ks, _ = gateway_env
    ks.manual_kill(flatten=True)
    assert gw.evaluate(entry()).verdict is Verdict.FLATTEN


def test_stale_quote_and_cross_check_blocked(gateway_env, clock):
    gw, rc, *_ = gateway_env
    rc.quotes["NIFTY_FUT"].ts = clock() - timedelta(hours=5)
    assert gw.evaluate(entry()).verdict is Verdict.BLOCK
    rc.quotes["NIFTY_FUT"].ts = clock()
    rc.quotes["NIFTY_FUT"].cross_check_ok = False
    assert gw.evaluate(entry()).verdict is Verdict.BLOCK


def test_market_state_ban_and_regime(gateway_env):
    gw, rc, *_ = gateway_env
    rc.registry.set_flag("HDFCBANK_FUT", banned=True)
    d = gw.evaluate(entry("HDFCBANK_FUT", px=1600, stop_dist=40, risk=40 * 550, sid="S2_CHANNEL_BREAKOUT"))
    assert d.verdict is Verdict.BLOCK and any("ban" in r for r in d.reasons)
    rc.regime.current = Regime.TREND_STRESSED       # S5 not permitted in this regime
    assert gw.evaluate(entry()).approved            # S1 still permitted (0.5x)
    rc.regime.current = None
    assert gw.evaluate(entry()).verdict is Verdict.BLOCK   # unknown regime fails closed


def test_event_blackout(gateway_env, clock):
    from algotrader.core.calendar import CalendarConfig, EventCalendar, MarketEvent
    gw, rc, *_ = gateway_env
    rc.calendar = EventCalendar(CalendarConfig(events=[MarketEvent(date=clock().date() + timedelta(days=1),
                                                                   type="rbi_policy")]))
    d = gw.evaluate(entry())
    assert d.verdict is Verdict.BLOCK and any("event window" in r for r in d.reasons)


def test_price_collar_tick_and_fat_finger(gateway_env):
    gw, *_ = gateway_env
    assert gw.evaluate(entry(order_type=OrderType.LIMIT, limit_price=26500.0)).verdict is Verdict.BLOCK
    assert gw.evaluate(entry(order_type=OrderType.LIMIT, limit_price=25000.03)).verdict is Verdict.BLOCK
    assert gw.evaluate(entry(order_type=OrderType.LIMIT, limit_price=25010.0)).approved
    big = OrderIntent(strategy_id=S1, action=SignalAction.EXIT, legs=[Leg("NIFTY_FUT", OrderSide.SELL, 500)],
                      reason="fat finger", reducing=True)
    assert gw.evaluate(big).verdict is Verdict.BLOCK          # price sanity applies to exits too


def test_duplicate_and_rejection_storm(gateway_env):
    gw, rc, ks, _ = gateway_env
    i = entry()
    assert gw.evaluate(i).approved
    assert gw.evaluate(entry()).verdict is Verdict.BLOCK      # identical intent within 5 s
    for _ in range(5):
        gw.on_reject("NIFTY_FUT", S1)
    assert ks.entry_block_reasons(S1, "NIFTY_FUT")            # storm tripped kill switches


def test_margin_cap(gateway_env):
    from algotrader.core.models import TradeLeg
    from algotrader.portfolio.book import new_trade
    gw, rc, *_ = gateway_env
    assert gw.evaluate(entry()).approved
    # Existing Bank Nifty position: 7 lots x 30 x 55,000 x 12% margin = 13.9 lakh (27.7% of NAV).
    # One more Nifty lot (1.95 lakh margin) takes utilisation past 35% - 5% buffer = 30%.
    t = new_trade("t-x", "S2_CHANNEL_BREAKOUT", Side.LONG,
                  [TradeLeg("BANKNIFTY_FUT", "BANKNIFTY_FUT", 30, qty=210, avg_price=55000, target_qty=210)])
    t.status = "OPEN"
    rc.book.add(t)
    d = gw.evaluate(entry(px=25000.0, stop_dist=150))
    assert d.verdict is Verdict.BLOCK and any("margin" in r for r in d.reasons)


def test_cost_rule_blocks_tiny_edge(gateway_env):
    gw, *_ = gateway_env
    d = gw.evaluate(entry(atr=5.0))          # ATR of 5 points: costs exceed 15% of one ATR
    assert d.verdict is Verdict.BLOCK and any("cost" in r for r in d.reasons)


def _condor(lots=1, naked=False):
    exp = date(2026, 9, 29)
    c = lambda k, r: OptionContract("NIFTY_OPT", exp, k, r)  # noqa: E731
    legs = [Leg("NIFTY_OPT", OrderSide.SELL, lots, contract=c(26000, "CE"), ref_price=60),
            Leg("NIFTY_OPT", OrderSide.SELL, lots, contract=c(24000, "PE"), ref_price=55)]
    if not naked:
        legs += [Leg("NIFTY_OPT", OrderSide.BUY, lots, contract=c(26300, "CE"), ref_price=25),
                 Leg("NIFTY_OPT", OrderSide.BUY, lots, contract=c(23700, "PE"), ref_price=22)]
    return OrderIntent(strategy_id=S5, action=SignalAction.ENTER, legs=legs, reason="condor",
                       reducing=False, side=Side.SHORT, risk_amount=(300 - 68) * 65 * lots)


def test_options_structure(gateway_env, clock):
    gw, rc, *_ = gateway_env
    from algotrader.core.models import Quote
    for leg in _condor(naked=False).legs:
        rc.quotes[leg.symbol] = Quote(leg.symbol, leg.ref_price, clock())
    assert gw.evaluate(_condor()).approved
    d = gw.evaluate(_condor(naked=True))
    assert d.verdict is Verdict.BLOCK and any("naked" in r for r in d.reasons)
    d = gw.evaluate(_condor(lots=3))          # max loss 3 x 15,080 > 0.5% NAV
    assert d.verdict is Verdict.BLOCK


def test_dry_run_reports_without_blocking(gateway_env):
    gw, *_ = gateway_env
    gw.dry_run = True
    d = gw.evaluate(entry(risk_amount=None))
    assert d.verdict is Verdict.ALLOW and d.would_block


def test_every_decision_is_audited(gateway_env):
    gw, rc, ks, audit = gateway_env
    gw.evaluate(entry())
    gw.what_if(entry(lots=2))                 # what-if has no side effects
    recs = [r for r in audit.records() if r["event"] == "gateway_decision"]
    assert len(recs) == 1 and recs[0]["payload"]["config_hash"]
    assert {r[0] for r in recs[0]["payload"]["rules"]} >= {"risk_per_trade", "margin", "system_state"}


def test_randomised_stream_never_breaches_risk_limit(gateway_env, clock):
    """Zero orders above the per-trade hard limit escape, whatever the stream (PRD s.16)."""
    gw, rc, *_ = gateway_env
    rng = np.random.default_rng(42)
    cap = 0.005 * rc.nav()
    for _ in range(300):
        clock.t = clock.t + timedelta(seconds=10)
        rc.quotes["NIFTY_FUT"].ts = clock.t
        lots = int(rng.integers(1, 40))
        dist = float(rng.uniform(5, 2000))
        d = gw.evaluate(entry(lots=lots, stop_dist=dist))
        if d.approved:
            assert d.intent.risk_amount <= cap * 1.0001
            assert d.intent.legs[0].lots <= lots
