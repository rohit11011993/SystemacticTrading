"""State database (PRD s.11): SQLite in write-ahead-log mode.

Stores orders, fills, trades, kill-switch state, strategy state and heartbeats. Every state
change is persisted *before* the action it enables (FR-11.1), so a restart can rebuild state,
reload the broker's books, reconcile and only then allow new entries.

Objects are stored as JSON blobs keyed by id; this keeps the schema stable while the domain
model evolves (migrations stay versioned per FR-13.7).
"""

from __future__ import annotations

import json
import sqlite3
import threading
from dataclasses import asdict, is_dataclass
from datetime import date, datetime
from enum import Enum
from pathlib import Path
from typing import Any

SCHEMA_VERSION = 1

_DDL = """
CREATE TABLE IF NOT EXISTS meta (key TEXT PRIMARY KEY, value TEXT);
CREATE TABLE IF NOT EXISTS orders (client_ref TEXT PRIMARY KEY, status TEXT, strategy TEXT,
                                   updated TEXT, body TEXT);
CREATE TABLE IF NOT EXISTS fills (fill_id TEXT PRIMARY KEY, client_ref TEXT, ts TEXT, body TEXT);
CREATE TABLE IF NOT EXISTS trades (trade_id TEXT PRIMARY KEY, strategy TEXT, status TEXT,
                                   updated TEXT, body TEXT);
CREATE TABLE IF NOT EXISTS kv (key TEXT PRIMARY KEY, value TEXT, updated TEXT);
CREATE TABLE IF NOT EXISTS heartbeats (process TEXT PRIMARY KEY, ts REAL);
CREATE TABLE IF NOT EXISTS equity (day TEXT PRIMARY KEY, body TEXT);
"""


def _json_default(o: Any) -> Any:
    if isinstance(o, (datetime, date)):
        return o.isoformat()
    if isinstance(o, Enum):
        return o.value
    if is_dataclass(o) and not isinstance(o, type):
        return asdict(o)
    if isinstance(o, set):
        return sorted(o)
    if hasattr(o, "item"):           # numpy scalars
        return o.item()
    return str(o)


def dumps(obj: Any) -> str:
    return json.dumps(obj, default=_json_default, sort_keys=True)


class StateStore:
    """Thin persistence layer. ``path=':memory:'`` is used by backtests and tests."""

    def __init__(self, path: str | Path = ":memory:"):
        if str(path) != ":memory:":
            Path(path).parent.mkdir(parents=True, exist_ok=True)
        self._conn = sqlite3.connect(str(path), check_same_thread=False, isolation_level=None)
        self._lock = threading.Lock()
        with self._lock:
            if str(path) != ":memory:":
                self._conn.execute("PRAGMA journal_mode=WAL")
                self._conn.execute("PRAGMA synchronous=FULL")
            self._conn.executescript(_DDL)
            self._conn.execute("INSERT OR IGNORE INTO meta VALUES ('schema_version', ?)",
                               (str(SCHEMA_VERSION),))

    def _exec(self, sql: str, params: tuple = ()) -> list[tuple]:
        with self._lock:
            cur = self._conn.execute(sql, params)
            return cur.fetchall()

    # -- orders / fills / trades -----------------------------------------------------------
    def save_order(self, order: Any) -> None:
        self._exec("INSERT OR REPLACE INTO orders VALUES (?,?,?,?,?)",
                   (order.client_ref, order.status.value, order.strategy_id,
                    datetime.now().isoformat(), dumps(order)))

    def load_orders(self, open_only: bool = False) -> list[dict[str, Any]]:
        sql = "SELECT body FROM orders"
        if open_only:
            sql += " WHERE status NOT IN ('FILLED','CANCELLED','REJECTED','EXPIRED')"
        return [json.loads(r[0]) for r in self._exec(sql)]

    def save_fill(self, fill: Any) -> None:
        self._exec("INSERT OR REPLACE INTO fills VALUES (?,?,?,?)",
                   (fill.fill_id, fill.client_ref, fill.ts.isoformat(), dumps(fill)))

    def save_trade(self, trade: Any) -> None:
        self._exec("INSERT OR REPLACE INTO trades VALUES (?,?,?,?,?)",
                   (trade.trade_id, trade.strategy_id, trade.status,
                    datetime.now().isoformat(), dumps(trade)))

    def load_trades(self, status: str | None = None) -> list[dict[str, Any]]:
        if status:
            rows = self._exec("SELECT body FROM trades WHERE status=?", (status,))
        else:
            rows = self._exec("SELECT body FROM trades")
        return [json.loads(r[0]) for r in rows]

    # -- key/value, heartbeats, equity -----------------------------------------------------
    def set(self, key: str, value: Any) -> None:
        self._exec("INSERT OR REPLACE INTO kv VALUES (?,?,?)",
                   (key, dumps(value), datetime.now().isoformat()))

    def get(self, key: str, default: Any = None) -> Any:
        rows = self._exec("SELECT value FROM kv WHERE key=?", (key,))
        return json.loads(rows[0][0]) if rows else default

    def heartbeat(self, process: str, ts: float) -> None:
        self._exec("INSERT OR REPLACE INTO heartbeats VALUES (?,?)", (process, ts))

    def heartbeats(self) -> dict[str, float]:
        return {p: t for p, t in self._exec("SELECT process, ts FROM heartbeats")}

    def save_equity(self, day: date, row: dict[str, Any]) -> None:
        self._exec("INSERT OR REPLACE INTO equity VALUES (?,?)", (day.isoformat(), dumps(row)))

    def close(self) -> None:
        with self._lock:
            self._conn.close()
