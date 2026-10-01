"""Reconciliation (FR-11.3, FR-8.6): the broker's figures are the truth.

Compares net positions per symbol in the internal book with the broker. Any difference blocks
new entries (via ``HealthFlags.reconciled``) and raises an alert / banner until resolved.
"""

from __future__ import annotations

from dataclasses import dataclass, field


@dataclass
class ReconResult:
    ok: bool
    mismatches: dict[str, tuple[int, int]] = field(default_factory=dict)  # symbol -> (internal, broker)

    def banner(self) -> str:
        if self.ok:
            return "positions reconciled"
        parts = [f"{s}: internal {i} vs broker {b}" for s, (i, b) in sorted(self.mismatches.items())]
        return "POSITION MISMATCH - new entries blocked: " + "; ".join(parts)


def reconcile(internal: dict[str, int], broker: dict[str, int], symbol_map=None) -> ReconResult:
    """``symbol_map`` optionally converts internal symbols to the broker's naming."""
    mapped: dict[str, int] = {}
    for sym, qty in internal.items():
        key = symbol_map(sym) if symbol_map else sym
        mapped[key] = mapped.get(key, 0) + qty
    mism = {}
    for sym in set(mapped) | set(broker):
        a, b = mapped.get(sym, 0), broker.get(sym, 0)
        if a != b:
            mism[sym] = (a, b)
    return ReconResult(not mism, mism)
