import pytest
import asyncio
import time
from unittest.mock import patch

from src.core.events import TickSizeChange, BookUpdate, MarketResolved
from src.core.models import Side
from src.strategy.aggressive_post_expiry import AggressivePostExpirySweepStrategy
from src.strategy.base import StrategyContext


@pytest.fixture
def ctx():
    c = StrategyContext()
    slug = "btc-updown-15m-1773541800"
    c.eval_cache[slug] = {
        "token_ids": ("token_yes", "token_no"),
        "outcomes": ("UP", "DOWN"),
        "prices": [0.001, 0.95],
        "best_idx": 1,
        "best_price": 0.95,
        "best_outcome": "DOWN",
        "best_token_id": "token_no",
        "min_order_size": 5.0,
    }
    return c


@pytest.fixture
def strategy():
    return AggressivePostExpirySweepStrategy()


# ── Phase 1: before expiry, should watch but not order ────────────────


def test_before_expiry_watches_but_no_order(ctx, strategy):
    slug = "btc-updown-15m-1773541800"
    with patch(
        "src.strategy.aggressive_post_expiry.extract_market_end_ts",
        return_value=time.time() + 60,
    ):
        event = TickSizeChange(
            condition_id="c1", slug=slug, token_id="token_no",
            old_tick_size="0.01", new_tick_size="0.01",
        )
        intents = asyncio.run(strategy.on_tick_size_change(event, ctx))

    assert intents is None
    assert strategy.last_watching is True
    assert slug in strategy._markets


# ── Phase 1: after expiry with tick_size 0.01 → order at 0.99 ────────


def test_phase1_order_after_expiry(ctx, strategy):
    slug = "btc-updown-15m-1773541800"
    with patch(
        "src.strategy.aggressive_post_expiry.extract_market_end_ts",
        return_value=time.time() - 5,
    ):
        event = TickSizeChange(
            condition_id="c1", slug=slug, token_id="token_no",
            old_tick_size="0.01", new_tick_size="0.01",
        )
        intents = asyncio.run(strategy.on_tick_size_change(event, ctx))

    assert intents is not None
    assert len(intents) == 1
    assert intents[0].side == Side.BUY
    assert intents[0].price == 0.99
    assert intents[0].tick_size == 0.01
    assert intents[0].skip_dedup is True
    assert intents[0].strategy == "aggressive_post_expiry"


# ── Phase 2: tick changes to 0.001 → escalate to 0.999 ──────────────


def test_phase2_escalation_on_tick_change(ctx, strategy):
    slug = "btc-updown-15m-1773541800"

    # First, trigger phase 1
    with patch(
        "src.strategy.aggressive_post_expiry.extract_market_end_ts",
        return_value=time.time() - 5,
    ):
        event1 = TickSizeChange(
            condition_id="c1", slug=slug, token_id="token_no",
            old_tick_size="0.01", new_tick_size="0.01",
        )
        asyncio.run(strategy.on_tick_size_change(event1, ctx))

    # Simulate rejection so has_live_order clears
    strategy.notify_order_result(slug, filled=False)

    # Now tick changes to 0.001
    ctx.tick_sizes["token_no"] = 0.001
    with patch(
        "src.strategy.aggressive_post_expiry.extract_market_end_ts",
        return_value=time.time() - 5,
    ):
        event2 = TickSizeChange(
            condition_id="c1", slug=slug, token_id="token_no",
            old_tick_size="0.01", new_tick_size="0.001",
        )
        intents = asyncio.run(strategy.on_tick_size_change(event2, ctx))

    assert intents is not None
    assert len(intents) == 1
    assert intents[0].price == 0.999
    assert intents[0].tick_size == 0.001


# ── Poll retries on rejection ────────────────────────────────────────


def test_poll_retries_after_rejection(ctx, strategy):
    slug = "btc-updown-15m-1773541800"

    # Set up the market state after expiry
    with patch(
        "src.strategy.aggressive_post_expiry.extract_market_end_ts",
        return_value=time.time() - 5,
    ):
        event = TickSizeChange(
            condition_id="c1", slug=slug, token_id="token_no",
            old_tick_size="0.01", new_tick_size="0.01",
        )
        intents1 = asyncio.run(strategy.on_tick_size_change(event, ctx))

    assert intents1 is not None

    # Simulate rejection
    strategy.notify_order_result(slug, filled=False)

    # Force past the poll interval
    strategy._markets[slug].last_attempt_time = time.time() - 10

    intents2 = asyncio.run(strategy.poll(ctx))

    assert intents2 is not None
    assert len(intents2) == 1
    assert intents2[0].price == 0.99  # still phase 1


