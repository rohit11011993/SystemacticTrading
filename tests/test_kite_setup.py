"""Kite setup tests with a fake KiteConnect client and an in-memory credential store.

They prove the workflow logic (login, contract mapping, roll choice, symbol round trip, history
download, check). They cannot prove Kite's live API behaves the same: that needs a real account.
"""

from __future__ import annotations

import threading
import time
import urllib.request
from datetime import date, datetime, timedelta

import pandas as pd
import pytest

from algotrader.core.instruments import InstrumentRegistry
from algotrader.execution import kite_setup as ks

from .conftest import CONFIG

TODAY = date(2026, 10, 1)


class MemKeyring:
    def __init__(self):
        self.d = {}

    def get_password(self, service, name):
        return self.d.get((service, name))

    def set_password(self, service, name, value):
        self.d[(service, name)] = value

    def delete_password(self, service, name):
        self.d.pop((service, name), None)


def _fut(name, exp, lot, tick=0.05, token=1, exch="NFO"):
    return {"instrument_token": token, "tradingsymbol": f"{name}{exp:%y%b}FUT".upper(), "name": name,
            "expiry": exp, "strike": 0.0, "tick_size": tick, "lot_size": lot, "instrument_type": "FUT",
            "segment": f"{exch}-FUT", "exchange": exch}


class FakeKite:
    def __init__(self, api_key, access_token=None):
        self.api_key, self.access_token = api_key, access_token
        self.calls = []

    def login_url(self):
        return f"https://kite.zerodha.com/connect/login?v=3&api_key={self.api_key}"

    def generate_session(self, request_token, api_secret):
        if request_token != "goodtoken" or api_secret != "s3cret":
            raise RuntimeError("Token is invalid or has expired.")
        return {"access_token": "acc-123", "user_id": "AB1234", "user_name": "Test Trader"}

    def profile(self):
        return {"user_id": "AB1234", "user_name": "Test Trader", "exchanges": ["NSE", "NFO", "BSE"]}

    def margins(self):
        return {"equity": {"net": 5_000_000}}

    def instruments(self, exchange=None):
        if exchange == "NFO":
            rows = [_fut("NIFTY", date(2026, 10, 6), 65, 0.1, 11), _fut("NIFTY", date(2026, 11, 3), 65, 0.1, 12),
                    _fut("HDFCBANK", date(2026, 10, 27), 550, 0.05, 21)]
            for exp in (date(2026, 10, 27), date(2026, 11, 24)):
                for k in (25000, 25300):
                    for r in ("CE", "PE"):
                        rows.append({"instrument_token": hash((exp, k, r)) % 10**6,
                                     "tradingsymbol": f"NIFTY{exp:%y%b}{k}{r}".upper(), "name": "NIFTY",
                                     "expiry": exp, "strike": float(k), "tick_size": 0.05, "lot_size": 75,
                                     "instrument_type": r, "segment": "NFO-OPT", "exchange": "NFO"})
            return rows
        if exchange == "NSE":
            return [{"instrument_token": 264969, "tradingsymbol": "INDIA VIX", "name": "INDIA VIX",
                     "segment": "INDICES", "instrument_type": "EQ", "expiry": "", "lot_size": 0, "tick_size": 0.0025}]
        if exchange == "MCX":
            return [_fut("GOLDM", date(2026, 11, 5), 10, 1.0, 31, "MCX")]
        return []

    def historical_data(self, token, a, b, interval, continuous=False, oi=False):
        self.calls.append((token, a, b, continuous))
        days = pd.bdate_range(a, b)
        return [{"date": datetime(d.year, d.month, d.day), "open": 100.0, "high": 101.0, "low": 99.0,
                 "close": 100.5, "volume": 10} for d in days]


@pytest.fixture()
def store():
    return ks.SecretStore(MemKeyring())


@pytest.fixture()
def reg():
    return InstrumentRegistry.from_yaml(CONFIG / "instruments.yaml")


def test_credentials_and_login(store):
    with pytest.raises(ks.KiteSetupError, match="API key"):
        ks.login_url(store, FakeKite)
    store.save_app("key123", "s3cret")
    assert "api_key=key123" in ks.login_url(store, FakeKite)
    with pytest.raises(ks.KiteSetupError, match="no Kite session"):
        ks.connect(store, FakeKite)
    with pytest.raises(ks.KiteSetupError, match="rejected"):
        ks.complete_login(store, "badtoken", FakeKite)
    info = ks.complete_login(store, "goodtoken", FakeKite)
    assert info == {"user_id": "AB1***", "user_name": "Test Trader"}        # identifiers masked
    assert ks.connect(store, FakeKite).access_token == "acc-123"
    assert store.token(date.today() - timedelta(days=1)) is None              # sessions are daily
    store.revoke()
    assert store.status() == {"app_configured": False, "session_today": False}


