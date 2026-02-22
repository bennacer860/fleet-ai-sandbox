"""Decoupled signal-detection and market-evaluation functions for the endgame sweep strategy.

All functions are pure/stateless so they can be reused from any context
(bot, back-test, notebook, etc.).
"""

from typing import Optional

from ..logging_config import get_logger
from ..utils.market_data import get_best_outcome_token, get_min_order_size

logger = get_logger(__name__)

# ── Constants ─────────────────────────────────────────────────────────────────

# Tick-size value that indicates the market is approaching settlement
SWEEP_TICK_SIZE = "0.001"

# Default minimum price to consider an outcome "most likely"
DEFAULT_PRICE_THRESHOLD = 0.9

# Maximum price we are willing to pay (avoid buying at 1.0)
MAX_ORDER_PRICE = 0.99


# ── Signal detection ──────────────────────────────────────────────────────────


def is_tick_size_signal(new_tick_size: str, target: str = SWEEP_TICK_SIZE) -> bool:
    """Return *True* when the new tick size matches the sweep trigger.

    Args:
        new_tick_size: The ``new_tick_size`` value from the WebSocket event.
        target: Target tick size that constitutes the signal.
    """
    try:
        return float(new_tick_size) == float(target)
    except (ValueError, TypeError):
        return False


# ── Price eligibility ─────────────────────────────────────────────────────────


def is_price_eligible(price: float, threshold: float = DEFAULT_PRICE_THRESHOLD) -> bool:
    """Return *True* if *price* meets or exceeds the sweep threshold."""
    return price >= threshold


# ── Composite decision ────────────────────────────────────────────────────────


def should_place_sweep_order(
    slug: str,
    new_tick_size: str,
    price_threshold: float = DEFAULT_PRICE_THRESHOLD,
) -> Optional[dict]:
    """Full decision pipeline: signal -> evaluate market -> return order params.

    Uses :func:`~src.utils.market_data.get_best_outcome_token` and
    :func:`~src.utils.market_data.get_min_order_size` from the shared utils.

    Returns:
        A dict with keys ``token_id``, ``price``, ``size``, ``outcome``, ``slug``
        if an order should be placed, or *None* if conditions are not met.
    """
    # 1. Check signal
    if not is_tick_size_signal(new_tick_size):
        return None

    # 2. Evaluate market
    result = get_best_outcome_token(slug)
    if result is None:
        return None

    token_id, best_price, outcome = result

    # 3. Price eligible?
    if not is_price_eligible(best_price, price_threshold):
        logger.info(
            "Price %.4f below threshold %.2f for %s – skipping order",
            best_price,
            price_threshold,
            slug,
        )
        return None

    # 4. Determine order price (cap at MAX_ORDER_PRICE)
    order_price = min(best_price, MAX_ORDER_PRICE)

    # 5. Minimum order size
    order_size = get_min_order_size(token_id)

    return {
        "token_id": token_id,
        "price": order_price,
        "size": order_size,
        "outcome": outcome,
        "slug": slug,
    }
