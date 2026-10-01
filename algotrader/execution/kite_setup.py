"""Zerodha Kite Connect setup: credentials, daily login, connection check, contract mapping and
historical data download (PRD s.6, REG-1/REG-2, FR-6.2, FR-6.3, FR-15.1).

Operator workflow
-----------------
1. Once:   create a Kite Connect app at https://developers.kite.trade and note its API key,
           API secret and redirect URL. ``algotrader kite-setup`` stores key and secret in the
           OS credential store (Windows Credential Manager) - never in files or logs.
2. Daily:  ``algotrader kite-login`` opens the Kite login page; you sign in with 2FA yourself
           (scripting 2FA may breach broker terms, so it is never automated). The request token
           is captured from the redirect (or pasted) and exchanged for today's access token.
3. Daily:  ``algotrader kite-instruments`` maps every registry instrument to the current Kite
           contract (tradingsymbol, token, lot size, tick, expiry) and reports spec changes.
4. As needed: ``algotrader kite-fetch-data`` downloads daily history (continuous futures)
           into the data folder, replacing demo data; backtests and paper trading then run on
           real prices.

Everything here takes the KiteConnect client as a parameter, so it is unit-tested with a fake.
"""

from __future__ import annotations

import http.server
import shutil
import time as _time
import urllib.parse
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Any, Callable

import numpy as np
import pandas as pd

from ..core.instruments import InstrumentRegistry
from ..core.models import InstrumentType

SERVICE = "AlgoTrader-Kite"
CONTRACTS_KEY = "kite_contracts"      # state-database key for the resolved contract table

# Kite instrument-master segments per registry type.
_SEGMENTS = {
    InstrumentType.INDEX_FUTURE: "FUT", InstrumentType.STOCK_FUTURE: "FUT",
    InstrumentType.COMMODITY_FUTURE: "FUT", InstrumentType.CURRENCY_FUTURE: "FUT",
}


class KiteSetupError(Exception):
    """A setup step cannot complete; the message tells the operator what to do."""


# --------------------------------------------------------------------------------------------
# Credentials (FR-15.1): OS credential store only
# --------------------------------------------------------------------------------------------
class SecretStore:
    """API key, API secret and today's access token in the OS credential store.

    ``backend`` defaults to the ``keyring`` module (Windows Credential Manager on Windows); tests
    pass an in-memory object with the same get/set/delete_password functions.
    """

    def __init__(self, backend: Any | None = None):
        if backend is None:
            try:
                import keyring as backend  # type: ignore[no-redef]
            except ImportError as exc:  # pragma: no cover - depends on installation
                raise KiteSetupError("the 'keyring' package is missing: pip install keyring") from exc
        self.kr = backend

    def _get(self, name: str) -> str | None:
        try:
            return self.kr.get_password(SERVICE, name)
        except Exception as exc:  # noqa: BLE001 - keyring raises backend-specific errors
            raise KiteSetupError(f"cannot read the OS credential store ({exc.__class__.__name__}). On Windows "
                                 "this is the Credential Manager; on Linux install a keyring backend.") from exc

    def _set(self, name: str, value: str) -> None:
        try:
            self.kr.set_password(SERVICE, name, value)
        except Exception as exc:  # noqa: BLE001
            raise KiteSetupError(f"cannot write to the OS credential store ({exc.__class__.__name__})") from exc

    def _delete(self, name: str) -> None:
        try:
            self.kr.delete_password(SERVICE, name)
        except Exception:  # noqa: BLE001 - absent entries are fine
            pass

    # api credentials
    def save_app(self, api_key: str, api_secret: str) -> None:
        if not api_key.strip() or not api_secret.strip():
            raise KiteSetupError("both the API key and the API secret are required")
        self._set("api_key", api_key.strip())
        self._set("api_secret", api_secret.strip())

    @property
    def api_key(self) -> str | None:
        return self._get("api_key")

    @property
    def api_secret(self) -> str | None:
        return self._get("api_secret")

    # daily session (REG-2: sessions close at the end of each trading day)
    def save_token(self, token: str, day: date | None = None) -> None:
        self._set(f"access_token:{(day or date.today()).isoformat()}", token)

    def token(self, day: date | None = None) -> str | None:
        return self._get(f"access_token:{(day or date.today()).isoformat()}")

    def revoke(self, day: date | None = None) -> None:
        """One action removes today's token and the app credentials (FR-15.8)."""
        self._delete(f"access_token:{(day or date.today()).isoformat()}")
        self._delete("api_key")
        self._delete("api_secret")

    def status(self) -> dict[str, bool]:
        return {"app_configured": bool(self.api_key and self.api_secret), "session_today": bool(self.token())}


