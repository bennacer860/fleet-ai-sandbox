import pytest
import time
from unittest.mock import patch, MagicMock

from src.core.events import TickSizeChange, BookUpdate
from src.core.models import Side
from src.strategy.post_expiry import PostExpirySweepStrategy
from src.strategy.base import StrategyContext

@pytest.fixture
def base_ctx():
    ctx = StrategyContext()
    slug = "btc-updown-15m-1773541800"
    ctx.eval_cache[slug] = {
        "token_ids": ["token_yes", "token_no"],
        "outcomes": ["UP", "DOWN"],
        "prices": [0.001, 0.999],
        "best_idx": 1,
        "best_price": 0.999,
        "best_outcome": "DOWN",
        "best_token_id": "token_no",
        "min_order_size": 5.0,
    }
    return ctx

@pytest.fixture
def post_expiry_strategy():
    return PostExpirySweepStrategy(order_price=0.999)

def test_tick_size_change_before_expiry(base_ctx, post_expiry_strategy):
    slug = "btc-updown-15m-1773541800"
    
    # Expiry is in the future
    with patch("src.strategy.post_expiry.extract_market_end_ts", return_value=time.time() + 60):
        event = TickSizeChange(
            condition_id="cond1",
            slug=slug,
            token_id="token_no",
            old_tick_size="0.01",
            new_tick_size="0.001"
        )
        
        import asyncio
        intents = asyncio.run(post_expiry_strategy.on_tick_size_change(event, base_ctx))
        
        # It should return None because it's before expiry
        assert intents is None
        assert post_expiry_strategy.last_skip_reason is not None
        assert "waiting for expiration" in post_expiry_strategy.last_skip_reason
        # But it should be watching the market now
        assert post_expiry_strategy.last_watching is True
        assert slug in post_expiry_strategy._watching

def test_tick_size_change_after_expiry(base_ctx, post_expiry_strategy):
    slug = "btc-updown-15m-1773541800"
    
    # Expiry is in the past
    with patch("src.strategy.post_expiry.extract_market_end_ts", return_value=time.time() - 10):
        event = TickSizeChange(
            condition_id="cond1",
            slug=slug,
            token_id="token_no",
            old_tick_size="0.01",
            new_tick_size="0.001"
        )
        
        import asyncio
        intents = asyncio.run(post_expiry_strategy.on_tick_size_change(event, base_ctx))
        
        # It should place an order immediately
        assert intents is not None
        assert len(intents) == 1
        assert intents[0].side == Side.BUY
        assert intents[0].price == 0.999
        assert intents[0].token_id == "token_no"
        
        # It should stop watching after placing the order
        assert slug not in post_expiry_strategy._watching

def test_book_update_after_expiry(base_ctx, post_expiry_strategy):
    slug = "btc-updown-15m-1773541800"
    import asyncio
    
    # First, simulate tick size change before expiry to start watching
    with patch("src.strategy.post_expiry.extract_market_end_ts", return_value=time.time() + 60):
        tick_event = TickSizeChange(
            condition_id="cond1",
            slug=slug,
            token_id="token_no",
            old_tick_size="0.01",
            new_tick_size="0.001"
        )
        asyncio.run(post_expiry_strategy.on_tick_size_change(tick_event, base_ctx))
        assert slug in post_expiry_strategy._watching

    # Now simulate a book update after expiry
    with patch("src.strategy.post_expiry.extract_market_end_ts", return_value=time.time() - 10):
        book_event = BookUpdate(
            token_id="token_no",
            condition_id="cond1",
            slug=slug,
            bids=((0.999, 100),),
            asks=(),
            best_bid=0.999,
            best_ask=0.0
        )
        
        # Update best_prices in context as the bot would
        base_ctx.best_prices["token_no"] = {"bid": 0.999, "ask": 0.0}
        
        intents = asyncio.run(post_expiry_strategy.on_book_update(book_event, base_ctx))
        
        # It should place an order now
        assert intents is not None
        assert len(intents) == 1
        assert intents[0].side == Side.BUY
        assert intents[0].price == 0.999
        assert intents[0].token_id == "token_no"
        
        # It should stop watching
        assert slug not in post_expiry_strategy._watching

def test_poll_triggers_order_after_expiry(base_ctx, post_expiry_strategy):
    slug = "btc-updown-15m-1773541800"
    import asyncio

    # Start watching before expiry
    with patch("src.strategy.post_expiry.extract_market_end_ts", return_value=time.time() + 60):
        tick_event = TickSizeChange(
            condition_id="cond1",
            slug=slug,
            token_id="token_no",
            old_tick_size="0.01",
            new_tick_size="0.001"
        )
        asyncio.run(post_expiry_strategy.on_tick_size_change(tick_event, base_ctx))
        assert slug in post_expiry_strategy._watching

    # Now expiry has passed — poll should trigger order
    with patch("src.strategy.post_expiry.extract_market_end_ts", return_value=time.time() - 5):
        base_ctx.best_prices["token_no"] = {"bid": 0.999, "ask": 0.0}
        intents = asyncio.run(post_expiry_strategy.poll(base_ctx))

    assert intents is not None
    assert len(intents) == 1
    assert intents[0].side == Side.BUY
    assert intents[0].price == 0.999

def test_no_proximity_filter_on_post_expiry(base_ctx, post_expiry_strategy):
    """Post-expiry orders should submit regardless of best_price level."""
    slug = "btc-updown-15m-1773541800"
    base_ctx.eval_cache[slug]["best_price"] = 0.50
    base_ctx.eval_cache[slug]["prices"] = [0.50, 0.50]

    with patch("src.strategy.post_expiry.extract_market_end_ts", return_value=time.time() - 10):
        event = TickSizeChange(
            condition_id="cond1",
            slug=slug,
            token_id="token_yes",
            old_tick_size="0.01",
            new_tick_size="0.001"
        )

        import asyncio
        intents = asyncio.run(post_expiry_strategy.on_tick_size_change(event, base_ctx))

    assert intents is not None
    assert len(intents) == 1
    assert intents[0].side == Side.BUY
