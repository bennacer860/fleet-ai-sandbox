import pytest
import time
from unittest.mock import patch, MagicMock

from src.core.events import TickSizeChange
from src.core.models import Side
from src.strategy.sweep import SweepStrategy
from src.strategy.base import StrategyContext
from src.strategy.proximity import (
    NoOpProximityCalculator,
    SimpleProximityCalculator,
)

@pytest.fixture
def base_ctx():
    ctx = StrategyContext()
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
        "price_to_beat": 1.4156
    }
    return ctx

def _make_strategy(min_distance: float = 0.001, enabled: bool = True) -> SweepStrategy:
    if enabled:
        prox = SimpleProximityCalculator(min_distance=min_distance)
    else:
        prox = NoOpProximityCalculator()
    return SweepStrategy(
        price_threshold=0.99, order_price=0.999,
        proximity_calculator=prox,
    )

def test_proximity_filter_blocks_trade(base_ctx):
    slug = "xrp-updown-15m-1773541800"
    strategy = _make_strategy(min_distance=0.001, enabled=True)
    
    with patch("src.strategy.sweep.extract_market_end_ts", return_value=time.time() + 60):
        # Prox = |1.4150 - 1.4156| / 1.4156 = 0.00042 (0.042%, which is < 0.1%)
        base_ctx.crypto_prices["XRP"] = 1.4150
        base_ctx.crypto_price_ts["XRP"] = time.monotonic()
        
        intent = strategy._build_order(slug, base_ctx.eval_cache[slug], ctx=base_ctx)
        
        assert intent is None
        assert strategy.last_skip_reason is not None
        assert "proximity" in strategy.last_skip_reason
        assert "0.1000%" in strategy.last_skip_reason

def test_proximity_filter_allows_trade(base_ctx):
    slug = "xrp-updown-15m-1773541800"
    strategy = _make_strategy(min_distance=0.001, enabled=True)
    
    with patch("src.strategy.sweep.extract_market_end_ts", return_value=time.time() + 60):
        # Prox = |1.4139 - 1.4156| / 1.4156 = 0.00120 (0.12%, which is > 0.1%)
        base_ctx.crypto_prices["XRP"] = 1.4139
        base_ctx.crypto_price_ts["XRP"] = time.monotonic()
        
        intent = strategy._build_order(slug, base_ctx.eval_cache[slug], ctx=base_ctx)
        
        assert intent is not None
        assert len(intent) == 1
        assert intent[0].side == Side.BUY
        assert intent[0].price == 0.999

def test_proximity_filter_disabled(base_ctx):
    slug = "xrp-updown-15m-1773541800"
    strategy = _make_strategy(enabled=False)
    
    with patch("src.strategy.sweep.extract_market_end_ts", return_value=time.time() + 60):
        # Prox = 0.042%, which is < 0.1%, but filter is DISABLED
        base_ctx.crypto_prices["XRP"] = 1.4150
        base_ctx.crypto_price_ts["XRP"] = time.monotonic()
        
        intent = strategy._build_order(slug, base_ctx.eval_cache[slug], ctx=base_ctx)
        
        assert intent is not None
        assert len(intent) == 1

def test_proximity_filter_50_percent(base_ctx):
    slug = "xrp-updown-15m-1773541800"
    strategy = _make_strategy(min_distance=0.50, enabled=True)
    
    with patch("src.strategy.sweep.extract_market_end_ts", return_value=time.time() + 60):
        # Prox = |1.45 - 1.4156| / 1.4156 = 0.0243 (2.43%) < 50%
        base_ctx.crypto_prices["XRP"] = 1.45
        base_ctx.crypto_price_ts["XRP"] = time.monotonic()
        
        intent = strategy._build_order(slug, base_ctx.eval_cache[slug], ctx=base_ctx)
        
        assert intent is None
        assert "proximity" in strategy.last_skip_reason

def test_different_calculators_different_behavior(base_ctx):
    """Two strategies with different proximity thresholds produce different outcomes."""
    slug = "xrp-updown-15m-1773541800"
    # 0.042% proximity
    base_ctx.crypto_prices["XRP"] = 1.4150
    base_ctx.crypto_price_ts["XRP"] = time.monotonic()

    tight = _make_strategy(min_distance=0.001, enabled=True)   # 0.1% — should block
    loose = _make_strategy(min_distance=0.0001, enabled=True)  # 0.01% — should allow

    with patch("src.strategy.sweep.extract_market_end_ts", return_value=time.time() + 60):
        tight_result = tight._build_order(slug, dict(base_ctx.eval_cache[slug]), ctx=base_ctx)
        loose_result = loose._build_order(slug, dict(base_ctx.eval_cache[slug]), ctx=base_ctx)

    assert tight_result is None
    assert loose_result is not None

def test_post_expiry_bypasses_proximity(base_ctx):
    """Sweep strategy bypasses proximity filter when TTE < 0 (post-expiry)."""
    slug = "xrp-updown-15m-1773541800"
    strategy = _make_strategy(min_distance=0.001, enabled=True)

    base_ctx.crypto_prices["XRP"] = 1.4150   # 0.042% — normally blocked
    base_ctx.crypto_price_ts["XRP"] = time.monotonic()

    with patch("src.strategy.sweep.extract_market_end_ts", return_value=time.time() - 10):
        intent = strategy._build_order(slug, base_ctx.eval_cache[slug], ctx=base_ctx)

    assert intent is not None
    assert len(intent) == 1