def mask(value: str | None, keep: int = 3) -> str:
    """Show only the first characters of an identifier (FR-15.5)."""
    if not value:
        return "-"
    return value[:keep] + "*" * max(0, len(value) - keep)


# --------------------------------------------------------------------------------------------
# Daily login
# --------------------------------------------------------------------------------------------
def default_client_factory(api_key: str, access_token: str | None = None):
    try:
        from kiteconnect import KiteConnect  # type: ignore
    except ImportError as exc:  # pragma: no cover - depends on installation
        raise KiteSetupError("the 'kiteconnect' package is missing: pip install kiteconnect") from exc
    kc = KiteConnect(api_key=api_key)
    if access_token:
        kc.set_access_token(access_token)
    return kc


def parse_request_token(text: str) -> str:
    """Accept either the full redirect URL from the browser or the bare request token."""
    text = text.strip()
    if "request_token=" in text:
        query = urllib.parse.urlparse(text).query or text.split("?", 1)[-1]
        params = urllib.parse.parse_qs(query)
        if params.get("status", ["success"])[0] != "success":
            raise KiteSetupError(f"Kite login did not succeed (status={params.get('status')})")
        token = params.get("request_token", [""])[0]
    else:
        token = text
    if not token or not token.isalnum():
        raise KiteSetupError("could not find a request token: paste the whole address from the browser "
                             "after logging in (it contains 'request_token=')")
    return token


def capture_request_token(redirect_url: str, timeout: float = 180.0) -> str | None:
    """Listen once on a localhost redirect URL (e.g. http://127.0.0.1:5010/kite) for the token.

    Only loopback addresses are served. Returns None on timeout so the caller can fall back to
    asking the operator to paste the redirected URL.
    """
    u = urllib.parse.urlparse(redirect_url)
    if u.hostname not in ("127.0.0.1", "localhost") or not u.port:
        return None
    found: dict[str, str] = {}

    class Handler(http.server.BaseHTTPRequestHandler):
        def do_GET(self):  # noqa: N802 - http.server naming
            try:
                found["token"] = parse_request_token(self.path)
                body = b"<h3>AlgoTrader: Kite login received. You can close this tab.</h3>"
                self.send_response(200)
            except KiteSetupError as exc:
                body = f"<h3>AlgoTrader: {exc}</h3>".encode()
                self.send_response(400)
            self.send_header("Content-Type", "text/html")
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, *args):  # silence the default stderr access log
            pass

    server = http.server.HTTPServer(("127.0.0.1", u.port), Handler)
    server.timeout = 1.0
    deadline = _time.time() + timeout
    try:
        while "token" not in found and _time.time() < deadline:
            server.handle_request()
    finally:
        server.server_close()
    return found.get("token")


def login_url(store: SecretStore, factory: Callable = default_client_factory) -> str:
    if not store.api_key:
        raise KiteSetupError("no API key stored yet: run kite-setup (or Settings -> Set API key) first")
    return factory(store.api_key).login_url()


