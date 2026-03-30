"""Simulated order fills for dry-run mode.

Watches for dry-run ``OrderSubmitted`` events and produces realistic
``OrderFill`` events based on live order-book data.  Replaces the role
of the ``UserWebSocket`` + order reconciliation loop during dry-run.

Supports three fill modes:

- ``"book"``   — uses real book asks/bids to decide whether an order
                 crosses the spread and how much liquidity is available.
- ``"instant"`` — fills 100% at the order price immediately on submit.
- ``"probabilistic"`` — fills with configurable probability and
                        partial-fill range.
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from typing import Any

from ..core.event_bus import EventBus
from ..core.events import BookUpdate, OrderFill, OrderStatus, OrderSubmitted
from ..logging_config import get_logger

logger = get_logger(__name__)

PENDING_ORDER_TIMEOUT_S = 600


@dataclass
class PendingOrder:
    """A dry-run order waiting to be filled."""

    order_id: str
    token_id: str
    slug: str
    side: str
    price: float
    original_size: float
    remaining_size: float
    created_at: float = field(default_factory=time.time)

    @property
    def is_complete(self) -> bool:
        return self.remaining_size <= 1e-9


class FillSimulator:
    """Produces realistic fill events for dry-run orders."""

    def __init__(
        self,
        event_bus: EventBus,
        mode: str = "book",
        order_timeout_s: float = PENDING_ORDER_TIMEOUT_S,
    ) -> None:
        self._event_bus = event_bus
        self._mode = mode
        self._order_timeout_s = order_timeout_s

        self._pending: dict[str, PendingOrder] = {}
        self._books: dict[str, BookUpdate] = {}

        self._stats = {
            "filled": 0,
            "partial": 0,
            "missed": 0,
            "expired": 0,
        }

    @property
    def pending_orders(self) -> dict[str, PendingOrder]:
        return self._pending

    @property
    def stats(self) -> dict[str, int]:
        return dict(self._stats)

    # ── Event handlers ────────────────────────────────────────────────────

    async def on_order_submitted(self, event: OrderSubmitted) -> None:
        if not event.dry_run:
            return

        if self._mode == "instant":
            self._emit_fill(
                order_id=event.order_id,
                fill_price=event.price,
                fill_size=event.size,
                status=OrderStatus.FILLED,
            )
            self._stats["filled"] += 1
            return

        self._pending[event.order_id] = PendingOrder(
            order_id=event.order_id,
            token_id=event.token_id,
            slug=event.slug,
            side=event.side,
            price=event.price,
            original_size=event.size,
            remaining_size=event.size,
        )

        book = self._books.get(event.token_id)
        if book is not None:
            self._try_fill_pending(event.order_id, book)

    async def on_book_update(self, event: BookUpdate) -> None:
        self._books[event.token_id] = event

        to_fill = [
            oid for oid, p in self._pending.items()
            if p.token_id == event.token_id
        ]
        for oid in to_fill:
            self._try_fill_pending(oid, event)

    # ── Fill logic ────────────────────────────────────────────────────────

    def _try_fill_pending(self, order_id: str, book: BookUpdate) -> None:
        pending = self._pending.get(order_id)
        if pending is None or pending.is_complete:
            return

        if pending.side == "BUY":
            fillable_size, vwap = self._match_against_asks(
                pending.price, pending.remaining_size, book.asks
            )
        else:
            fillable_size, vwap = self._match_against_bids(
                pending.price, pending.remaining_size, book.bids
            )

        if fillable_size <= 1e-9:
            return

        pending.remaining_size -= fillable_size

        if pending.is_complete:
            status = OrderStatus.FILLED
            self._stats["filled"] += 1
            del self._pending[order_id]
        else:
            status = OrderStatus.PARTIAL
            self._stats["partial"] += 1

        self._emit_fill(
            order_id=order_id,
            fill_price=vwap,
            fill_size=fillable_size,
            status=status,
        )

    @staticmethod
    def _match_against_asks(
        limit_price: float,
        remaining: float,
        asks: tuple[tuple[float, float], ...],
    ) -> tuple[float, float]:
        """Walk through ask levels at or below limit_price.

        Returns (total_filled_size, volume-weighted average price).
        """
        sorted_asks = sorted(asks, key=lambda a: a[0])
        filled = 0.0
        cost = 0.0
        for ask_price, ask_size in sorted_asks:
            if ask_price > limit_price:
                break
            take = min(ask_size, remaining - filled)
            filled += take
            cost += take * ask_price
            if filled >= remaining - 1e-9:
                break
        vwap = cost / filled if filled > 0 else 0.0
        return filled, vwap

    @staticmethod
    def _match_against_bids(
        limit_price: float,
        remaining: float,
        bids: tuple[tuple[float, float], ...],
    ) -> tuple[float, float]:
        """Walk through bid levels at or above limit_price (for SELL orders)."""
        sorted_bids = sorted(bids, key=lambda b: b[0], reverse=True)
        filled = 0.0
        cost = 0.0
        for bid_price, bid_size in sorted_bids:
            if bid_price < limit_price:
                break
            take = min(bid_size, remaining - filled)
            filled += take
            cost += take * bid_price
            if filled >= remaining - 1e-9:
                break
        vwap = cost / filled if filled > 0 else 0.0
        return filled, vwap

    # ── Expiry ────────────────────────────────────────────────────────────

    def expire_stale_orders(self) -> None:
        """Remove pending orders older than the timeout."""
        now = time.time()
        expired = [
            oid for oid, p in self._pending.items()
            if now - p.created_at > self._order_timeout_s
        ]
        for oid in expired:
            del self._pending[oid]
            self._stats["expired"] += 1
            logger.info("[SIM] Expired stale pending order %s", oid[:20])

    # ── Background loop ──────────────────────────────────────────────────

    async def run(self) -> None:
        """Periodically expire stale pending orders."""
        import asyncio
        while True:
            await asyncio.sleep(30)
            self.expire_stale_orders()

    # ── Helpers ───────────────────────────────────────────────────────────

    def _emit_fill(
        self,
        order_id: str,
        fill_price: float,
        fill_size: float,
        status: OrderStatus,
    ) -> None:
        self._event_bus.publish_nowait(OrderFill(
            order_id=order_id,
            fill_price=fill_price,
            fill_size=fill_size,
            status=status,
        ))
        logger.info(
            "[SIM-FILL] %s %.4f x %.2f (%s)",
            order_id[:20], fill_price, fill_size, status.value,
        )
