"""Broker interface (PRD s.6 ``BrokerGateway``).

The core only talks to this interface; each vendor is an adapter behind it. The paper broker
implements the same interface as the live broker (FR-6.8), so a strategy cannot tell the
modes apart.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass
from typing import Any

from ..core.models import Fill, Order, OrderStatus


class BrokerError(Exception):
    """Base class for broker failures."""


class OrderRejected(BrokerError):
    """Synchronous rejection at placement. ``reason`` is classified by the order manager."""

    def __init__(self, reason: str):
        super().__init__(reason)
        self.reason = reason


class BrokerTimeout(BrokerError):
    """Request outcome unknown; the order manager must query the order book before retrying."""


@dataclass
class StatusUpdate:
    client_ref: str
    status: OrderStatus
    reason: str = ""


class BrokerGateway(ABC):
    name: str = "broker"
    exit_only: bool = False      # the fallback broker never opens new risk (PRD s.6)

    @abstractmethod
    def place_order(self, order: Order) -> str:
        """Place an order; returns the broker order id. Raises OrderRejected / BrokerTimeout."""

    @abstractmethod
    def modify_order(self, broker_order_id: str, trigger_price: float | None = None,
                     limit_price: float | None = None) -> None: ...

    @abstractmethod
    def cancel_order(self, broker_order_id: str) -> None: ...

    @abstractmethod
    def find_order(self, client_ref: str) -> dict[str, Any] | None:
        """Look up an order by client reference (idempotent retry, FR-6.6)."""

    @abstractmethod
    def poll(self) -> tuple[list[Fill], list[StatusUpdate]]:
        """New fills and status changes since the previous call."""

    @abstractmethod
    def positions(self) -> dict[str, int]:
        """Net signed units per tradable symbol (broker's truth for reconciliation)."""

    @abstractmethod
    def margins(self) -> dict[str, float]:
        """{'available': ..., 'used': ...} in rupees."""

    def connected(self) -> bool:
        return True
