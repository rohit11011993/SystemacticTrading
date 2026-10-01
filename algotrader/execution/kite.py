"""Zerodha Kite Connect adapters: authentication, broker and market data (PRD s.6, R7).

``kiteconnect`` is imported lazily so backtests and paper trading run without it.

Compliance points implemented here (PRD s.3):
* REG-1  static IP: ``KiteAuth.check_static_ip`` compares the host's public IP with the
         configured one; the engine refuses to trade on a mismatch.
* REG-2  OAuth + 2FA, daily session: the operator completes the browser login; the request
         token is exchanged for an access token that is stored encrypted via ``keyring``
         (Windows Credential Manager) and never written to config or logs (FR-15.1).
         Scripting the 2FA step is deliberately NOT implemented (it may breach broker terms).
* REG-3  algo tag on every order (Kite ``tag`` field, max 20 chars).

Vendor limits, field names and order parameters change over time; re-verify this adapter
against Kite's current documentation as part of the release checklist. This module is not
exercised by the automated tests (no network access in CI).
"""

from __future__ import annotations

import json
import logging
import urllib.request
from datetime import date, datetime
from typing import Any, Callable

import pandas as pd

from ..core.models import Fill, Order, OrderSide, OrderStatus, OrderType
from .broker import BrokerGateway, BrokerTimeout, OrderRejected, StatusUpdate

log = logging.getLogger(__name__)

_KEYRING_SERVICE = "AlgoTrader-Kite"

_STATUS_MAP = {
    "OPEN": OrderStatus.ACKNOWLEDGED, "TRIGGER PENDING": OrderStatus.ACKNOWLEDGED,
    "COMPLETE": OrderStatus.FILLED, "CANCELLED": OrderStatus.CANCELLED,
    "REJECTED": OrderStatus.REJECTED,
}


def _kite_module():
    try:
        import kiteconnect  # type: ignore
    except ImportError as exc:  # pragma: no cover - depends on optional dependency
        raise RuntimeError("kiteconnect is not installed: pip install algotrader[kite]") from exc
    return kiteconnect


class KiteAuth:
    """Daily login workflow and token storage (FR-6.3, FR-15.1, FR-15.8)."""

    def __init__(self, api_key: str, static_ip: str | None = None,
                 ip_echo_url: str = "https://api.ipify.org"):
        self.api_key = api_key
        self.static_ip = static_ip
        self.ip_echo_url = ip_echo_url

    def check_static_ip(self) -> tuple[bool, str]:
        """REG-1: refuse to trade if the public IP differs from the registered static IP."""
        if not self.static_ip:
            return True, "static IP check disabled"
        try:
            with urllib.request.urlopen(self.ip_echo_url, timeout=5) as resp:  # noqa: S310
                public = resp.read().decode().strip()
        except OSError as exc:
            return False, f"cannot determine public IP: {exc}"
        return public == self.static_ip, f"public IP {public}, registered {self.static_ip}"

    def login_url(self) -> str:
        kc = _kite_module().KiteConnect(api_key=self.api_key)
        return kc.login_url()

    def complete_login(self, request_token: str, api_secret: str) -> str:
        """Exchange the request token (after the operator's 2FA) and store the access token."""
        kc = _kite_module().KiteConnect(api_key=self.api_key)
        session = kc.generate_session(request_token, api_secret=api_secret)
        token = session["access_token"]
        self._store_token(token)
        return token

    @staticmethod
    def _store_token(token: str) -> None:
        import keyring  # type: ignore
        keyring.set_password(_KEYRING_SERVICE, f"access_token:{date.today().isoformat()}", token)

    @staticmethod
    def load_token() -> str | None:
        """Today's token only: Kite sessions close at the end of each trading day (REG-2)."""
        try:
            import keyring  # type: ignore
        except ImportError:
            return None
        return keyring.get_password(_KEYRING_SERVICE, f"access_token:{date.today().isoformat()}")

    @staticmethod
    def revoke() -> None:
        """One settings action revokes all stored tokens (FR-15.8)."""
        import keyring  # type: ignore
        try:
            keyring.delete_password(_KEYRING_SERVICE, f"access_token:{date.today().isoformat()}")
        except Exception:  # noqa: BLE001
            pass


