"""End-to-end integration test for the gabagool strategy in dry-run mode.

Exercises the full async pipeline with real components wired together
the same way bot.py does it:

  BookUpdate → GabagoolStrategy → OrderIntent → OrderManager
      → OrderSubmitted → FillSimulator → OrderFill
      → PositionTracker (+ fill sync back into adapter)
      → MarketResolved → P&L

Uses real EventBus, OrderManager, FillSimulator, PositionTracker,
and AsyncPersistence with a temp SQLite database. No mocking.
"""

import asyncio
import math
import os
import tempfile

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
from src.strategy.base import StrategyContext
from src.strategy.gabagool_adapter import GabagoolConfig, GabagoolStrategy


# ═══════════════════════════════════════════════════════════════════════════════
# Test constants
# ═══════════════════════════════════════════════════════════════════════════════

TOKEN_YES = "token_yes_gabagool_e2e"
TOKEN_NO = "token_no_gabagool_e2e"
SLUG = "btc-updown-5m-gabagool-e2e"
CONDITION_ID = "cond_gabagool_e2e"


# ═══════════════════════════════════════════════════════════════════════════════
# Harness — wires all components like bot.py does
# ═══════════════════════════════════════════════════════════════════════════════


class GabagoolHarness:
    """Full pipeline harness matching bot.py wiring for the gabagool strategy."""

    def __init__(self, fill_mode: str = "instant"):
        tmp = tempfile.mkdtemp()
        db_path = os.path.join(tmp, "test_gabagool_e2e.db")
        self.conn = init_db(db_path)

        self.event_bus = EventBus()
        self.persistence = AsyncPersistence(self.conn, flush_interval=0.05)
        self.rest_client = AsyncRestClient()
        self.risk_manager = RiskManager(RiskConfig(
            max_position_per_market=10_000,
            max_total_exposure=50_000,
            max_orders_per_minute=200,
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
        self.strategy = GabagoolStrategy(
            config=GabagoolConfig(
                observation_ticks=5,
                trend_min_reversals=1,
                trend_min_amplitude=0.10,
                base_order_size=10.0,
                probe_size_factor=0.25,
                max_pair_cost=0.98,
                max_imbalance=2.0,
                fee_bps=0,
            ),
        )

        self.fill_events: list[OrderFill] = []
        self.submitted_events: list[OrderSubmitted] = []

        async def _capture_fill(event: OrderFill):
            self.fill_events.append(event)

        async def _capture_submitted(event: OrderSubmitted):
            self.submitted_events.append(event)

        # Wire subscriptions exactly like bot.py
        bus = self.event_bus
        bus.subscribe(OrderFill, self.order_manager.on_order_fill)
        bus.subscribe(OrderFill, self.position_tracker.on_fill)
        bus.subscribe(OrderFill, _capture_fill)
        bus.subscribe(OrderFill, self._on_fill_sync_strategy)
        bus.subscribe(OrderSubmitted, self.fill_simulator.on_order_submitted)
        bus.subscribe(OrderSubmitted, _capture_submitted)
        bus.subscribe(BookUpdate, self.fill_simulator.on_book_update)
        bus.subscribe(MarketResolved, self.position_tracker.on_market_resolved)

    async def _on_fill_sync_strategy(self, event: OrderFill) -> None:
        """Replicates bot.py's _on_order_fill_notify_strategy for gabagool."""
        state = self.order_manager.active_orders.get(event.order_id)
        if not state:
            return
        if hasattr(self.strategy, "on_fill_event"):
            self.strategy.on_fill_event(
                token_id=state.intent.token_id,
                fill_size=event.fill_size,
                fill_price=event.fill_price,
            )

    async def start(self):
        self._bus_task = asyncio.create_task(self.event_bus.run())
        self._persist_task = asyncio.create_task(self.persistence.drain_loop())

    async def stop(self):
        await self.event_bus.stop()
        await self.persistence.stop()
        self._bus_task.cancel()
        self._persist_task.cancel()
        await asyncio.gather(
            self._bus_task, self._persist_task, return_exceptions=True,
        )
        self.conn.close()

    def _make_ctx(
        self,
        yes_ask: float = 0.50,
        no_ask: float = 0.50,
    ) -> StrategyContext:
        return StrategyContext(
            market_meta={
                SLUG: {
                    "token_ids": (TOKEN_YES, TOKEN_NO),
                    "outcomes": ("Up", "Down"),
                    "condition_id": CONDITION_ID,
                },
            },
            best_prices={
                TOKEN_YES: {"ask": yes_ask, "bid": yes_ask - 0.01},
                TOKEN_NO: {"ask": no_ask, "bid": no_ask - 0.01},
            },
            tick_sizes={TOKEN_YES: 0.01, TOKEN_NO: 0.01},
            dry_run=True,
        )

    async def feed_book_update(self, yes_ask: float) -> list[OrderIntent] | None:
        """Feed one price tick and return any intents the strategy produces."""
        no_ask = max(0.01, min(0.99, 1.0 - yes_ask))
        ctx = self._make_ctx(yes_ask=yes_ask, no_ask=no_ask)
        event = BookUpdate(
            token_id=TOKEN_YES,
            condition_id=CONDITION_ID,
            slug=SLUG,
            bids=((yes_ask - 0.01, 100.0),),
            asks=((yes_ask, 100.0),),
            best_bid=yes_ask - 0.01,
            best_ask=yes_ask,
        )
        return await self.strategy.on_book_update(event, ctx)

    async def submit_intents(self, intents: list[OrderIntent]) -> None:
        """Submit intents through OrderManager and register with PositionTracker,
        replicating what bot.py's _submit_intents does."""
        for intent in intents:
            state = await self.order_manager.submit(intent)
            if state is not None and not state.is_terminal:
                self.position_tracker.register_order(
                    order_id=state.order_id,
                    token_id=intent.token_id,
                    slug=intent.slug,
                    strategy=intent.strategy,
                    side=intent.side.value,
                    price=intent.price,
                    size=intent.size,
                )

    async def drain(self, seconds: float = 0.15):
        await asyncio.sleep(seconds)

    def oscillating_prices(
        self, n: int, center: float = 0.50, amplitude: float = 0.25, period: int = 10,
    ) -> list[float]:
        return [
            max(0.05, min(0.95, center + amplitude * math.sin(2 * math.pi * i / period)))
            for i in range(n)
        ]


# ═══════════════════════════════════════════════════════════════════════════════
# Test 1: Full pipeline — strategy produces intents from oscillating prices
# ═══════════════════════════════════════════════════════════════════════════════


class TestGabagoolProducesOrders:
    """Oscillating BookUpdates should activate the strategy and produce orders."""

    def test_strategy_activates_and_submits(self):
        async def _run():
            h = GabagoolHarness(fill_mode="instant")
            await h.start()
            try:
                prices = h.oscillating_prices(40, center=0.50, amplitude=0.25)
                total_intents = 0

                for yes_ask in prices:
                    intents = await h.feed_book_update(yes_ask)
                    if intents:
                        await h.submit_intents(intents)
                        total_intents += len(intents)
                        await h.drain(0.1)

                assert total_intents > 0, "Strategy should produce intents from oscillating prices"
                assert len(h.submitted_events) > 0, "OrderSubmitted events should be published"
                assert all(e.dry_run for e in h.submitted_events), "All orders should be dry-run"
                assert all(e.strategy == "gabagool" for e in h.submitted_events)

            finally:
                await h.stop()

        Metrics.reset()
        asyncio.run(_run())


# ═══════════════════════════════════════════════════════════════════════════════
# Test 2: Full pipeline — instant fills flow through to PositionTracker
# ═══════════════════════════════════════════════════════════════════════════════


class TestInstantFillsReachPositionTracker:
    """In instant mode, fills should immediately create positions."""

    def test_fills_create_positions(self):
        async def _run():
            h = GabagoolHarness(fill_mode="instant")
            await h.start()
            try:
                prices = h.oscillating_prices(40, center=0.50, amplitude=0.25)

                for yes_ask in prices:
                    intents = await h.feed_book_update(yes_ask)
                    if intents:
                        await h.submit_intents(intents)
                        await h.drain(0.1)

                assert len(h.fill_events) > 0, "Should have received fill events"

                has_position = (
                    TOKEN_YES in h.position_tracker.positions
                    or TOKEN_NO in h.position_tracker.positions
                )
                assert has_position, "PositionTracker should have at least one position"

            finally:
                await h.stop()

        Metrics.reset()
        asyncio.run(_run())


# ═══════════════════════════════════════════════════════════════════════════════
# Test 3: Fill sync — adapter PairState matches PositionTracker
# ═══════════════════════════════════════════════════════════════════════════════


class TestFillSyncKeepsPairStateInSync:
    """Fills should be synced back to the adapter's PairState so the
    gabagool algorithm's guards (pair cost, balance ratio) work correctly."""

    def test_pair_state_reflects_fills(self):
        async def _run():
            h = GabagoolHarness(fill_mode="instant")
            await h.start()
            try:
                prices = h.oscillating_prices(40, center=0.50, amplitude=0.25)

                for yes_ask in prices:
                    intents = await h.feed_book_update(yes_ask)
                    if intents:
                        await h.submit_intents(intents)
                        await h.drain(0.1)

                slug_state = h.strategy.get_slug_state(SLUG)
                assert slug_state is not None

                pair = slug_state.pair
                total_fills = len(h.fill_events)
                total_qty = pair.qty_yes + pair.qty_no
                assert total_fills > 0
                assert total_qty > 0, "PairState should have accumulated shares"

                # Verify PairState quantities match what PositionTracker has
                pt_yes_qty = 0.0
                pt_no_qty = 0.0
                yes_pos = h.position_tracker.positions.get(TOKEN_YES)
                no_pos = h.position_tracker.positions.get(TOKEN_NO)
                if yes_pos:
                    pt_yes_qty = yes_pos.quantity
                if no_pos:
                    pt_no_qty = no_pos.quantity

                assert abs(pair.qty_yes - pt_yes_qty) < 1e-9, (
                    f"PairState YES qty {pair.qty_yes} != PositionTracker {pt_yes_qty}"
                )
                assert abs(pair.qty_no - pt_no_qty) < 1e-9, (
                    f"PairState NO qty {pair.qty_no} != PositionTracker {pt_no_qty}"
                )

            finally:
                await h.stop()

        Metrics.reset()
        asyncio.run(_run())


# ═══════════════════════════════════════════════════════════════════════════════
# Test 4: Full pipeline — market resolution produces correct P&L
# ═══════════════════════════════════════════════════════════════════════════════


class TestMarketResolutionPnl:
    """After fills and market resolution, P&L should be correctly calculated."""

    def test_pnl_on_yes_wins(self):
        async def _run():
            h = GabagoolHarness(fill_mode="instant")
            await h.start()
            try:
                prices = h.oscillating_prices(40, center=0.50, amplitude=0.25)

                for yes_ask in prices:
                    intents = await h.feed_book_update(yes_ask)
                    if intents:
                        await h.submit_intents(intents)
                        await h.drain(0.1)

                assert len(h.fill_events) > 0

                yes_pos = h.position_tracker.positions.get(TOKEN_YES)
                no_pos = h.position_tracker.positions.get(TOKEN_NO)
                yes_qty = yes_pos.quantity if yes_pos else 0.0
                no_qty = no_pos.quantity if no_pos else 0.0
                yes_cost = yes_pos.cost_basis if yes_pos else 0.0
                no_cost = no_pos.cost_basis if no_pos else 0.0

                # YES wins: payout = qty_yes * 1.0, NO worthless
                expected_pnl = yes_qty * 1.0 - (yes_cost + no_cost)

                resolved = MarketResolved(
                    slug=SLUG,
                    condition_id=CONDITION_ID,
                    winning_token_id=TOKEN_YES,
                )
                await h.position_tracker.on_market_resolved(resolved)

                assert abs(h.position_tracker.session_pnl - expected_pnl) < 1e-6, (
                    f"Session P&L {h.position_tracker.session_pnl:.6f} != expected {expected_pnl:.6f}"
                )
                assert h.position_tracker.trades_closed >= 1

            finally:
                await h.stop()

        Metrics.reset()
        asyncio.run(_run())

    def test_pnl_on_no_wins(self):
        async def _run():
            h = GabagoolHarness(fill_mode="instant")
            await h.start()
            try:
                prices = h.oscillating_prices(40, center=0.50, amplitude=0.25)

                for yes_ask in prices:
                    intents = await h.feed_book_update(yes_ask)
                    if intents:
                        await h.submit_intents(intents)
                        await h.drain(0.1)

                assert len(h.fill_events) > 0

                yes_pos = h.position_tracker.positions.get(TOKEN_YES)
                no_pos = h.position_tracker.positions.get(TOKEN_NO)
                yes_qty = yes_pos.quantity if yes_pos else 0.0
                no_qty = no_pos.quantity if no_pos else 0.0
                yes_cost = yes_pos.cost_basis if yes_pos else 0.0
                no_cost = no_pos.cost_basis if no_pos else 0.0

                # NO wins: payout = qty_no * 1.0, YES worthless
                expected_pnl = no_qty * 1.0 - (yes_cost + no_cost)

                resolved = MarketResolved(
                    slug=SLUG,
                    condition_id=CONDITION_ID,
                    winning_token_id=TOKEN_NO,
                )
                await h.position_tracker.on_market_resolved(resolved)

                assert abs(h.position_tracker.session_pnl - expected_pnl) < 1e-6, (
                    f"Session P&L {h.position_tracker.session_pnl:.6f} != expected {expected_pnl:.6f}"
                )

            finally:
                await h.stop()

        Metrics.reset()
        asyncio.run(_run())


# ═══════════════════════════════════════════════════════════════════════════════
# Test 5: Strategy guards — no orders when market is flat
# ═══════════════════════════════════════════════════════════════════════════════


class TestNoOrdersOnFlatMarket:
    """A flat market should not activate the strategy or produce orders."""

    def test_flat_market_no_orders(self):
        async def _run():
            h = GabagoolHarness(fill_mode="instant")
            await h.start()
            try:
                for _ in range(30):
                    intents = await h.feed_book_update(0.50)
                    assert intents is None, "Flat market should not produce intents"

                assert len(h.submitted_events) == 0
                state = h.strategy.get_slug_state(SLUG)
                assert state is not None
                assert state.activated is False

            finally:
                await h.stop()

        Metrics.reset()
        asyncio.run(_run())


# ═══════════════════════════════════════════════════════════════════════════════
# Test 6: One-sided guard — only one probe fill, then blocked
# ═══════════════════════════════════════════════════════════════════════════════


class TestOneSidedGuardBlocks:
    """After one side gets a fill, the strategy should not keep buying
    that side — it must wait for the other side to fill first."""

    def test_no_double_buy_same_side(self):
        async def _run():
            h = GabagoolHarness(fill_mode="instant")
            await h.start()
            try:
                prices = h.oscillating_prices(50, center=0.50, amplitude=0.25)

                for yes_ask in prices:
                    intents = await h.feed_book_update(yes_ask)
                    if intents:
                        await h.submit_intents(intents)
                        await h.drain(0.1)

                # After fills, check that balance ratio is reasonable
                slug_state = h.strategy.get_slug_state(SLUG)
                if slug_state and min(slug_state.pair.qty_yes, slug_state.pair.qty_no) > 0:
                    assert slug_state.pair.balance_ratio <= 2.01

            finally:
                await h.stop()

        Metrics.reset()
        asyncio.run(_run())


# ═══════════════════════════════════════════════════════════════════════════════
# Test 7: Book-mode fills — partial fills from real book depth
# ═══════════════════════════════════════════════════════════════════════════════


class TestBookModeFills:
    """In book mode, fills should respect the liquidity available in the book."""

    def test_book_mode_produces_fills(self):
        async def _run():
            h = GabagoolHarness(fill_mode="book")
            await h.start()
            try:
                prices = h.oscillating_prices(40, center=0.50, amplitude=0.25)

                for yes_ask in prices:
                    no_ask = max(0.01, min(0.99, 1.0 - yes_ask))
                    ctx = h._make_ctx(yes_ask=yes_ask, no_ask=no_ask)
                    event = BookUpdate(
                        token_id=TOKEN_YES,
                        condition_id=CONDITION_ID,
                        slug=SLUG,
                        bids=((yes_ask - 0.01, 100.0),),
                        asks=((yes_ask, 100.0),),
                        best_bid=yes_ask - 0.01,
                        best_ask=yes_ask,
                    )
                    # Publish book update to bus (for FillSimulator)
                    await h.event_bus.publish(event)
                    # Also publish NO side book for FillSimulator
                    no_event = BookUpdate(
                        token_id=TOKEN_NO,
                        condition_id=CONDITION_ID,
                        slug=SLUG,
                        bids=((no_ask - 0.01, 100.0),),
                        asks=((no_ask, 100.0),),
                        best_bid=no_ask - 0.01,
                        best_ask=no_ask,
                    )
                    await h.event_bus.publish(no_event)

                    intents = await h.strategy.on_book_update(event, ctx)
                    if intents:
                        await h.submit_intents(intents)

                    await h.drain(0.1)

                # In book mode with ample liquidity at the ask price,
                # orders should fill since our limit price matches the ask
                if len(h.submitted_events) > 0:
                    assert len(h.fill_events) > 0, (
                        f"Book mode should produce fills when liquidity is available "
                        f"({len(h.submitted_events)} orders submitted)"
                    )

            finally:
                await h.stop()

        Metrics.reset()
        asyncio.run(_run())


# ═══════════════════════════════════════════════════════════════════════════════
# Test 8: Persistence — orders and fills are written to SQLite
# ═══════════════════════════════════════════════════════════════════════════════


class TestPersistence:
    """Verify that dry-run orders are persisted to the database."""

    def test_orders_in_sqlite(self):
        async def _run():
            h = GabagoolHarness(fill_mode="instant")
            await h.start()
            try:
                prices = h.oscillating_prices(40, center=0.50, amplitude=0.25)

                for yes_ask in prices:
                    intents = await h.feed_book_update(yes_ask)
                    if intents:
                        await h.submit_intents(intents)
                        await h.drain(0.1)

                assert len(h.submitted_events) > 0

                # Wait for persistence flush
                await h.drain(0.3)

                rows = h.conn.execute(
                    "SELECT * FROM orders WHERE dry_run = 1"
                ).fetchall()
                assert len(rows) >= 1, f"Expected orders in SQLite, got {len(rows)}"

            finally:
                await h.stop()

        Metrics.reset()
        asyncio.run(_run())


# ═══════════════════════════════════════════════════════════════════════════════
# Test 9: Multi-market isolation — two slugs don't interfere
# ═══════════════════════════════════════════════════════════════════════════════

SLUG_B = "eth-updown-5m-gabagool-e2e"
TOKEN_YES_B = "token_yes_gabagool_e2e_b"
TOKEN_NO_B = "token_no_gabagool_e2e_b"
CONDITION_B = "cond_gabagool_e2e_b"


class TestMultiMarketIsolation:
    """Fills on one market should not affect the other's PairState."""

    def test_two_markets_independent(self):
        async def _run():
            h = GabagoolHarness(fill_mode="instant")
            await h.start()
            try:
                # Feed market A with oscillating prices
                prices_a = h.oscillating_prices(30, center=0.50, amplitude=0.25)
                for yes_ask in prices_a:
                    no_ask = max(0.01, min(0.99, 1.0 - yes_ask))
                    ctx = StrategyContext(
                        market_meta={
                            SLUG: {
                                "token_ids": (TOKEN_YES, TOKEN_NO),
                                "outcomes": ("Up", "Down"),
                                "condition_id": CONDITION_ID,
                            },
                            SLUG_B: {
                                "token_ids": (TOKEN_YES_B, TOKEN_NO_B),
                                "outcomes": ("Up", "Down"),
                                "condition_id": CONDITION_B,
                            },
                        },
                        best_prices={
                            TOKEN_YES: {"ask": yes_ask, "bid": yes_ask - 0.01},
                            TOKEN_NO: {"ask": no_ask, "bid": no_ask - 0.01},
                            TOKEN_YES_B: {"ask": 0.50, "bid": 0.49},
                            TOKEN_NO_B: {"ask": 0.50, "bid": 0.49},
                        },
                        tick_sizes={
                            TOKEN_YES: 0.01, TOKEN_NO: 0.01,
                            TOKEN_YES_B: 0.01, TOKEN_NO_B: 0.01,
                        },
                        dry_run=True,
                    )

                    event_a = BookUpdate(
                        token_id=TOKEN_YES,
                        condition_id=CONDITION_ID,
                        slug=SLUG,
                        bids=((yes_ask - 0.01, 100.0),),
                        asks=((yes_ask, 100.0),),
                        best_bid=yes_ask - 0.01,
                        best_ask=yes_ask,
                    )
                    intents = await h.strategy.on_book_update(event_a, ctx)
                    if intents:
                        await h.submit_intents(intents)
                        await h.drain(0.1)

                    # Market B gets flat prices — should NOT activate
                    event_b = BookUpdate(
                        token_id=TOKEN_YES_B,
                        condition_id=CONDITION_B,
                        slug=SLUG_B,
                        bids=((0.49, 100.0),),
                        asks=((0.50, 100.0),),
                        best_bid=0.49,
                        best_ask=0.50,
                    )
                    intents_b = await h.strategy.on_book_update(event_b, ctx)
                    assert intents_b is None, "Flat market B should not produce intents"

                state_a = h.strategy.get_slug_state(SLUG)
                state_b = h.strategy.get_slug_state(SLUG_B)

                assert state_b is not None
                assert state_b.pair.qty_yes == 0.0
                assert state_b.pair.qty_no == 0.0
                assert state_b.activated is False

                if state_a and state_a.activated:
                    assert state_a.pair.qty_yes > 0 or state_a.pair.qty_no > 0

            finally:
                await h.stop()

        Metrics.reset()
        asyncio.run(_run())
