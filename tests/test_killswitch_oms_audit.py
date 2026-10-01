import hashlib
import json
from datetime import timedelta

import pytest

from algotrader.core.audit import AuditLog
from algotrader.core.models import (OptionContract, OrderSide, OrderStatus, OrderType, Purpose)
from algotrader.core.state import StateStore
from algotrader.execution.broker import BrokerTimeout
from algotrader.execution.oms import OrderManager, RateLimiter, classify_rejection
from algotrader.execution.paper import PaperBroker
from algotrader.core.models import Bar
from algotrader.risk.killswitch import KillSwitchManager, KSAction, KSLevel


# -- kill switches ----------------------------------------------------------------------------
@pytest.fixture()
def ks(cfg, clock):
    c = cfg.killswitch.model_copy(deep=True)
    c.external_reset_code_hashes = [hashlib.sha256(b"ABC123").hexdigest()]
    return KillSwitchManager(c, cfg.portfolio, AuditLog(), clock)


def test_drawdown_ladder_worsens_automatically_never_recovers(ks):
    ks.evaluate_account(100.0, 100.0, 100.0)
    assert ks.evaluate_account(95.5, 95.5, 95.5) == "YELLOW"
    assert ks.evaluate_account(92.5, 92.5, 92.5) == "ORANGE"
    assert ks.risk_multiplier("S1_ADAPTIVE_TREND") == 0.5
    assert ks.entry_block_reasons("S5_RANGE_IRON_CONDOR")       # high-tail-risk stopped at Orange
    assert ks.evaluate_account(99.0, 99.0, 99.0) == "ORANGE"     # no automatic upgrade
    assert ks.evaluate_account(89.0, 89.0, 89.0) == "RED"
    assert ks.actions_due() and ks.actions_due()[0].action is KSAction.FLATTEN


def test_daily_loss_blocks_entries(ks):
    ks.evaluate_account(98.0, 100.0, 100.0)
    assert any("daily loss" in r for r in ks.entry_block_reasons("S1_ADAPTIVE_TREND"))


def test_reset_needs_reason_cooloff_and_external_key(ks, clock):
    ks.evaluate_account(100.0, 100.0, 100.0)
    ks.evaluate_account(89.0, 100.0, 100.0)                      # RED: external key required
    assert ks.request_reset(KSLevel.DRAWDOWN, "account", "  ", "op") == "a written reason is required"
    ks.request_reset(KSLevel.DRAWDOWN, "account", "reviewed with adviser", "op")
    assert ks.confirm_reset(KSLevel.DRAWDOWN, "account", "op")[0] is False   # cooling-off
    clock.t += timedelta(days=30)                                 # past cooling-off and halt period
    assert ks.confirm_reset(KSLevel.DRAWDOWN, "account", "op")[1] == "external reset key required"
    assert ks.confirm_reset(KSLevel.DRAWDOWN, "account", "op", "WRONG")[0] is False
    ok, _ = ks.confirm_reset(KSLevel.DRAWDOWN, "account", "op", "ABC123", equity=89.0)
    assert ok and ks.ladder_state == "GREEN"
    # A one-time code cannot be reused.
    ks.evaluate_account(70.0, 100.0, 100.0)
    ks.request_reset(KSLevel.DRAWDOWN, "account", "again", "op")
    clock.t += timedelta(days=30)
    assert ks.confirm_reset(KSLevel.DRAWDOWN, "account", "op", "ABC123")[0] is False


def test_only_data_health_trips_auto_reset(ks, clock):
    ks.instrument_data_fault("NIFTY_FUT", "stale")
    ks.trip(KSLevel.INSTRUMENT, "GOLD_FUT", KSAction.BLOCK_ENTRIES, "loss budget")
    clock.t += timedelta(minutes=1)
    ks.instrument_data_clean("NIFTY_FUT")
    ks.instrument_data_clean("GOLD_FUT")
    clock.t += timedelta(minutes=20)
    ks.instrument_data_clean("NIFTY_FUT")
    ks.instrument_data_clean("GOLD_FUT")
    assert (KSLevel.INSTRUMENT, "NIFTY_FUT") not in ks.trips
    assert (KSLevel.INSTRUMENT, "GOLD_FUT") in ks.trips


def test_escalation_instrument_to_strategy(ks):
    for key in ("NIFTY_FUT", "GOLD_FUT", "CRUDE_FUT"):
        ks.trip(KSLevel.INSTRUMENT, key, KSAction.BLOCK_ENTRIES, "x", strategy_id="S1_ADAPTIVE_TREND")
    assert (KSLevel.STRATEGY, "S1_ADAPTIVE_TREND") in ks.trips


def test_strategy_hard_and_soft_stops(ks):
    ks.evaluate_strategy("S1_ADAPTIVE_TREND", pnl=-160_000, peak_pnl=0, nav=5_000_000, stats=None)
    assert ks.risk_multiplier("S1_ADAPTIVE_TREND") == 0.5           # soft stop (3%) halves budget
    ks.evaluate_strategy("S1_ADAPTIVE_TREND", pnl=-260_000, peak_pnl=0, nav=5_000_000, stats=None)
    assert ks.trips[(KSLevel.STRATEGY, "S1_ADAPTIVE_TREND")].action is KSAction.FLATTEN