def complete_login(store: SecretStore, request_token: str, factory: Callable = default_client_factory
                   ) -> dict[str, Any]:
    """Exchange the request token for today's access token and store it."""
    if not (store.api_key and store.api_secret):
        raise KiteSetupError("API key and secret are not stored yet: run kite-setup first")
    kc = factory(store.api_key)
    try:
        session = kc.generate_session(request_token, api_secret=store.api_secret)
    except Exception as exc:  # noqa: BLE001 - vendor exceptions carry the useful message
        raise KiteSetupError(f"Kite rejected the login: {exc}. Request tokens are single-use and expire "
                             "within minutes; log in again.") from exc
    store.save_token(session["access_token"])
    return {"user_id": mask(session.get("user_id")), "user_name": session.get("user_name", "")}


def connect(store: SecretStore, factory: Callable = default_client_factory):
    """A client with today's session, or a clear error saying what to do."""
    if not store.api_key:
        raise KiteSetupError("Kite is not configured: run kite-setup (or Settings -> Set API key)")
    token = store.token()
    if not token:
        raise KiteSetupError("no Kite session for today: log in again (sessions end every trading day)")
    return factory(store.api_key, token)


# --------------------------------------------------------------------------------------------
# Connection check
# --------------------------------------------------------------------------------------------
@dataclass
class CheckResult:
    ok: bool
    lines: list[str]


def check_connection(kc, registry: InstrumentRegistry, needed_keys: list[str],
                     static_ip: tuple[bool, str] | None = None) -> CheckResult:
    lines: list[str] = []
    ok = True
    if static_ip is not None:
        ip_ok, msg = static_ip
        lines.append(("OK    " if ip_ok else "FAIL  ") + f"static IP: {msg}")
        ok &= ip_ok
    try:
        prof = kc.profile()
    except Exception as exc:  # noqa: BLE001
        return CheckResult(False, lines + [f"FAIL  session: {exc} - log in again"])
    lines.append(f"OK    logged in as {prof.get('user_name', '?')} ({mask(prof.get('user_id'))})")
    enabled = set(prof.get("exchanges") or [])
    needed = {registry.get(k).exchange for k in needed_keys if registry.get(k).tradable}
    for ex in sorted(needed):
        if ex in enabled:
            lines.append(f"OK    segment {ex} enabled")
        else:
            lines.append(f"FAIL  segment {ex} NOT enabled on this account - enable it with the broker "
                         "or remove its instruments from bindings.yaml")
            ok = False
    try:
        eq = kc.margins().get("equity", {})
        lines.append(f"OK    equity margin available: {eq.get('net', 0):,.0f}")
    except Exception as exc:  # noqa: BLE001
        lines.append(f"WARN  margins unavailable: {exc}")
    return CheckResult(ok, lines)


# --------------------------------------------------------------------------------------------
# Contract mapping (FR-6.2 instrument-master refresh)
# --------------------------------------------------------------------------------------------
def _as_date(v: Any) -> date | None:
    if v in (None, ""):
        return None
    if isinstance(v, datetime):
        return v.date()
    if isinstance(v, date):
        return v
    return date.fromisoformat(str(v)[:10])


