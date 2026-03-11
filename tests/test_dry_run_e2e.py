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
import time as _time
from unittest.mock import patch

from src.core.event_bus import EventBus
from src.core.events import BookUpdate, MarketResolved, OrderSubmitted, TickSizeChange
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

    # ── Test 5: Low-price market enters watchlist ───────────────────────

    watch_slug = "btc-5m-watch-market"
    watch_strategy = SweepStrategy(price_threshold=0.99)

    low_ctx = StrategyContext(
        best_prices={
            token_id_yes: {"bid": 0.95, "ask": 0.96},
            token_id_no: {"bid": 0.05, "ask": 0.06},
        },
        market_meta={
            watch_slug: {
                "token_ids": (token_id_yes, token_id_no),
                "outcomes": ("Up", "Down"),
                "condition_id": "cond_watch",
            }
        },
        dry_run=True,
    )

    watch_tick = TickSizeChange(
        condition_id="cond_watch",
        slug=watch_slug,
        token_id=token_id_yes,
        old_tick_size="0.01",
        new_tick_size="0.001",
    )
    result = await watch_strategy.on_tick_size_change(watch_tick, low_ctx)
    assert result is None, "Strategy should not order when bid < threshold"
    assert watch_slug in watch_strategy._watching, "Market should be in watchlist"
    print("[PASS] Low-price market added to watchlist")

    # ── Test 6: BookUpdate below threshold keeps watching ──────────────

    book_low = BookUpdate(
        token_id=token_id_yes,
        condition_id="cond_watch",
        slug=watch_slug,
        bids=((0.96, 100.0),),
        asks=((0.97, 100.0),),
        best_bid=0.96,
        best_ask=0.97,
    )
    low_book_ctx = StrategyContext(
        best_prices={
            token_id_yes: {"bid": 0.96, "ask": 0.97},
            token_id_no: {"bid": 0.04, "ask": 0.05},
        },
        market_meta=low_ctx.market_meta,
        dry_run=True,
    )
    result = await watch_strategy.on_book_update(book_low, low_book_ctx)
    assert result is None, "Should keep watching when bid < threshold"
    assert watch_slug in watch_strategy._watching, "Market should still be in watchlist"
    print("[PASS] BookUpdate below threshold keeps watching")

    # ── Test 6b: BookUpdate at/above threshold triggers order ──────────

    high_book_ctx = StrategyContext(
        best_prices={
            token_id_yes: {"bid": 0.993, "ask": 0.995},
            token_id_no: {"bid": 0.005, "ask": 0.007},
        },
        market_meta=low_ctx.market_meta,
        dry_run=True,
    )
    book_high = BookUpdate(
        token_id=token_id_yes,
        condition_id="cond_watch",
        slug=watch_slug,
        bids=((0.993, 100.0),),
        asks=((0.995, 100.0),),
        best_bid=0.993,
        best_ask=0.995,
    )
    result = await watch_strategy.on_book_update(book_high, high_book_ctx)
    assert result is not None, "Should place order when bid >= threshold"
    assert len(result) == 1
    assert result[0].price == 0.999
    assert result[0].side == Side.BUY
    assert watch_slug not in watch_strategy._watching, "Market should be removed from watchlist"
    print("[PASS] BookUpdate at threshold triggers order and removes from watchlist")

    # ── Test 6c: BookUpdate for non-watched market is a no-op ──────────

    book_unwatched = BookUpdate(
        token_id=token_id_yes,
        condition_id="cond_test",
        slug="some-other-market",
        bids=((0.99, 100.0),),
        asks=((0.995, 100.0),),
        best_bid=0.99,
        best_ask=0.995,
    )
    result = await watch_strategy.on_book_update(book_unwatched, high_book_ctx)
    assert result is None
    print("[PASS] BookUpdate for non-watched market returns None")

    # ── Test 6d: MarketResolved cleans up watchlist ────────────────────

    cleanup_slug = "btc-5m-cleanup-market"
    watch_strategy._watching[cleanup_slug] = {"dummy": True}
    resolved = MarketResolved(
        slug=cleanup_slug,
        condition_id="cond_cleanup",
        winning_token_id=token_id_yes,
    )
    await watch_strategy.on_market_resolved(resolved, low_ctx)
    assert cleanup_slug not in watch_strategy._watching, "Resolved market should be removed"
    print("[PASS] MarketResolved cleans up watchlist")

    # ── Test 6e: TTE too early → market enters watchlist with tte_early flag

    tte_strategy = SweepStrategy(price_threshold=0.99, early_tick_threshold=0.995)
    future_start = int(_time.time()) + 200
    tte_slug = f"btc-updown-5m-{future_start}"

    tte_ctx = StrategyContext(
        best_prices={
            token_id_yes: {"bid": 0.993, "ask": 0.995},
            token_id_no: {"bid": 0.005, "ask": 0.007},
        },
        market_meta={
            tte_slug: {
                "token_ids": (token_id_yes, token_id_no),
                "outcomes": ("Up", "Down"),
                "condition_id": "cond_tte",
            }
        },
        dry_run=True,
    )

    tte_tick = TickSizeChange(
        condition_id="cond_tte",
        slug=tte_slug,
        token_id=token_id_yes,
        old_tick_size="0.01",
        new_tick_size="0.001",
    )
    result = await tte_strategy.on_tick_size_change(tte_tick, tte_ctx)
    assert result is None, "Should not order when TTE is too early"
    assert tte_slug in tte_strategy._watching, "TTE-rejected market should be in watchlist"
    assert tte_strategy._watching[tte_slug].get("tte_early") is True, "Should be flagged as tte_early"
    assert tte_strategy.last_watching is True, "last_watching should be True"
    print("[PASS] TTE too early: market added to watchlist with tte_early flag")

    # ── Test 6f: BookUpdate keeps watching while TTE still too early ──

    tte_book_high = BookUpdate(
        token_id=token_id_yes,
        condition_id="cond_tte",
        slug=tte_slug,
        bids=((0.996, 100.0),),
        asks=((0.998, 100.0),),
        best_bid=0.996,
        best_ask=0.998,
    )
    tte_ctx_high = StrategyContext(
        best_prices={
            token_id_yes: {"bid": 0.996, "ask": 0.998},
            token_id_no: {"bid": 0.002, "ask": 0.004},
        },
        market_meta=tte_ctx.market_meta,
        dry_run=True,
    )
    result = await tte_strategy.on_book_update(tte_book_high, tte_ctx_high)
    assert result is None, "Should not order while TTE still too early"
    assert tte_slug in tte_strategy._watching, "Market should remain in watchlist"
    print("[PASS] BookUpdate keeps watching while TTE still too early")

    # ── Test 6g: Early-tick market rejects price between normal and stricter threshold

    tte_ctx_mid = StrategyContext(
        best_prices={
            token_id_yes: {"bid": 0.993, "ask": 0.995},
            token_id_no: {"bid": 0.005, "ask": 0.007},
        },
        market_meta=tte_ctx.market_meta,
        dry_run=True,
    )
    tte_book_mid = BookUpdate(
        token_id=token_id_yes,
        condition_id="cond_tte",
        slug=tte_slug,
        bids=((0.993, 100.0),),
        asks=((0.995, 100.0),),
        best_bid=0.993,
        best_ask=0.995,
    )
    end_ts = future_start + 300
    mock_time_val = end_ts - 10.0
    with patch("src.strategy.sweep.time") as mock_time_mod:
        mock_time_mod.time.return_value = mock_time_val
        result = await tte_strategy.on_book_update(tte_book_mid, tte_ctx_mid)
    assert result is None, "0.993 is above normal threshold 0.99 but below early_tick 0.995 — should skip"
    assert tte_slug in tte_strategy._watching, "Market should remain in watchlist"
    print("[PASS] Early-tick market rejects price between normal and stricter threshold")

    # ── Test 6h: BookUpdate places order once price meets stricter threshold and TTE in window

    with patch("src.strategy.sweep.time") as mock_time_mod:
        mock_time_mod.time.return_value = mock_time_val
        result = await tte_strategy.on_book_update(tte_book_high, tte_ctx_high)
    assert result is not None, "Should place order: price 0.996 >= 0.995 and TTE in window"
    assert len(result) == 1
    assert result[0].price == 0.999
    assert result[0].side == Side.BUY
    assert tte_slug not in tte_strategy._watching, "Market should be removed from watchlist"
    print("[PASS] Early-tick market places order once price meets stricter threshold and TTE in window")

    # ── Test 6i: Normal watchlist market uses standard threshold (not stricter)

    normal_watch_strategy = SweepStrategy(price_threshold=0.99, early_tick_threshold=0.995)
    normal_watch_slug = "btc-5m-normal-watch"
    normal_watch_ctx = StrategyContext(
        best_prices={
            token_id_yes: {"bid": 0.95, "ask": 0.96},
            token_id_no: {"bid": 0.05, "ask": 0.06},
        },
        market_meta={
            normal_watch_slug: {
                "token_ids": (token_id_yes, token_id_no),
                "outcomes": ("Up", "Down"),
                "condition_id": "cond_normal",
            }
        },
        dry_run=True,
    )
    normal_tick = TickSizeChange(
        condition_id="cond_normal",
        slug=normal_watch_slug,
        token_id=token_id_yes,
        old_tick_size="0.01",
        new_tick_size="0.001",
    )
    result = await normal_watch_strategy.on_tick_size_change(normal_tick, normal_watch_ctx)
    assert result is None
    assert normal_watch_slug in normal_watch_strategy._watching
    assert normal_watch_strategy._watching[normal_watch_slug].get("tte_early") is not True

    normal_book_ctx = StrategyContext(
        best_prices={
            token_id_yes: {"bid": 0.993, "ask": 0.995},
            token_id_no: {"bid": 0.005, "ask": 0.007},
        },
        market_meta=normal_watch_ctx.market_meta,
        dry_run=True,
    )
    normal_book = BookUpdate(
        token_id=token_id_yes,
        condition_id="cond_normal",
        slug=normal_watch_slug,
        bids=((0.993, 100.0),),
        asks=((0.995, 100.0),),
        best_bid=0.993,
        best_ask=0.995,
    )
    result = await normal_watch_strategy.on_book_update(normal_book, normal_book_ctx)
    assert result is not None, "Normal watchlist: 0.993 >= 0.99 should place order (no tte_early)"
    assert normal_watch_slug not in normal_watch_strategy._watching
    print("[PASS] Normal watchlist market uses standard threshold (0.993 >= 0.99 triggers order)")

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
    print("  ALL 18 TESTS PASSED")
    print("=" * 50)


if __name__ == "__main__":
    Metrics.reset()
    asyncio.run(run_test())
