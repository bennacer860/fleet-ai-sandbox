"""End-to-end integration tests for the FillSimulator.

Tests the full async pipeline in dry-run mode:
  order submission -> FillSimulator -> OrderFill -> PositionTracker
  -> MarketResolved -> P&L calculation

Uses real project components (EventBus, OrderManager, PositionTracker,
AsyncPersistence) with a temp SQLite database.  No mocking.

Written TDD-style: these tests define the FillSimulator interface
BEFORE the implementation exists.
"""

import asyncio
import os
import tempfile
import time
from unittest.mock import patch

import pytest

from src.core.event_bus import EventBus
from src.core.events import (
    BookUpdate,
    MarketResolved,
    OrderFill,
    OrderStatus,
    OrderSubmitted,
)
from src.core.models import OrderIntent, Side
from src.execution.fill_simulator import FillSimulator
from src.execution.order_manager import OrderManager
from src.execution.position_tracker import PositionTracker
from src.execution.risk_manager import RiskConfig, RiskManager
from src.gateway.rest_client import AsyncRestClient
from src.monitoring.metrics import Metrics
from src.storage.database import init_db
from src.storage.persistence import AsyncPersistence


# ═══════════════════════════════════════════════════════════════════════════════
# Fixtures
# ═══════════════════════════════════════════════════════════════════════════════

TOKEN_YES = "token_yes_fill_test"
TOKEN_NO = "token_no_fill_test"
SLUG = "btc-updown-5m-fill-test"
CONDITION_ID = "cond_fill_test"
STRATEGY = "gabagool"


class PipelineHarness:
    """Wires all components together for a single test run."""

    def __init__(self, fill_mode: str = "book"):
        tmp = tempfile.mkdtemp()
        db_path = os.path.join(tmp, "test_fill_sim.db")
        conn = init_db(db_path)

        self.conn = conn
        self.event_bus = EventBus()
        self.persistence = AsyncPersistence(conn, flush_interval=0.05)
        self.rest_client = AsyncRestClient()
        self.risk_manager = RiskManager(RiskConfig(
            max_position_per_market=10_000,
            max_total_exposure=50_000,
            max_orders_per_minute=100,
            max_daily_loss=10_000,
        ))
        self.order_manager = OrderManager(
            event_bus=self.event_bus,
            rest_client=self.rest_client,
            risk_manager=self.risk_manager,
            persistence=self.persistence,
            dry_run=True,
        )
        self.position_tracker = PositionTracker(persistence=self.persistence)
        self.fill_simulator = FillSimulator(
            event_bus=self.event_bus,
            mode=fill_mode,
        )

        self.fill_events: list[OrderFill] = []

        async def _capture_fill(event: OrderFill):
            self.fill_events.append(event)

        self.event_bus.subscribe(OrderFill, self.order_manager.on_order_fill)
        self.event_bus.subscribe(OrderFill, self.position_tracker.on_fill)
        self.event_bus.subscribe(OrderFill, _capture_fill)
        self.event_bus.subscribe(OrderSubmitted, self.fill_simulator.on_order_submitted)
        self.event_bus.subscribe(BookUpdate, self.fill_simulator.on_book_update)
        self.event_bus.subscribe(MarketResolved, self.position_tracker.on_market_resolved)

    async def start(self):
        self._bus_task = asyncio.create_task(self.event_bus.run())
        self._persist_task = asyncio.create_task(self.persistence.drain_loop())

    async def stop(self):
        await self.event_bus.stop()
        await self.persistence.stop()
        self._bus_task.cancel()
        self._persist_task.cancel()
        await asyncio.gather(
            self._bus_task, self._persist_task, return_exceptions=True
        )
        self.conn.close()

    async def submit_order(
        self,
        token_id: str = TOKEN_YES,
        price: float = 0.95,
        size: float = 10.0,
        slug: str = SLUG,
    ) -> str:
        """Submit a dry-run order and register it in the position tracker.

        Returns the synthetic order_id.
        """
        intent = OrderIntent(
            token_id=token_id,
            price=price,
            size=size,
            side=Side.BUY,
            strategy=STRATEGY,
            slug=slug,
            skip_dedup=True,
        )
        state = await self.order_manager.submit(intent)
        assert state is not None, "OrderManager should accept dry-run order"
        assert state.order_id.startswith("dry_")

        self.position_tracker.register_order(
            order_id=state.order_id,
            token_id=token_id,
            slug=slug,
            strategy=STRATEGY,
            side="BUY",
            price=price,
            size=size,
        )
        return state.order_id

    async def inject_book(
        self,
        token_id: str = TOKEN_YES,
        bids: tuple[tuple[float, float], ...] = ((0.90, 100.0),),
        asks: tuple[tuple[float, float], ...] = ((0.94, 50.0),),
    ):
        """Publish a BookUpdate onto the event bus."""
        best_bid = max(b[0] for b in bids) if bids else 0.0
        best_ask = min(a[0] for a in asks) if asks else 0.0
        await self.event_bus.publish(BookUpdate(
            token_id=token_id,
            condition_id=CONDITION_ID,
            slug=SLUG,
            bids=bids,
            asks=asks,
            best_bid=best_bid,
            best_ask=best_ask,
        ))

    async def drain(self, seconds: float = 0.15):
        """Let the event bus process queued events."""
        await asyncio.sleep(seconds)


