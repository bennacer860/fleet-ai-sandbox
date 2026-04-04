import asyncio
import time
from unittest.mock import patch

import pytest

from src.core.events import TickSizeChange
from src.core.models import Side
from src.strategy.base import StrategyContext
from src.strategy.end_market import EndMarketStrategy


@pytest.fixture
def base_ctx():
    ctx = StrategyContext()
    slug = "btc-updown-15m-1773541800"
    ctx.eval_cache[slug] = {
        "token_ids": ["token_yes", "token_no"],
        "outcomes": ["UP", "DOWN"],
        "prices": [0.10, 0.20],
        "best_idx": 1,
        "best_price": 0.20,
        "best_outcome": "DOWN",
        "best_token_id": "token_no",
    }
    return ctx


@pytest.fixture
def end_market_strategy():
    return EndMarketStrategy()


def _tick_event(slug: str) -> TickSizeChange:
    return TickSizeChange(
        condition_id="cond1",
        slug=slug,
        token_id="token_no",
        old_tick_size="0.01",
        new_tick_size="0.001",
    )


def test_end_market_waits_until_expiry(base_ctx, end_market_strategy):
    slug = "btc-updown-15m-1773541800"
    with patch("src.strategy.end_market.extract_market_end_ts", return_value=time.time() + 60):
        intents = asyncio.run(end_market_strategy.on_tick_size_change(_tick_event(slug), base_ctx))

    assert intents is None
    assert slug in end_market_strategy._watching
    assert end_market_strategy.last_skip_reason is not None
    assert "waiting for expiration" in end_market_strategy.last_skip_reason


def test_end_market_places_once_after_expiry_even_with_low_price(base_ctx, end_market_strategy):
    slug = "btc-updown-15m-1773541800"
    base_ctx.tick_sizes["token_yes"] = 0.001
    base_ctx.tick_sizes["token_no"] = 0.001

    with patch("src.strategy.end_market.extract_market_end_ts", return_value=time.time() - 1):
        intents = asyncio.run(end_market_strategy.on_tick_size_change(_tick_event(slug), base_ctx))

    assert intents is not None
    assert len(intents) == 1
    intent = intents[0]
    assert intent.side == Side.BUY
    assert intent.token_id == "token_no"
    assert intent.price == 0.999
    assert intent.size == 5.0
    assert intent.skip_dedup is False
    assert slug not in end_market_strategy._watching


def test_end_market_uses_coarse_tick_price_cap(base_ctx, end_market_strategy):
    slug = "btc-updown-15m-1773541800"
    base_ctx.tick_sizes["token_yes"] = 0.01
    base_ctx.tick_sizes["token_no"] = 0.01

    with patch("src.strategy.end_market.extract_market_end_ts", return_value=time.time() - 1):
        intents = asyncio.run(end_market_strategy.on_tick_size_change(_tick_event(slug), base_ctx))

    assert intents is not None
    assert len(intents) == 1
    assert intents[0].price == 0.99
    assert intents[0].size == 5.0
    assert intents[0].skip_dedup is False


def test_end_market_activates_hot_tokens_within_two_minutes(base_ctx, end_market_strategy):
    slug = "btc-updown-15m-1773541800"

    with patch("src.strategy.end_market.extract_market_end_ts", return_value=time.time() + 300):
        intents = asyncio.run(end_market_strategy.on_tick_size_change(_tick_event(slug), base_ctx))

    assert intents is None
    assert slug in end_market_strategy._watching
    assert slug not in end_market_strategy._hot_slugs
    assert "token_yes" not in end_market_strategy._hot_tokens
    assert "token_no" not in end_market_strategy._hot_tokens

    with patch("src.strategy.end_market.extract_market_end_ts", return_value=time.time() + 100):
        intents = asyncio.run(end_market_strategy.poll(base_ctx))

    assert intents is None
    assert slug in end_market_strategy._hot_slugs
    assert "token_yes" in end_market_strategy._hot_tokens
    assert "token_no" in end_market_strategy._hot_tokens