def resolve_contracts(kc, registry: InstrumentRegistry, keys: list[str], today: date | None = None,
                      option_expiries: int = 3) -> tuple[dict[str, Any], list[str]]:
    """Map registry instruments to current Kite contracts.

    Futures: the nearest expiry that is at least ``roll_days_before_expiry`` days away (so we
    trade the more liquid next month around expiry, strategy doc s.4). Indices: by Kite name or
    ``data_symbol``. Options: the next ``option_expiries`` expiries, indexed by strike and right.
    Returns (contract table, list of human-readable warnings / spec changes).
    """
    today = today or date.today()
    masters: dict[str, list[dict[str, Any]]] = {}

    def master(exchange: str) -> list[dict[str, Any]]:
        if exchange not in masters:
            masters[exchange] = kc.instruments(exchange)
        return masters[exchange]

    table: dict[str, Any] = {"updated": today.isoformat(), "futures": {}, "indices": {}, "options": {}}
    notes: list[str] = []
    for key in keys:
        inst = registry.get(key)
        name = inst.broker_symbol or inst.symbol      # Kite's 'name' / index tradingsymbol
        if inst.type in _SEGMENTS:
            rows = [r for r in master(inst.exchange) if r.get("name") == name and r.get("instrument_type") == "FUT"]
            # Roll a fixed number of TRADING days before expiry (strategy doc s.4).
            roll = inst.flags.roll_days_before_expiry
            rows = sorted((r for r in rows if _as_date(r.get("expiry"))
                           and np.busday_count(today, _as_date(r["expiry"])) > roll),
                          key=lambda r: _as_date(r["expiry"]))
            if not rows:
                notes.append(f"{key}: no {inst.exchange} future named '{name}' found - check 'symbol' in instruments.yaml")
                continue
            r = rows[0]
            table["futures"][key] = {"exchange": inst.exchange, "tradingsymbol": r["tradingsymbol"],
                                     "token": int(r["instrument_token"]), "lot_size": int(r["lot_size"]),
                                     "tick_size": float(r["tick_size"]), "expiry": _as_date(r["expiry"]).isoformat()}
            spec = inst.spec(today)
            if int(r["lot_size"]) != spec.lot_size or abs(float(r["tick_size"]) - spec.tick_size) > 1e-9:
                notes.append(f"{key}: exchange lot {r['lot_size']} / tick {r['tick_size']} differ from config "
                             f"lot {spec.lot_size} / tick {spec.tick_size} - the exchange values are used")
        elif inst.type is InstrumentType.INDEX:
            want = name.upper()
            rows = [r for r in master(inst.exchange) if r.get("segment") == "INDICES"
                    and want in (str(r.get("tradingsymbol", "")).upper(), str(r.get("name", "")).upper())]
            if not rows:
                notes.append(f"{key}: index '{name}' not found on {inst.exchange}")
                continue
            r = rows[0]
            table["indices"][key] = {"exchange": inst.exchange, "tradingsymbol": r["tradingsymbol"],
                                     "token": int(r["instrument_token"])}
        elif inst.type is InstrumentType.INDEX_OPTION:
            rows = [r for r in master(inst.exchange) if r.get("name") == name and r.get("instrument_type") in ("CE", "PE")]
            expiries = sorted({_as_date(r["expiry"]) for r in rows if _as_date(r.get("expiry")) and
                               _as_date(r["expiry"]) >= today})[:option_expiries]
            chain = {f"{_as_date(r['expiry'])}|{float(r['strike']):g}|{r['instrument_type']}":
                     {"tradingsymbol": r["tradingsymbol"], "token": int(r["instrument_token"])}
                     for r in rows if _as_date(r.get("expiry")) in expiries}
            if not chain:
                notes.append(f"{key}: no {name} options found on {inst.exchange}")
                continue
            lot = int(next(r["lot_size"] for r in rows if _as_date(r.get("expiry")) in expiries))
            table["options"][key] = {"exchange": inst.exchange, "lot_size": lot,
                                     "expiries": [d.isoformat() for d in expiries], "chain": chain}
            if lot != inst.spec(today).lot_size:
                notes.append(f"{key}: exchange lot {lot} differs from config lot {inst.spec(today).lot_size}")
    return table, notes


def master_refresh_records(table: dict[str, Any]) -> dict[str, dict[str, Any]]:
    """Records for ``InstrumentRegistry.apply_master_refresh`` from a contract table."""
    out: dict[str, dict[str, Any]] = {}
    for key, c in table.get("futures", {}).items():
        out[key] = {"lot_size": c["lot_size"], "tick_size": c["tick_size"], "broker_token": str(c["token"])}
    for key, c in table.get("options", {}).items():
        out[key] = {"lot_size": c["lot_size"], "expiries": [date.fromisoformat(d) for d in c["expiries"]]}
    return out