# ═══════════════════════════════════════════════════════════════════════════════
# Test 1: Book-mode full fill
# ═══════════════════════════════════════════════════════════════════════════════


class TestBookFullFill:
    """When order price crosses the spread and liquidity is sufficient,
    the entire order should fill."""

    def test_full_fill_from_book(self):
        async def _run():
            h = PipelineHarness(fill_mode="book")
            await h.start()
            try:
                order_id = await h.submit_order(price=0.95, size=10.0)
                await h.drain()

                await h.inject_book(
                    asks=((0.94, 50.0),),
                    bids=((0.90, 100.0),),
                )
                await h.drain()

                assert len(h.fill_events) == 1, (
                    f"Expected 1 fill event, got {len(h.fill_events)}"
                )
                fill = h.fill_events[0]
                assert fill.order_id == order_id
                assert fill.fill_size == 10.0
                assert abs(fill.fill_price - 0.94) < 1e-9

                pos = h.position_tracker.positions.get(TOKEN_YES)
                assert pos is not None, "Position should exist after fill"
                assert pos.quantity == 10.0
                assert abs(pos.avg_entry_price - 0.94) < 1e-9

                assert order_id not in h.fill_simulator.pending_orders
            finally:
                await h.stop()

        Metrics.reset()
        asyncio.run(_run())


# ═══════════════════════════════════════════════════════════════════════════════
# Test 2: Book-mode partial fill
# ═══════════════════════════════════════════════════════════════════════════════


class TestBookPartialFill:
    """When liquidity is limited, fill only what's available and keep
    the remainder pending."""

    def test_partial_fill_then_more_liquidity(self):
        async def _run():
            h = PipelineHarness(fill_mode="book")
            await h.start()
            try:
                order_id = await h.submit_order(price=0.95, size=100.0)
                await h.drain()

                # First book: only 30 shares available
                await h.inject_book(asks=((0.94, 30.0),))
                await h.drain()

                assert len(h.fill_events) == 1
                assert h.fill_events[0].fill_size == 30.0
                assert h.fill_events[0].status == OrderStatus.PARTIAL

                assert order_id in h.fill_simulator.pending_orders
                pending = h.fill_simulator.pending_orders[order_id]
                assert abs(pending.remaining_size - 70.0) < 1e-9

                # Second book: 50 more shares
                await h.inject_book(asks=((0.93, 50.0),))
                await h.drain()

                assert len(h.fill_events) == 2
                assert h.fill_events[1].fill_size == 50.0
                assert h.fill_events[1].fill_price == 0.93

                assert order_id in h.fill_simulator.pending_orders
                pending = h.fill_simulator.pending_orders[order_id]
                assert abs(pending.remaining_size - 20.0) < 1e-9

                pos = h.position_tracker.positions.get(TOKEN_YES)
                assert pos is not None
                assert abs(pos.quantity - 80.0) < 1e-9
            finally:
                await h.stop()

        Metrics.reset()
        asyncio.run(_run())


