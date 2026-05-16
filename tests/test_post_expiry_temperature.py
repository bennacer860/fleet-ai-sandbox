import asyncio
import time

from src.core.events import TickSizeChange
from src.core.models import Side
from src.strategy.base import StrategyContext
from src.strategy.post_expiry_temperature import PostExpiryTemperatureStrategy


def _build_ctx(slug: str, safe_expiry_ts: float) -> StrategyContext:
    ctx = StrategyContext()
    ctx.eval_cache[slug] = {
        "token_ids": ("token_yes", "token_no"),
        "outcomes": ("YES", "NO"),
        "prices": [0.05, 0.95],
        "best_idx": 1,
        "best_price": 0.95,
        "best_outcome": "NO",
        "best_token_id": "token_no",
        "min_order_size": 5.0,
    }
    ctx.market_meta[slug] = {
        "token_ids": ("token_yes", "token_no"),
        "outcomes": ("YES", "NO"),
        "market_family": "city_temperature",
        "safe_expiry_ts": safe_expiry_ts,
    }
    ctx.best_prices["token_yes"] = {"bid": 0.05, "ask": 0.06}
    ctx.best_prices["token_no"] = {"bid": 0.95, "ask": 0.96}
    ctx.tick_sizes["token_yes"] = 0.001
    ctx.tick_sizes["token_no"] = 0.001
    return ctx


def test_waits_before_safe_expiry() -> None:
    slug = "highest-temperature-in-nyc-on-may-16-2026"
    ctx = _build_ctx(slug, time.time() + 60)
    strategy = PostExpiryTemperatureStrategy()
    event = TickSizeChange(
        condition_id="c1",
        slug=slug,
        token_id="token_no",
        old_tick_size="0.01",
        new_tick_size="0.001",
    )

    intents = asyncio.run(strategy.on_tick_size_change(event, ctx))
    assert intents is None
    assert strategy.last_skip_reason is not None
    assert "safe expiry" in strategy.last_skip_reason


def test_orders_after_safe_expiry() -> None:
    slug = "lowest-temperature-in-paris-on-may-16-2026"
    ctx = _build_ctx(slug, time.time() - 10)
    strategy = PostExpiryTemperatureStrategy()
    event = TickSizeChange(
        condition_id="c1",
        slug=slug,
        token_id="token_no",
        old_tick_size="0.01",
        new_tick_size="0.001",
    )

    intents = asyncio.run(strategy.on_tick_size_change(event, ctx))
    assert intents is not None
    assert len(intents) == 1
    assert intents[0].strategy == "post_expiry_temperature"
    assert intents[0].side == Side.BUY
    assert intents[0].token_id == "token_no"


def test_skips_non_city_temperature_market_family() -> None:
    slug = "btc-updown-15m-1773541800"
    ctx = _build_ctx(slug, time.time() - 10)
    ctx.market_meta[slug]["market_family"] = "general"
    strategy = PostExpiryTemperatureStrategy()
    event = TickSizeChange(
        condition_id="c1",
        slug=slug,
        token_id="token_no",
        old_tick_size="0.01",
        new_tick_size="0.001",
    )

    intents = asyncio.run(strategy.on_tick_size_change(event, ctx))
    assert intents is None
    assert strategy.last_skip_reason == "not a city temperature market"