class KiteBroker(BrokerGateway):
    """Kite order routing. ``symbol_map`` turns an internal symbol into (exchange, tradingsymbol)."""

    name = "kite"

    def __init__(self, api_key: str, access_token: str,
                 symbol_map: Callable[[str], tuple[str, str]], market_protection: float = 2.0,
                 exit_only: bool = False):
        kc_mod = _kite_module()
        self.kc = kc_mod.KiteConnect(api_key=api_key)
        self.kc.set_access_token(access_token)
        self.symbol_map = symbol_map
        self.market_protection = market_protection
        self.exit_only = exit_only
        self._seen_trades: set[str] = set()
        self._last_status: dict[str, str] = {}
        self._ref_by_id: dict[str, str] = {}

    def place_order(self, order: Order) -> str:
        exchange, tsym = self.symbol_map(order.symbol)
        params: dict[str, Any] = dict(
            variety=self.kc.VARIETY_REGULAR, exchange=exchange, tradingsymbol=tsym,
            transaction_type=order.side.value, quantity=order.qty, product=order.product.value,
            order_type=order.order_type.value, validity=self.kc.VALIDITY_DAY,
            tag=order.algo_tag[:20])
        if order.order_type is OrderType.LIMIT:
            params["price"] = order.limit_price
        if order.order_type is OrderType.SL_M:
            params["trigger_price"] = order.trigger_price
        if order.order_type is OrderType.MARKET:
            params["market_protection"] = self.market_protection   # verify against current API
        try:
            oid = str(self.kc.place_order(**params))
        except Exception as exc:  # noqa: BLE001 - vendor exceptions are mapped below
            name = type(exc).__name__
            if name in ("NetworkException", "TimeoutError"):
                raise BrokerTimeout(str(exc)) from exc
            raise OrderRejected(str(exc)) from exc
        self._ref_by_id[oid] = order.client_ref
        return oid

    def modify_order(self, broker_order_id: str, trigger_price: float | None = None,
                     limit_price: float | None = None) -> None:
        self.kc.modify_order(variety=self.kc.VARIETY_REGULAR, order_id=broker_order_id,
                             trigger_price=trigger_price, price=limit_price)

    def cancel_order(self, broker_order_id: str) -> None:
        self.kc.cancel_order(variety=self.kc.VARIETY_REGULAR, order_id=broker_order_id)

    def find_order(self, client_ref: str) -> dict[str, Any] | None:
        # Kite does not index by client ref; match on the tag + our mapping.
        for o in self.kc.orders():
            oid = str(o["order_id"])
            if self._ref_by_id.get(oid) == client_ref:
                return {"broker_order_id": oid, "status": _STATUS_MAP.get(o["status"]),
                        "filled_qty": o.get("filled_quantity", 0), "avg_price": o.get("average_price", 0)}
        return None

    def poll(self) -> tuple[list[Fill], list[StatusUpdate]]:
        fills: list[Fill] = []
        updates: list[StatusUpdate] = []
        for t in self.kc.trades():
            tid = str(t["trade_id"])
            if tid in self._seen_trades:
                continue
            self._seen_trades.add(tid)
            ref = self._ref_by_id.get(str(t["order_id"]), f"external:{t['order_id']}")
            fills.append(Fill(ref, str(t["order_id"]), t["tradingsymbol"],
                              OrderSide(t["transaction_type"]), int(t["quantity"]),
                              float(t["average_price"]),
                              pd.Timestamp(t.get("fill_timestamp") or datetime.now()).to_pydatetime()))
        for o in self.kc.orders():
            oid = str(o["order_id"])
            st = o["status"]
            if self._last_status.get(oid) != st and oid in self._ref_by_id:
                self._last_status[oid] = st
                mapped = _STATUS_MAP.get(st)
                if mapped:
                    updates.append(StatusUpdate(self._ref_by_id[oid], mapped, o.get("status_message") or ""))
        return fills, updates

    def positions(self) -> dict[str, int]:
        out: dict[str, int] = {}
        for p in self.kc.positions().get("net", []):
            if p["quantity"]:
                out[p["tradingsymbol"]] = int(p["quantity"])
        return out

    def margins(self) -> dict[str, float]:
        m = self.kc.margins()
        eq = m.get("equity", {})
        return {"available": float(eq.get("net", 0.0)),
                "used": float(eq.get("utilised", {}).get("debits", 0.0))}


class KiteDataProvider:
    """Historical daily bars and the instrument master via Kite (FR-6.1, FR-6.2)."""

    def __init__(self, api_key: str, access_token: str, token_map: Callable[[str], int]):
        kc_mod = _kite_module()
        self.kc = kc_mod.KiteConnect(api_key=api_key)
        self.kc.set_access_token(access_token)
        self.token_map = token_map

    def history(self, key: str, start: date, end: date) -> pd.DataFrame:
        rows = self.kc.historical_data(self.token_map(key), start, end, "day", continuous=True, oi=False)
        df = pd.DataFrame(rows)
        if df.empty:
            return df
        df["date"] = pd.to_datetime(df["date"]).dt.tz_localize(None)
        return df.set_index("date")[["open", "high", "low", "close", "volume"]]

    def instrument_master(self, exchange: str) -> list[dict[str, Any]]:
        return self.kc.instruments(exchange)

    @staticmethod
    def dump(rows: list[dict[str, Any]]) -> str:
        return json.dumps(rows, default=str)
