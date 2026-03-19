import pytest
import time
from unittest.mock import patch, MagicMock

from src.core.events import TickSizeChange
from src.core.models import Side
from src.strategy.sweep import SweepStrategy
from src.strategy.base import StrategyContext

@pytest.fixture
def base_ctx():
    ctx = StrategyContext()
    # Mocking standard data
    slug = "xrp-updown-15m-1773541800"
    ctx.eval_cache[slug] = {
        "token_ids": ["token_yes", "token_no"],
        "outcomes": ["UP", "DOWN"],
        "prices": [0.001, 0.999],
        "best_idx": 1,
        "best_price": 0.999,
        "best_outcome": "DOWN",
        "best_token_id": "token_no",
        "min_order_size": 5.0,
        "price_to_beat": 1.4156  # Custom field we inject for the mock
    }
    return ctx

@pytest.fixture
def sweep_strategy():
    return SweepStrategy(price_threshold=0.99, order_price=0.999)

def test_proximity_filter_blocks_trade(base_ctx, sweep_strategy):
    slug = "xrp-updown-15m-1773541800"
    
    # 0.1% = 0.001
    with patch("src.strategy.sweep.PROXIMITY_FILTER_ENABLED", True), \
         patch("src.strategy.sweep.PROXIMITY_MIN_DISTANCE", 0.001), \
         patch("src.strategy.sweep.fetch_strike_price", return_value=1.4156), \
         patch("src.strategy.sweep.extract_market_end_ts", return_value=time.time() + 60):  # 60s to expiry
        
        # We need to simulate spot price
        # Prox = |1.4150 - 1.4156| / 1.4156 = 0.0006 / 1.4156 = 0.00042 (0.042%, which is < 0.1%)
        base_ctx.crypto_prices["XRP"] = 1.4150
        base_ctx.crypto_price_ts["XRP"] = time.monotonic()
        
        intent = sweep_strategy._build_order(slug, base_ctx.eval_cache[slug], ctx=base_ctx)
        
        # Because proxy threshold is 0.1% (0.001), and our distance is 0.042% (0.00042),
        # the trade should be BLOCKED (returns None)
        assert intent is None
        assert sweep_strategy.last_skip_reason is not None
        assert "proximity" in sweep_strategy.last_skip_reason
        assert "0.1000%" in sweep_strategy.last_skip_reason

def test_proximity_filter_allows_trade(base_ctx, sweep_strategy):
    slug = "xrp-updown-15m-1773541800"
    
    with patch("src.strategy.sweep.PROXIMITY_FILTER_ENABLED", True), \
         patch("src.strategy.sweep.PROXIMITY_MIN_DISTANCE", 0.001), \
         patch("src.strategy.sweep.fetch_strike_price", return_value=1.4156), \
         patch("src.strategy.sweep.extract_market_end_ts", return_value=time.time() + 60):  # 60s to expiry
        
        # Prox = |1.4139 - 1.4156| / 1.4156 = 0.0017 / 1.4156 = 0.00120 (0.12%, which is > 0.1%)
        base_ctx.crypto_prices["XRP"] = 1.4139
        base_ctx.crypto_price_ts["XRP"] = time.monotonic()
        
        intent = sweep_strategy._build_order(slug, base_ctx.eval_cache[slug], ctx=base_ctx)
        
        # Because distance is 0.12%, which is > 0.1%, it should ALLOW the trade (returns OrderIntent)
        assert intent is not None
        assert len(intent) == 1
        assert intent[0].side == Side.BUY
        assert intent[0].price == 0.999

def test_proximity_filter_disabled(base_ctx, sweep_strategy):
    slug = "xrp-updown-15m-1773541800"
    
    with patch("src.strategy.sweep.PROXIMITY_FILTER_ENABLED", False), \
         patch("src.strategy.sweep.PROXIMITY_MIN_DISTANCE", 0.001), \
         patch("src.strategy.sweep.fetch_strike_price", return_value=1.4156), \
         patch("src.strategy.sweep.extract_market_end_ts", return_value=time.time() + 60):  
        
        # Prox = 0.042%, which is < 0.1%, but filter is DISABLED
        base_ctx.crypto_prices["XRP"] = 1.4150
        base_ctx.crypto_price_ts["XRP"] = time.monotonic()
        
        intent = sweep_strategy._build_order(slug, base_ctx.eval_cache[slug], ctx=base_ctx)
        
        # It should ALLOW the trade because filter is disabled
        assert intent is not None
        assert len(intent) == 1

def test_proximity_filter_50_percent(base_ctx, sweep_strategy):
    slug = "xrp-updown-15m-1773541800"
    
    with patch("src.strategy.sweep.PROXIMITY_FILTER_ENABLED", True), \
         patch("src.strategy.sweep.PROXIMITY_MIN_DISTANCE", 0.50), \
         patch("src.strategy.sweep.fetch_strike_price", return_value=1.4156), \
         patch("src.strategy.sweep.extract_market_end_ts", return_value=time.time() + 60):
        
        # Spot price is $1.45 (a HUGE ~2.4% move, much larger than normal 15-minute movements)
        # Prox = |1.45 - 1.4156| / 1.4156 = 0.0243 (2.43%)
        base_ctx.crypto_prices["XRP"] = 1.45
        base_ctx.crypto_price_ts["XRP"] = time.monotonic()
        
        intent = sweep_strategy._build_order(slug, base_ctx.eval_cache[slug], ctx=base_ctx)
        
        # Because 2.43% < 50%, the trade is BLOCKED
        assert intent is None
        assert "proximity" in sweep_strategy.last_skip_reason