# ── Poll stops after fill ────────────────────────────────────────────


def test_poll_stops_after_fill(ctx, strategy):
    slug = "btc-updown-15m-1773541800"

    with patch(
        "src.strategy.aggressive_post_expiry.extract_market_end_ts",
        return_value=time.time() - 5,
    ):
        event = TickSizeChange(
            condition_id="c1", slug=slug, token_id="token_no",
            old_tick_size="0.01", new_tick_size="0.01",
        )
        asyncio.run(strategy.on_tick_size_change(event, ctx))

    # Simulate fill
    strategy.notify_order_result(slug, filled=True)

    strategy._markets[slug].last_attempt_time = time.time() - 10

    intents = asyncio.run(strategy.poll(ctx))

    assert intents is None


# ── Max retries exhausted ────────────────────────────────────────────


def test_max_retries_exhausted(ctx, strategy):
    slug = "btc-updown-15m-1773541800"

    with patch(
        "src.strategy.aggressive_post_expiry.extract_market_end_ts",
        return_value=time.time() - 5,
    ), patch(
        "src.strategy.aggressive_post_expiry.AGGRESSIVE_MAX_RETRIES", 2,
    ):
        event = TickSizeChange(
            condition_id="c1", slug=slug, token_id="token_no",
            old_tick_size="0.01", new_tick_size="0.01",
        )
        # Attempt 1
        intents1 = asyncio.run(strategy.on_tick_size_change(event, ctx))
        assert intents1 is not None

        strategy.notify_order_result(slug, filled=False)
        strategy._markets[slug].last_attempt_time = time.time() - 10

        # Attempt 2
        intents2 = asyncio.run(strategy.poll(ctx))
        assert intents2 is not None

        strategy.notify_order_result(slug, filled=False)
        strategy._markets[slug].last_attempt_time = time.time() - 10

        # Attempt 3 — should be blocked
        intents3 = asyncio.run(strategy.poll(ctx))
        assert intents3 is None


# ── Market resolved cleans up ────────────────────────────────────────


def test_market_resolved_cleanup(ctx, strategy):
    slug = "btc-updown-15m-1773541800"

    with patch(
        "src.strategy.aggressive_post_expiry.extract_market_end_ts",
        return_value=time.time() + 60,
    ):
        event = TickSizeChange(
            condition_id="c1", slug=slug, token_id="token_no",
            old_tick_size="0.01", new_tick_size="0.01",
        )
        asyncio.run(strategy.on_tick_size_change(event, ctx))
    assert slug in strategy._markets

    resolved = MarketResolved(
        slug=slug, condition_id="c1", winning_token_id="token_no",
    )
    asyncio.run(strategy.on_market_resolved(resolved, ctx))

    assert slug not in strategy._markets


# ── Low best price still places order (no proximity/distance filter) ──


def test_low_best_price_still_orders(ctx, strategy):
    slug = "btc-updown-15m-1773541800"
    ctx.eval_cache[slug]["best_price"] = 0.96
    ctx.eval_cache[slug]["prices"] = [0.04, 0.96]

    with patch(
        "src.strategy.aggressive_post_expiry.extract_market_end_ts",
        return_value=time.time() - 5,
    ):
        event = TickSizeChange(
            condition_id="c1", slug=slug, token_id="token_yes",
            old_tick_size="0.01", new_tick_size="0.01",
        )
        intents = asyncio.run(strategy.on_tick_size_change(event, ctx))

    assert intents is not None
    assert len(intents) == 1
    assert intents[0].side == Side.BUY
    assert intents[0].skip_dedup is True


# ── Book update after expiry triggers order ──────────────────────────


def test_book_update_after_expiry(ctx, strategy):
    slug = "btc-updown-15m-1773541800"

    # Register market before expiry
    with patch(
        "src.strategy.aggressive_post_expiry.extract_market_end_ts",
        return_value=time.time() + 60,
    ):
        event = TickSizeChange(
            condition_id="c1", slug=slug, token_id="token_no",
            old_tick_size="0.01", new_tick_size="0.01",
        )
        asyncio.run(strategy.on_tick_size_change(event, ctx))

    # Now expiry has passed
    strategy._markets[slug].end_ts = time.time() - 5
    ctx.best_prices["token_no"] = {"bid": 0.95, "ask": 0.0}

    book_event = BookUpdate(
        token_id="token_no", condition_id="c1", slug=slug,
        bids=((0.95, 100),), asks=(), best_bid=0.95, best_ask=0.0,
    )
    intents = asyncio.run(strategy.on_book_update(book_event, ctx))

    assert intents is not None
    assert intents[0].price == 0.99
    assert intents[0].skip_dedup is True
