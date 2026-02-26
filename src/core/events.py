"""Typed event dataclasses for the event-driven architecture.

All events are frozen (immutable) and use __slots__ for memory efficiency.
They flow from gateway -> event bus -> strategy / execution / monitoring.
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from enum import Enum


class OrderStatus(Enum):
    SUBMITTED = "SUBMITTED"
    LIVE = "LIVE"
    PARTIAL = "PARTIAL"
    FILLED = "FILLED"
    CANCELLED = "CANCELLED"
    REJECTED = "REJECTED"
    EXPIRED = "EXPIRED"
    EXPIRED_STALE = "EXPIRED_STALE"
    FAILED = "FAILED"


def _now_ns() -> int:
    return time.time_ns()


# ── Market data events ────────────────────────────────────────────────────────


@dataclass(frozen=True, slots=True)
class BookUpdate:
    token_id: str
    condition_id: str
    slug: str
    bids: tuple[tuple[float, float], ...]
    asks: tuple[tuple[float, float], ...]
    best_bid: float
    best_ask: float
    timestamp_ns: int = field(default_factory=_now_ns)


@dataclass(frozen=True, slots=True)
class TickSizeChange:
    condition_id: str
    slug: str
    token_id: str
    old_tick_size: str
    new_tick_size: str
    timestamp_ns: int = field(default_factory=_now_ns)


@dataclass(frozen=True, slots=True)
class LastTradePrice:
    token_id: str
    slug: str
    price: float
    size: float
    side: str
    timestamp_ns: int = field(default_factory=_now_ns)


# ── Order lifecycle events ────────────────────────────────────────────────────


@dataclass(frozen=True, slots=True)
class OrderSubmitted:
    order_id: str
    token_id: str
    slug: str
    strategy: str
    price: float
    size: float
    side: str
    dry_run: bool = False
    timestamp_ns: int = field(default_factory=_now_ns)


@dataclass(frozen=True, slots=True)
class OrderLive:
    order_id: str
    timestamp_ns: int = field(default_factory=_now_ns)


@dataclass(frozen=True, slots=True)
class OrderFill:
    order_id: str
    fill_price: float
    fill_size: float
    status: OrderStatus
    timestamp_ns: int = field(default_factory=_now_ns)


@dataclass(frozen=True, slots=True)
class OrderTerminal:
    order_id: str
    status: OrderStatus
    reason: str = ""
    timestamp_ns: int = field(default_factory=_now_ns)


# ── Market metadata events ────────────────────────────────────────────────────


@dataclass(frozen=True, slots=True)
class MarketMeta:
    slug: str
    condition_id: str
    token_ids: tuple[str, ...]
    outcomes: tuple[str, ...]
    timestamp_ns: int = field(default_factory=_now_ns)


@dataclass(frozen=True, slots=True)
class MarketResolved:
    slug: str
    condition_id: str
    winning_token_id: str
    timestamp_ns: int = field(default_factory=_now_ns)


# ── System events ─────────────────────────────────────────────────────────────


@dataclass(frozen=True, slots=True)
class HealthTick:
    timestamp_ns: int = field(default_factory=_now_ns)


# Union type for dispatching
Event = (
    BookUpdate
    | TickSizeChange
    | LastTradePrice
    | OrderSubmitted
    | OrderLive
    | OrderFill
    | OrderTerminal
    | MarketMeta
    | MarketResolved
    | HealthTick
)