# -- order manager ----------------------------------------------------------------------------
@pytest.fixture()
def oms_env(registry, costs, clock):
    broker = PaperBroker(registry, costs, clock)
    store = StateStore()
    oms = OrderManager(broker, store, AuditLog(), clock, {"S1": "ALGO-S1"}, RateLimiter(2, virtual=True))
    return oms, broker, store


def _order(oms, **kw):
    base = dict(strategy_id="S1", symbol="NIFTY_FUT", instrument="NIFTY_FUT", side=OrderSide.BUY, lots=1,
                lot_size=65, purpose=Purpose.ENTRY)
    base.update(kw)
    return oms.new_order(**base)


def test_order_lifecycle_tag_and_persistence(oms_env, clock):
    oms, broker, store = oms_env
    o = oms.submit(_order(oms))
    assert o.status is OrderStatus.ACKNOWLEDGED and o.algo_tag == "ALGO-S1"
    broker.process_open("NIFTY_FUT", Bar(clock(), 100, 101, 99, 100))
    [(order, fill)] = oms.poll()
    assert order.status is OrderStatus.FILLED and fill.qty == 65
    saved = store.load_orders()
    assert saved[0]["status"] == "FILLED" and [h[1] for h in saved[0]["history"]][:2] == ["SENT", "ACKNOWLEDGED"]


def test_illegal_transition_raises(oms_env):
    oms, *_ = oms_env
    o = oms.submit(_order(oms))
    with pytest.raises(RuntimeError):
        oms._transition(o, OrderStatus.CREATED)


def test_retry_after_timeout_reads_order_book_first(oms_env):
    oms, broker, _ = oms_env
    real = broker.place_order
    calls = {"n": 0}

    def flaky(order):
        calls["n"] += 1
        real(order)                  # the order reaches the broker...
        raise BrokerTimeout("ack lost")   # ...but the acknowledgement is lost
    broker.place_order = flaky
    o = oms.submit(_order(oms))
    assert calls["n"] == 1 and o.status is OrderStatus.ACKNOWLEDGED
    assert len(broker.orders) == 1    # no duplicate order (FR-6.6)


def test_multileg_group_unwinds_on_rejection(oms_env, clock):
    oms, broker, _ = oms_env
    exp = clock().date() + timedelta(days=30)
    c1 = OptionContract("NIFTY_OPT", exp, 26300, "CE")
    c2 = OptionContract("NIFTY_OPT", exp, 26000, "CE")
    buy = _order(oms, symbol=c1.symbol, instrument="NIFTY_OPT", contract=c1)
    sell = _order(oms, symbol=c2.symbol, instrument="NIFTY_OPT", contract=c2, side=OrderSide.SELL)
    oms.submit_group([sell, buy])
    assert buy.status is OrderStatus.ACKNOWLEDGED          # long wing goes first
    broker.on_quote(c1.symbol, 20, 19, 21)
    broker.reject_next = "insufficient margin"
    oms2 = _order(oms, symbol=c2.symbol, instrument="NIFTY_OPT", contract=c2, side=OrderSide.SELL)
    oms2.group_id = buy.group_id
    oms.submit(oms2)
    oms.poll()
    assert any("broken" in a for a in oms.alerts)
    reversal = [o for o in oms.orders.values() if o.purpose is Purpose.REVERSAL]
    assert reversal and reversal[0].side is OrderSide.SELL


def test_rejection_classification():
    assert classify_rejection("Insufficient margin available") == "margin"
    assert classify_rejection("Price outside circuit band") == "price_band"
    assert classify_rejection("Security is in ban period") == "ban"
    assert classify_rejection("Market closed") == "session"


def test_paper_stop_gap_fills_at_open(oms_env, clock):
    oms, broker, _ = oms_env
    oms.submit(_order(oms, side=OrderSide.SELL, purpose=Purpose.STOP, order_type=OrderType.SL_M,
                          trigger_price=95.0))
    broker.process_open("NIFTY_FUT", Bar(clock(), 90, 92, 88, 91))      # gap through the stop
    [(order, fill)] = oms.poll()
    assert fill.price < 95.0 and fill.price == pytest.approx(90 - 0.2, abs=0.11)


def test_paper_limit_expires(oms_env, clock):
    oms, broker, _ = oms_env
    o = oms.submit(_order(oms, order_type=OrderType.LIMIT, limit_price=95.0))
    broker.process_open("NIFTY_FUT", Bar(clock(), 100, 101, 99, 100))
    broker.process_intraday("NIFTY_FUT", Bar(clock(), 100, 101, 99, 100))
    oms.poll()
    assert o.status is OrderStatus.EXPIRED


# -- audit ------------------------------------------------------------------------------------
def test_audit_chain_detects_tampering_and_masks_secrets(tmp_path):
    path = tmp_path / "audit.jsonl"
    log = AuditLog(path)
    log.record("login", "op", {"access_token": "SECRET", "user": "x"})
    log.record("decision", "gw", {"verdict": "ALLOW"})
    assert log.verify() == (True, None)
    assert "SECRET" not in path.read_text()
    lines = path.read_text().splitlines()
    rec = json.loads(lines[1])
    rec["payload"]["verdict"] = "BLOCK"
    lines[1] = json.dumps(rec, sort_keys=True)
    path.write_text("\n".join(lines) + "\n")
    assert AuditLog(path).verify() == (False, 1)
