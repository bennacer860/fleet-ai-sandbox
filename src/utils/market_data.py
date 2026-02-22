"""Reusable market-data helpers.

Provides functions that query the Gamma API / CLOB to evaluate markets
(best outcome, minimum order size, etc.).  Used by the sweep strategy,
back-tests, and ad-hoc scripts.
"""

from typing import Optional

from ..clob_client import create_clob_client
from ..gamma_client import (
    fetch_event_by_slug,
    get_market_token_ids,
    get_outcomes,
    get_outcome_prices,
)
from ..logging_config import get_logger

logger = get_logger(__name__)

# Fallback when the order book is unavailable
FALLBACK_MIN_ORDER_SIZE: float = 1.0


# ── Market evaluation ────────────────────────────────────────────────────────


def get_best_outcome_token(
    slug: str,
) -> Optional[tuple[str, float, str]]:
    """Fetch the market for *slug* and return info on the most-likely outcome.

    Queries the Gamma API for current outcome prices and picks the outcome
    with the highest price.

    Args:
        slug: Market/event slug (e.g. ``"btc-updown-5m-1707523200"``).

    Returns:
        ``(token_id, price, outcome_label)`` for the best outcome,
        or *None* if the market cannot be evaluated.
    """
    event = fetch_event_by_slug(slug)
    if not event:
        logger.warning("Cannot evaluate market – event not found: %s", slug)
        return None

    markets = event.get("markets", [])
    if not markets:
        logger.warning("Cannot evaluate market – no markets in event: %s", slug)
        return None

    market = markets[0]
    token_ids = get_market_token_ids(market)
    outcomes = get_outcomes(market)
    prices = get_outcome_prices(market)

    if len(token_ids) < 2 or len(prices) < 2:
        logger.warning(
            "Cannot evaluate market – incomplete data: tokens=%d prices=%d slug=%s",
            len(token_ids),
            len(prices),
            slug,
        )
        return None

    # Pick the outcome with the highest current price
    best_idx = 0
    best_price = prices[0]
    for i, p in enumerate(prices):
        if p > best_price:
            best_price = p
            best_idx = i

    outcome_label = outcomes[best_idx] if best_idx < len(outcomes) else "?"
    token_id = token_ids[best_idx]

    logger.info(
        "Best outcome for %s: %s (price=%.4f, token=%s…)",
        slug,
        outcome_label,
        best_price,
        token_id[:20],
    )
    return token_id, best_price, outcome_label


# ── Order-book helpers ───────────────────────────────────────────────────────


def get_min_order_size(token_id: str) -> float:
    """Query the CLOB order book for the minimum order size of *token_id*.

    Falls back to ``FALLBACK_MIN_ORDER_SIZE`` if the book cannot be fetched.

    Args:
        token_id: CLOB token ID to look up.

    Returns:
        Minimum order size (float).
    """
    client = create_clob_client()
    if client is None:
        logger.warning("CLOB client unavailable – using fallback min_order_size")
        return FALLBACK_MIN_ORDER_SIZE
    try:
        book = client.get_order_book(token_id)
        mos = (
            float(book.min_order_size)
            if book.min_order_size
            else FALLBACK_MIN_ORDER_SIZE
        )
        logger.debug("min_order_size for %s…: %.2f", token_id[:20], mos)
        return mos
    except Exception as exc:
        logger.warning("Failed to fetch order book for min_order_size: %s", exc)
        return FALLBACK_MIN_ORDER_SIZE
