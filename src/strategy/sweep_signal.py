"""Decoupled signal-detection and market-evaluation functions for the endgame sweep strategy.

All functions are pure/stateless so they can be reused from any context
(bot, back-test, notebook, etc.).
"""

from typing import Optional

from ..logging_config import get_logger
from ..utils.market_data import get_best_outcome_token, get_min_order_size, get_market_evaluation, FALLBACK_MIN_ORDER_SIZE

logger = get_logger(__name__)

# ── Constants ─────────────────────────────────────────────────────────────────

# Tick-size value that indicates the market is approaching settlement
SWEEP_TICK_SIZE = "0.001"

# Default minimum price to consider an outcome "almost certain" (increased to avoid reversals)
DEFAULT_PRICE_THRESHOLD = 0.95

# The price at which we place our limit order to "sweep" the book.
# Using 0.999 ensures we fill against any available sellers below settlement.
MAX_ORDER_PRICE = 0.999  # Valid when tick_size=0.001 (range: 0.001–0.999)


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
    eval_data: Optional[dict] = None,
) -> Optional[dict]:
    """Full decision pipeline: signal -> evaluate market -> return order params.

    Returns:
        A dict with keys ``token_id``, ``price``, ``size``, ``outcome``, ``slug``,
        and optionally ``eval_data`` for logging.
    """
    # 1. Check signal
    if not is_tick_size_signal(new_tick_size):
        return None

    # 2. Evaluate market (use pre-fetched if available)
    if eval_data is None:
        eval_data = get_market_evaluation(slug)
    
    if eval_data is None:
        return None

    token_id = eval_data["best_token_id"]
    best_price = eval_data["best_price"]
    outcome = eval_data["best_outcome"]

    # 3. Price eligible?
    if not is_price_eligible(best_price, price_threshold):
        return {
            "skip": True,
            "reason": f"Price {best_price:.4f} below threshold {price_threshold:.2f}",
            "eval_data": eval_data
        }

    # 4. Use aggressive sweep price (0.999) to guarantee fill
    order_price = MAX_ORDER_PRICE

    # 5. Minimum order size — prefer the pre-cached value from eval_data
    #    so the hot path avoids a blocking CLOB HTTP call.
    order_size = eval_data.get("min_order_size") or get_min_order_size(token_id)

    return {
        "token_id": token_id,
        "price": order_price,
        "size": order_size,
        "outcome": outcome,
        "slug": slug,
        "eval_data": eval_data
    }