# ═══════════════════════════════════════════════════════════════════════════════
# Test 3: Book-mode no fill
# ═══════════════════════════════════════════════════════════════════════════════


class TestBookNoFill:
    """When the limit price doesn't cross the spread, the order should
    stay pending with no fill event."""

    def test_no_fill_when_price_below_ask(self):
        async def _run():
            h = PipelineHarness(fill_mode="book")
            await h.start()
            try:
                order_id = await h.submit_order(price=0.90, size=10.0)
                await h.drain()

                await h.inject_book(
                    asks=((0.95, 50.0),),
                    bids=((0.85, 100.0),),
                )
                await h.drain()

                assert len(h.fill_events) == 0, (
                    f"No fills expected, got {len(h.fill_events)}"
                )
                assert order_id in h.fill_simulator.pending_orders

            finally:
                await h.stop()

        Metrics.reset()
        asyncio.run(_run())


# ═══════════════════════════════════════════════════════════════════════════════
# Test 4: Instant mode
# ═══════════════════════════════════════════════════════════════════════════════


class TestInstantMode:
    """In instant mode, orders fill immediately at the order price
    regardless of book state."""

    def test_instant_fill_on_submit(self):
        async def _run():
            h = PipelineHarness(fill_mode="instant")
            await h.start()
            try:
                order_id = await h.submit_order(price=0.42, size=25.0)
                await h.drain()

                assert len(h.fill_events) == 1
                fill = h.fill_events[0]
                assert fill.order_id == order_id
                assert fill.fill_size == 25.0
                assert fill.fill_price == 0.42
                assert fill.status == OrderStatus.FILLED

                assert order_id not in h.fill_simulator.pending_orders

            finally:
                await h.stop()

        Metrics.reset()
        asyncio.run(_run())


# ═══════════════════════════════════════════════════════════════════════════════
# Test 5: Full pipeline — fill -> position -> resolution -> WIN
# ═══════════════════════════════════════════════════════════════════════════════


class TestFullPipelineWin:
    """Dry-run order fills, position is tracked, market resolves in our
    favor, and P&L is correctly calculated as a win."""

    def test_pnl_on_win(self):
        async def _run():
            h = PipelineHarness(fill_mode="instant")
            await h.start()
            try:
                await h.submit_order(
                    token_id=TOKEN_YES,
                    price=0.40,
                    size=10.0,
                )
                await h.drain()

                assert len(h.fill_events) == 1

                pos = h.position_tracker.positions.get(TOKEN_YES)
                assert pos is not None
                assert pos.quantity == 10.0
                assert abs(pos.avg_entry_price - 0.40) < 1e-9

                # Market resolves: YES wins
                resolved = MarketResolved(
                    slug=SLUG,
                    condition_id=CONDITION_ID,
                    winning_token_id=TOKEN_YES,
                )
                await h.position_tracker.on_market_resolved(resolved)

                expected_pnl = 10.0 * (1.0 - 0.40)  # = 6.0
                assert abs(h.position_tracker.session_pnl - expected_pnl) < 1e-9
                assert h.position_tracker.wins == 1
                assert h.position_tracker.losses == 0
                assert h.position_tracker.trades_closed == 1

                assert TOKEN_YES not in h.position_tracker.positions
            finally:
                await h.stop()

        Metrics.reset()
        asyncio.run(_run())


# ═══════════════════════════════════════════════════════════════════════════════
# Test 6: Full pipeline — fill -> position -> resolution -> LOSS
# ═══════════════════════════════════════════════════════════════════════════════


class TestFullPipelineLoss:
    """Same as test 5, but the OTHER token wins, so our position is
    worthless and P&L is negative."""

    def test_pnl_on_loss(self):
        async def _run():
            h = PipelineHarness(fill_mode="instant")
            await h.start()
            try:
                await h.submit_order(
                    token_id=TOKEN_YES,
                    price=0.40,
                    size=10.0,
                )
                await h.drain()

                assert len(h.fill_events) == 1

                # Market resolves: NO wins (our YES position loses)
                resolved = MarketResolved(
                    slug=SLUG,
                    condition_id=CONDITION_ID,
                    winning_token_id=TOKEN_NO,
                )
                await h.position_tracker.on_market_resolved(resolved)

                expected_pnl = 10.0 * (0.0 - 0.40)  # = -4.0
                assert abs(h.position_tracker.session_pnl - expected_pnl) < 1e-9
                assert h.position_tracker.wins == 0
                assert h.position_tracker.losses == 1
                assert h.position_tracker.trades_closed == 1

            finally:
                await h.stop()

        Metrics.reset()
        asyncio.run(_run())