def symbol_mapper(table: dict[str, Any]) -> tuple[Callable[[str], tuple[str, str]], dict[str, str]]:
    """(internal symbol -> (exchange, tradingsymbol), reverse map tradingsymbol -> internal symbol)."""
    reverse: dict[str, str] = {}
    for key, c in {**table.get("futures", {}), **table.get("indices", {})}.items():
        reverse[c["tradingsymbol"]] = key
    for key, o in table.get("options", {}).items():
        for k, c in o["chain"].items():
            exp, strike, right = k.split("|")
            reverse[c["tradingsymbol"]] = f"{key}:{exp.replace('-', '')}:{strike}:{right}"

    def to_broker(symbol: str) -> tuple[str, str]:
        if symbol in table.get("futures", {}):
            c = table["futures"][symbol]
            return c["exchange"], c["tradingsymbol"]
        if ":" in symbol:                                   # option contract symbol
            key, exp, strike, right = symbol.split(":")
            o = table.get("options", {}).get(key)
            if o:
                iso = f"{exp[:4]}-{exp[4:6]}-{exp[6:]}"
                c = o["chain"].get(f"{iso}|{float(strike):g}|{right}")
                if c:
                    return o["exchange"], c["tradingsymbol"]
        raise KiteSetupError(f"no Kite contract mapped for {symbol}: run kite-instruments")

    return to_broker, reverse


# --------------------------------------------------------------------------------------------
# Historical data (FR-6.10)
# --------------------------------------------------------------------------------------------
def fetch_history(kc, table: dict[str, Any], registry: InstrumentRegistry, data_dir: str | Path,
                  start: date, end: date | None = None, chunk_days: int = 1800, pause: float = 0.4,
                  log: Callable[[str], None] = print) -> list[Path]:
    """Download daily bars into ``data_dir/<feed>.csv``.

    Futures use Kite's *continuous* series (roll-adjusted across expiries) so backtests see one
    unbroken history. Demo files already in the folder are moved to ``_demo_backup`` first.
    """
    end = end or date.today()
    data_dir = Path(data_dir)
    data_dir.mkdir(parents=True, exist_ok=True)
    backup = data_dir / "_demo_backup"
    written: list[Path] = []
    items = [(k, c, True) for k, c in table.get("futures", {}).items()] + \
            [(k, c, False) for k, c in table.get("indices", {}).items()]
    if not items:
        raise KiteSetupError("the contract table is empty: run kite-instruments first")
    for key, c, continuous in items:
        frames = []
        a = start
        while a <= end:
            b = min(end, a + timedelta(days=chunk_days))
            try:
                rows = kc.historical_data(c["token"], a, b, "day", continuous=continuous)
            except Exception as exc:  # noqa: BLE001
                if continuous:
                    log(f"{key}: continuous history unavailable ({exc}); using the current contract only")
                    rows = kc.historical_data(c["token"], a, b, "day", continuous=False)
                else:
                    raise
            if rows:
                frames.append(pd.DataFrame(rows))
            a = b + timedelta(days=1)
            _time.sleep(pause)                       # stay inside the historical API rate limit
        if not frames:
            log(f"{key}: no data returned")
            continue
        df = pd.concat(frames)
        df["date"] = pd.to_datetime(df["date"]).dt.tz_localize(None).dt.normalize()
        df = df.drop_duplicates("date").sort_values("date")[["date", "open", "high", "low", "close", "volume"]]
        feed = registry.get(key).feed
        path = data_dir / f"{feed}.csv"
        if path.exists() and not (backup / path.name).exists():
            backup.mkdir(exist_ok=True)               # keep the previous (demo) file once
            shutil.copy2(path, backup / path.name)
        df.to_csv(path, index=False)
        written.append(path)
        log(f"{key}: {len(df)} bars {df['date'].iloc[0].date()} -> {df['date'].iloc[-1].date()}")
    return written
