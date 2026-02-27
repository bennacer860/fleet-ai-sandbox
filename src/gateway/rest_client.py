"""Async wrapper around the synchronous CLOB and Gamma REST clients.

Wraps blocking calls in ``run_in_executor`` so the asyncio event loop
stays responsive.  The underlying ``py_clob_client`` and ``requests``
libraries remain unchanged.
"""

from __future__ import annotations

import asyncio
import time
from typing import Any, Optional

from ..clob_client import get_order_status, place_limit_order
from ..core.events import OrderStatus, OrderSubmitted, OrderTerminal
from ..core.models import OrderIntent, Side
from ..gamma_client import fetch_event_by_slug
from ..logging_config import get_logger
from ..utils.market_data import get_market_evaluation, get_min_order_size

logger = get_logger(__name__)


class AsyncRestClient:
    """Non-blocking façade over the synchronous CLOB and Gamma APIs."""

    # ── Order placement ───────────────────────────────────────────────────

    async def place_order(
        self,
        intent: OrderIntent,
        dry_run: bool = False,
    ) -> OrderSubmitted | OrderTerminal:
        """Submit an order to the CLOB API (or simulate in dry-run).

        Returns an ``OrderSubmitted`` on success / dry-run, or an
        ``OrderTerminal`` if the order was rejected or failed.
        """
        signal_ns = time.time_ns()

        if dry_run:
            logger.info(
                "[DRY-RUN] Would place %s %s @ %.4f x %.2f for %s",
                intent.side.value, intent.slug, intent.price, intent.size, intent.token_id[:20],
            )
            return OrderSubmitted(
                order_id=f"dry_{signal_ns}",
                token_id=intent.token_id,
                slug=intent.slug,
                strategy=intent.strategy,
                price=intent.price,
                size=intent.size,
                side=intent.side.value,
                dry_run=True,
            )

        loop = asyncio.get_event_loop()
        resp: dict[str, Any] | None = await loop.run_in_executor(
            None,
            lambda: place_limit_order(
                token_id=intent.token_id,
                price=intent.price,
                size=intent.size,
                side=intent.side.value,
                tick_size=intent.tick_size,
            ),
        )

        rest_ns = time.time_ns()

        if resp is None:
            return OrderTerminal(
                order_id="",
                status=OrderStatus.FAILED,
                reason="CLOB client returned None",
            )

        if resp.get("success"):
            order_id = resp.get("orderId", "unknown")
            latency_ms = (rest_ns - signal_ns) / 1_000_000
            logger.info(
                "[ORDER] Submitted %s: %s %s @ %.4f x %.2f (%.0fms)",
                order_id, intent.side.value, intent.slug,
                intent.price, intent.size, latency_ms,
            )
            return OrderSubmitted(
                order_id=order_id,
                token_id=intent.token_id,
                slug=intent.slug,
                strategy=intent.strategy,
                price=intent.price,
                size=intent.size,
                side=intent.side.value,
                timestamp_ns=rest_ns,
            )

        error_msg = resp.get("errorMsg", "unknown")
        logger.warning("[ORDER] Rejected: %s for %s", error_msg, intent.slug)
        return OrderTerminal(
            order_id=resp.get("orderId", ""),
            status=OrderStatus.REJECTED,
            reason=error_msg,
        )

    # ── Order status ─────────────────────────────────────────────────

    async def get_order(self, order_id: str) -> dict[str, Any] | None:
        """Fetch a single order's current state from the CLOB REST API."""
        loop = asyncio.get_event_loop()
        return await loop.run_in_executor(None, get_order_status, order_id)

    # ── Market data ───────────────────────────────────────────────────────

    async def fetch_market_eval(self, slug: str) -> dict[str, Any] | None:
        loop = asyncio.get_event_loop()
        return await loop.run_in_executor(None, get_market_evaluation, slug)

    async def fetch_min_order_size(self, token_id: str) -> float:
        loop = asyncio.get_event_loop()
        return await loop.run_in_executor(None, get_min_order_size, token_id)

    async def fetch_event(self, slug: str) -> dict[str, Any] | None:
        loop = asyncio.get_event_loop()
        return await loop.run_in_executor(None, fetch_event_by_slug, slug)
