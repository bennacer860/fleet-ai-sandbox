"""Domain models for orders, positions, and trade records.

These are mutable working objects (unlike events which are frozen).
They represent the current state of orders and positions as tracked
by the execution layer.
"""

from __future__ import annotations

import time
import uuid
from dataclasses import dataclass, field
from enum import Enum

from .events import OrderStatus


class Side(Enum):
    BUY = "BUY"
    SELL = "SELL"


# ── Strategy output ───────────────────────────────────────────────────────────


@dataclass(frozen=True, slots=True)
class OrderIntent:
    """What a strategy wants to do — produced by strategy, consumed by OrderManager."""

    token_id: str
    price: float
    size: float
    side: Side
    strategy: str
    slug: str
    tick_size: float | None = None


# ── Order lifecycle state ─────────────────────────────────────────────────────


@dataclass(slots=True)
class OrderState:
    """Tracks a single order from submission through fill/cancel/expiry."""

    order_id: str
    intent: OrderIntent
    status: OrderStatus = OrderStatus.SUBMITTED
    placed_at_ns: int = field(default_factory=time.time_ns)
    signal_ns: int | None = None
    rest_response_ns: int | None = None
    filled_size: float = 0.0
    fill_price: float | None = None
    rejection_reason: str = ""
    resolved_at_ns: int | None = None
    dry_run: bool = False

    @property
    def is_terminal(self) -> bool:
        return self.status in (
            OrderStatus.FILLED,
            OrderStatus.CANCELLED,
            OrderStatus.REJECTED,
            OrderStatus.EXPIRED,
            OrderStatus.EXPIRED_STALE,
            OrderStatus.FAILED,
        )

    @property
    def signal_to_rest_ms(self) -> float | None:
        if self.signal_ns is not None and self.rest_response_ns is not None:
            return (self.rest_response_ns - self.signal_ns) / 1_000_000
        return None

    @property
    def signal_to_fill_ms(self) -> float | None:
        if self.signal_ns is not None and self.resolved_at_ns is not None:
            return (self.resolved_at_ns - self.signal_ns) / 1_000_000
        return None


# ── Position tracking ─────────────────────────────────────────────────────────


@dataclass(slots=True)
class Position:
    """Represents a held position in a single token."""

    token_id: str
    slug: str
    strategy: str
    quantity: float = 0.0
    avg_entry_price: float = 0.0
    realized_pnl: float = 0.0

    @property
    def cost_basis(self) -> float:
        return self.quantity * self.avg_entry_price

    def unrealized_pnl(self, current_price: float) -> float:
        return self.quantity * (current_price - self.avg_entry_price)

    def apply_fill(self, fill_size: float, fill_price: float, side: Side) -> None:
        if side == Side.BUY:
            total_cost = self.cost_basis + fill_size * fill_price
            self.quantity += fill_size
            if self.quantity > 0:
                self.avg_entry_price = total_cost / self.quantity
        else:
            self.realized_pnl += fill_size * (fill_price - self.avg_entry_price)
            self.quantity -= fill_size
            if self.quantity <= 1e-9:
                self.quantity = 0.0
                self.avg_entry_price = 0.0


# ── Closed trade record (for analytics) ──────────────────────────────────────


@dataclass(frozen=True, slots=True)
class TradeRecord:
    """Immutable record of a completed trade for P&L analytics."""

    trade_id: str = field(default_factory=lambda: uuid.uuid4().hex[:16])
    strategy: str = ""
    slug: str = ""
    token_id: str = ""
    side: str = ""
    entry_price: float = 0.0
    exit_price: float = 0.0
    size: float = 0.0
    gross_pnl: float = 0.0
    net_pnl: float = 0.0
    fees: float = 0.0
    hold_duration_s: float = 0.0
    timestamp_entry: float = 0.0
    timestamp_exit: float = 0.0
    dry_run: bool = False