@pytest.mark.parametrize("text", [
    "http://127.0.0.1:5010/kite?action=login&type=login&status=success&request_token=goodtoken",
    "goodtoken"])
def test_parse_request_token(text):
    assert ks.parse_request_token(text) == "goodtoken"


def test_parse_request_token_rejects_failure():
    with pytest.raises(ks.KiteSetupError):
        ks.parse_request_token("http://127.0.0.1:5010/kite?status=error")
    with pytest.raises(ks.KiteSetupError):
        ks.parse_request_token("not a token!")


def test_capture_request_token_from_local_redirect():
    url = "http://127.0.0.1:5017/kite"
    out = {}
    t = threading.Thread(target=lambda: out.update(tok=ks.capture_request_token(url, timeout=10)))
    t.start()
    for _ in range(50):
        try:
            urllib.request.urlopen(url + "?status=success&request_token=abc123XYZ", timeout=2).read()
            break
        except OSError:
            time.sleep(0.1)
    t.join(10)
    assert out["tok"] == "abc123XYZ"
    assert ks.capture_request_token("https://example.com/cb", timeout=0.1) is None   # never non-local


def test_resolve_contracts_rolls_and_maps(reg):
    table, notes = ks.resolve_contracts(FakeKite("k"), reg, reg.keys(), TODAY)
    # Oct-06 expiry is 3 trading days away (Oct 1, 2, 5) = the roll window -> November is used.
    assert table["futures"]["NIFTY_FUT"]["tradingsymbol"] == "NIFTY26NOVFUT"
    assert table["futures"]["GOLD_FUT"]["exchange"] == "MCX"
    assert table["indices"]["INDIAVIX"]["tradingsymbol"] == "INDIA VIX"
    assert table["options"]["NIFTY_OPT"]["expiries"] == ["2026-10-27", "2026-11-24"]
    assert any("NIFTY_OPT: exchange lot 75" in n for n in notes)           # spec change reported
    assert any("CRUDE_FUT" in n for n in notes)                            # not on this account: reported

    to_broker, reverse = ks.symbol_mapper(table)
    assert to_broker("NIFTY_FUT") == ("NFO", "NIFTY26NOVFUT")
    opt = "NIFTY_OPT:20261027:25300:CE"
    exch, tsym = to_broker(opt)
    assert (exch, tsym) == ("NFO", "NIFTY26OCT25300CE") and reverse[tsym] == opt
    assert reverse["NIFTY26NOVFUT"] == "NIFTY_FUT"
    with pytest.raises(ks.KiteSetupError, match="kite-instruments"):
        to_broker("TCS_FUT")

    recs = ks.master_refresh_records(table)
    changes = reg.apply_master_refresh(recs, TODAY)
    assert reg.get("NIFTY_OPT").lot_size(TODAY) == 75 and any("NIFTY_OPT" in c for c in changes)


def test_fetch_history_writes_csv_and_backs_up_demo(reg, tmp_path):
    kc = FakeKite("k", "t")
    table, _ = ks.resolve_contracts(kc, reg, ["NIFTY_FUT", "INDIAVIX"], TODAY)
    (tmp_path / "NIFTY_FUT.csv").write_text("demo")
    paths = ks.fetch_history(kc, table, reg, tmp_path, date(2020, 1, 1), date(2026, 9, 30),
                             pause=0, log=lambda m: None)
    assert {p.name for p in paths} == {"NIFTY_FUT.csv", "INDIAVIX.csv"}
    df = pd.read_csv(tmp_path / "NIFTY_FUT.csv", parse_dates=["date"])
    assert df["date"].is_monotonic_increasing and df["date"].is_unique and len(df) > 1500
    assert (tmp_path / "_demo_backup" / "NIFTY_FUT.csv").read_text() == "demo"
    fut_calls = [c for c in kc.calls if c[0] == table["futures"]["NIFTY_FUT"]["token"]]
    assert all(c[3] for c in fut_calls) and len(fut_calls) >= 2      # continuous + chunked requests
    from algotrader.data.provider import FileDataProvider
    assert len(FileDataProvider(tmp_path).history("NIFTY_FUT")) == len(df)   # readable by the engine


def test_check_connection_flags_missing_segments(reg):
    res = ks.check_connection(FakeKite("k", "t"), reg, ["NIFTY_FUT", "GOLD_FUT"], (True, "match"))
    assert not res.ok
    assert any("MCX NOT enabled" in line for line in res.lines)
    assert any("Test Trader" in line for line in res.lines)
    ok = ks.check_connection(FakeKite("k", "t"), reg, ["NIFTY_FUT"], (True, "match"))
    assert ok.ok
