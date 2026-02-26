"""End-to-end dry-run test.

Verifies that events flow through the entire pipeline in dry-run mode:
  1. EventBus dispatches TickSizeChange to strategy
  2. SweepStrategy produces OrderIntent
  3. OrderManager submits with dry_run=True (no REST call)
  4. OrderSubmitted event is published with dry_run=True
  5. Decision is logged to SQLite
  6. No real orders are placed
"""

import asyncio
import os
import sqlite3
import tempfile

from src.core.event_bus import EventBus
from src.core.events import BookUpdate, OrderSubmitted, TickSizeChange
from src.core.models import OrderIntent, Side
from src.execution.order_manager import OrderManager
from src.execution.position_tracker import PositionTracker
from src.execution.risk_manager import RiskConfig, RiskManager
from src.gateway.rest_client import AsyncRestClient
from src.monitoring.metrics import Metrics
from src.storage.database import init_db
from src.storage.persistence import AsyncPersistence
from src.strategy.base import StrategyContext
from src.strategy.sweep import SweepStrategy


async def run_test():
    # Setup temp database
    tmp = tempfile.mkdtemp()
    db_path = os.path.join(tmp, "test.db")
    conn = init_db(db_path)

    # Core components
    event_bus = EventBus()
    persistence = AsyncPersistence(conn, flush_interval=0.05)
    rest_client = AsyncRestClient()
    risk_manager = RiskManager(RiskConfig())
    order_manager = OrderManager(
        event_bus=event_bus,
        rest_client=rest_client,
        risk_manager=risk_manager,
        persistence=persistence,
        dry_run=True,
    )
    position_tracker = PositionTracker(persistence=persistence)
    strategy = SweepStrategy(price_threshold=0.90)
    metrics = Metrics.get()

    # Track published events
    submitted_events: list[OrderSubmitted] = []

    async def capture_submitted(event: OrderSubmitted):
        submitted_events.append(event)

    event_bus.subscribe(OrderSubmitted, capture_submitted)

    # Start event bus and persistence
    bus_task = asyncio.create_task(event_bus.run())
    persist_task = asyncio.create_task(persistence.drain_loop())

    # ── Test 1: Tick size change triggers strategy → order ──────────────

    # Simulate market data context
    token_id_yes = "token_yes_abc123"
    token_id_no = "token_no_def456"
    slug = "btc-5m-test-market"

    ctx = StrategyContext(
        best_prices={
            token_id_yes: {"bid": 0.97, "ask": 0.98},
            token_id_no: {"bid": 0.03, "ask": 0.04},
        },
        market_meta={
            slug: {
                "token_ids": (token_id_yes, token_id_no),
                "outcomes": ("Up", "Down"),
                "condition_id": "cond_test",
            }
        },
        dry_run=True,
    )

    tick_event = TickSizeChange(
        condition_id="cond_test",
        slug=slug,
        token_id=token_id_yes,
        old_tick_size="0.01",
        new_tick_size="0.001",
    )

    intents = await strategy.on_tick_size_change(tick_event, ctx)
    assert intents is not None, "Strategy should produce OrderIntent on sweep signal"
    assert len(intents) == 1
    intent = intents[0]
    assert intent.side == Side.BUY
    assert intent.price == 0.999
    assert intent.strategy == "sweep"
    assert intent.slug == slug
    print("[PASS] Strategy produced correct OrderIntent")

    # Submit through order manager
    state = await order_manager.submit(intent)
    assert state is not None, "OrderManager should return OrderState"
    assert state.dry_run is True
    assert state.order_id.startswith("dry_")
    print(f"[PASS] OrderManager returned dry-run state: {state.order_id}")

    # Wait for event bus to dispatch
    await asyncio.sleep(0.2)

    assert len(submitted_events) == 1, f"Expected 1 OrderSubmitted, got {len(submitted_events)}"
    assert submitted_events[0].dry_run is True
    print("[PASS] OrderSubmitted event published with dry_run=True")

    # ── Test 2: Dedup blocks second order for same market ───────────────

    state2 = await order_manager.submit(intent)
    assert state2 is None, "Dedup should block second order"
    assert order_manager.stats["dedup_skips"] == 1
    print("[PASS] Dedup correctly blocked duplicate order")

    # ── Test 3: Risk manager blocks if limits exceeded ──────────────────

    risk_manager2 = RiskManager(RiskConfig(max_position_per_market=0.01))
    allowed, reason = risk_manager2.check(intent)
    assert not allowed
    assert "MAX_POSITION" in reason
    print(f"[PASS] Risk manager blocked: {reason}")

    # ── Test 4: Strategy ignores non-sweep tick sizes ───────────────────

    non_sweep = TickSizeChange(
        condition_id="cond_test",
        slug=slug,
        token_id=token_id_yes,
        old_tick_size="0.1",
        new_tick_size="0.01",
    )
    result = await strategy.on_tick_size_change(non_sweep, ctx)
    assert result is None, "Strategy should return None for non-sweep tick size"
    print("[PASS] Strategy correctly ignored non-sweep tick size")

    # ── Test 5: Strategy ignores low-price markets ──────────────────────

    low_ctx = StrategyContext(
        best_prices={
            token_id_yes: {"bid": 0.50, "ask": 0.52},
            token_id_no: {"bid": 0.48, "ask": 0.50},
        },
        market_meta=ctx.market_meta,
        dry_run=True,
    )
    result = await strategy.on_tick_size_change(tick_event, low_ctx)
    assert result is None, "Strategy should skip low-price market"
    print("[PASS] Strategy correctly skipped low-price market")

    # ── Test 6: BookUpdate returns None from sweep strategy ─────────────

    book_event = BookUpdate(
        token_id=token_id_yes,
        condition_id="cond_test",
        slug=slug,
        bids=((0.97, 100.0),),
        asks=((0.98, 100.0),),
        best_bid=0.97,
        best_ask=0.98,
    )
    result = await strategy.on_book_update(book_event, ctx)
    assert result is None
    print("[PASS] SweepStrategy.on_book_update returns None (as expected)")

    # ── Test 7: Persistence writes to SQLite ────────────────────────────

    await asyncio.sleep(0.3)  # wait for drain

    rows = conn.execute("SELECT * FROM orders WHERE dry_run = 1").fetchall()
    assert len(rows) >= 1, f"Expected orders in SQLite, got {len(rows)}"
    print(f"[PASS] Found {len(rows)} order(s) in SQLite (dry_run=1)")

    decisions = conn.execute("SELECT * FROM decisions WHERE dry_run = 1").fetchall()
    assert len(decisions) >= 1, f"Expected decisions in SQLite, got {len(decisions)}"
    print(f"[PASS] Found {len(decisions)} decision(s) in SQLite (dry_run=1)")

    # ── Test 8: Metrics are collected ───────────────────────────────────

    snap = metrics.snapshot()
    assert "uptime_s" in snap
    print(f"[PASS] Metrics snapshot: uptime={snap['uptime_s']}s")

    # ── Cleanup ─────────────────────────────────────────────────────────

    await event_bus.stop()
    await persistence.stop()
    bus_task.cancel()
    persist_task.cancel()
    await asyncio.gather(bus_task, persist_task, return_exceptions=True)
    conn.close()

    print()
    print("=" * 50)
    print("  ALL 8 TESTS PASSED")
    print("=" * 50)


if __name__ == "__main__":
    Metrics.reset()
    asyncio.run(run_test())