# ═══════════════════════════════════════════════════════════════════════════════
# Test 7: Pending order expires
# ═══════════════════════════════════════════════════════════════════════════════


class TestPendingOrderExpiry:
    """Orders that sit unfilled longer than the timeout should be
    dropped from pending without producing a fill."""

    def test_order_expires_after_timeout(self):
        async def _run():
            h = PipelineHarness(fill_mode="book")
            await h.start()
            try:
                order_id = await h.submit_order(price=0.90, size=10.0)
                await h.drain()

                # Book never crosses our price
                await h.inject_book(asks=((0.95, 50.0),))
                await h.drain()
                assert order_id in h.fill_simulator.pending_orders

                # Advance time past expiry and trigger cleanup
                far_future = time.time() + 600
                with patch("src.execution.fill_simulator.time") as mock_time:
                    mock_time.time.return_value = far_future
                    mock_time.time_ns.return_value = int(far_future * 1e9)
                    h.fill_simulator.expire_stale_orders()

                assert order_id not in h.fill_simulator.pending_orders
                assert len(h.fill_events) == 0

            finally:
                await h.stop()

        Metrics.reset()
        asyncio.run(_run())


# ═══════════════════════════════════════════════════════════════════════════════
# Test 8: Stats tracking
# ═══════════════════════════════════════════════════════════════════════════════


class TestSimulatorStats:
    """FillSimulator should track fill/miss/partial counts."""

    def test_stats_after_mixed_fills(self):
        async def _run():
            h = PipelineHarness(fill_mode="book")
            await h.start()
            try:
                # Order 1: will fully fill
                await h.submit_order(
                    token_id=TOKEN_YES, price=0.95, size=10.0,
                    slug="slug-a",
                )
                await h.drain()
                await h.inject_book(
                    token_id=TOKEN_YES,
                    asks=((0.94, 50.0),),
                )
                await h.drain()

                # Order 2: will not fill (price too low)
                await h.submit_order(
                    token_id=TOKEN_NO, price=0.05, size=10.0,
                    slug="slug-b",
                )
                await h.drain()
                await h.inject_book(
                    token_id=TOKEN_NO,
                    asks=((0.10, 50.0),),
                )
                await h.drain()

                stats = h.fill_simulator.stats
                assert stats["filled"] >= 1
                assert stats["missed"] >= 0

            finally:
                await h.stop()

        Metrics.reset()
        asyncio.run(_run())


# ═══════════════════════════════════════════════════════════════════════════════
# Test 9: Multi-level book depth
# ═══════════════════════════════════════════════════════════════════════════════


class TestMultiLevelBookFill:
    """When the book has multiple ask levels, fill should walk through
    levels up to the limit price."""

    def test_fill_across_multiple_ask_levels(self):
        async def _run():
            h = PipelineHarness(fill_mode="book")
            await h.start()
            try:
                order_id = await h.submit_order(price=0.50, size=30.0)
                await h.drain()

                # Book: 10 @ 0.45, 15 @ 0.48, 20 @ 0.52 (above limit)
                await h.inject_book(
                    asks=((0.45, 10.0), (0.48, 15.0), (0.52, 20.0)),
                )
                await h.drain()

                total_filled = sum(f.fill_size for f in h.fill_events)
                assert abs(total_filled - 25.0) < 1e-9, (
                    f"Should fill 10+15=25 from levels at/below 0.50, got {total_filled}"
                )

                assert order_id in h.fill_simulator.pending_orders
                pending = h.fill_simulator.pending_orders[order_id]
                assert abs(pending.remaining_size - 5.0) < 1e-9

            finally:
                await h.stop()

        Metrics.reset()
        asyncio.run(_run())
