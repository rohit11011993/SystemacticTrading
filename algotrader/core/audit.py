"""Append-only, hash-chained audit log (PRD s.11, FR-7.3, FR-9.3, FR-11.2, FR-15.5).

Every gateway decision, kill-switch trip and reset, manual action, login and configuration
change is written as one JSON line. Each record carries the hash of the previous record, so
editing or deleting any line breaks the chain and ``verify`` reports where.
"""

from __future__ import annotations

import hashlib
import json
import threading
from datetime import datetime, date
from enum import Enum
from pathlib import Path
from typing import Any

_SECRET_KEYS = ("token", "secret", "api_key", "password", "access", "account")
_GENESIS = "0" * 64


def _mask(obj: Any) -> Any:
    """Mask secrets, tokens and account identifiers before anything is written (FR-15.5)."""
    if isinstance(obj, dict):
        return {k: ("***" if any(s in str(k).lower() for s in _SECRET_KEYS) else _mask(v))
                for k, v in obj.items()}
    if isinstance(obj, (list, tuple)):
        return [_mask(v) for v in obj]
    return obj


def _default(o: Any) -> Any:
    if isinstance(o, (datetime, date)):
        return o.isoformat()
    if isinstance(o, Enum):
        return o.value
    if hasattr(o, "__dataclass_fields__"):
        return {k: getattr(o, k) for k in o.__dataclass_fields__}
    return str(o)


class AuditLog:
    """Thread-safe hash-chained JSONL writer. ``path=None`` keeps records in memory (tests)."""

    def __init__(self, path: str | Path | None = None):
        self.path = Path(path) if path else None
        self._lock = threading.Lock()
        self._memory: list[dict[str, Any]] = []
        self._prev = _GENESIS
        if self.path:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            if self.path.exists():
                last = None
                with self.path.open("r", encoding="utf-8") as fh:
                    for line in fh:
                        if line.strip():
                            last = line
                if last:
                    self._prev = json.loads(last)["hash"]

    def record(self, event: str, actor: str, payload: dict[str, Any] | None = None,
               ts: datetime | None = None) -> dict[str, Any]:
        with self._lock:
            body = {
                "ts": (ts or datetime.now()).isoformat(),
                "event": event,
                "actor": actor,
                "payload": _mask(payload or {}),
                "prev": self._prev,
            }
            canonical = json.dumps(body, sort_keys=True, default=_default)
            body["hash"] = hashlib.sha256(canonical.encode()).hexdigest()
            self._prev = body["hash"]
            if self.path:
                with self.path.open("a", encoding="utf-8") as fh:
                    fh.write(json.dumps(body, sort_keys=True, default=_default) + "\n")
            else:
                self._memory.append(json.loads(json.dumps(body, default=_default)))
            return body

    def records(self) -> list[dict[str, Any]]:
        if self.path and self.path.exists():
            with self.path.open("r", encoding="utf-8") as fh:
                return [json.loads(line) for line in fh if line.strip()]
        return list(self._memory)

    @staticmethod
    def verify_records(records: list[dict[str, Any]]) -> tuple[bool, int | None]:
        """Recompute the chain. Returns (ok, index of first bad record)."""
        prev = _GENESIS
        for i, rec in enumerate(records):
            body = {k: v for k, v in rec.items() if k != "hash"}
            if body.get("prev") != prev:
                return False, i
            digest = hashlib.sha256(json.dumps(body, sort_keys=True, default=_default).encode()
                                    ).hexdigest()
            if digest != rec.get("hash"):
                return False, i
            prev = rec["hash"]
        return True, None

    def verify(self) -> tuple[bool, int | None]:
        return self.verify_records(self.records())
