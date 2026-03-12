"""Reusable market-data helpers.

Provides functions that query the Gamma API / CLOB to evaluate markets
(best outcome, minimum order size, etc.).  Used by the sweep strategy,
back-tests, and ad-hoc scripts.
"""

from typing import Any, Optional

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
FALLBACK_MIN_ORDER_SIZE: float = 5.0


# ── Market evaluation ────────────────────────────────────────────────────────


def get_market_evaluation(slug: str) -> Optional[dict[str, Any]]:
    """Fetch comprehensive market data for *slug*.

    Queries Gamma API for all outcome prices, labels, and token IDs.
    
    Returns:
        Dict with 'token_ids', 'prices', 'outcomes', 'best_idx', 'best_price', 
        'best_outcome', 'best_token_id', or None if failed.
    """
    event = fetch_event_by_slug(slug)
    if not event:
        return None

    markets = event.get("markets", [])
    if not markets:
        return None

    market = markets[0]
    token_ids = get_market_token_ids(market)
    outcomes = get_outcomes(market)
    prices = get_outcome_prices(market)

    if len(token_ids) < 2 or len(prices) < 2:
        return None

    # Pick the outcome with the highest current price
    best_idx = 0
    best_price = prices[0]
    for i, p in enumerate(prices):
        if p > best_price:
            best_price = p
            best_idx = i

    best_outcome = outcomes[best_idx] if best_idx < len(outcomes) else "?"
    best_token_id = token_ids[best_idx]

    metadata = market.get("eventMetadata") or {}
    price_to_beat = metadata.get("priceToBeat")
    if price_to_beat is not None:
        try:
            price_to_beat = float(price_to_beat)
        except (ValueError, TypeError):
            price_to_beat = None

    return {
        "token_ids": token_ids,
        "prices": prices,
        "outcomes": outcomes,
        "best_idx": best_idx,
        "best_price": best_price,
        "best_outcome": best_outcome,
        "best_token_id": best_token_id,
        "price_to_beat": price_to_beat,
        "raw_prices_compact": "|".join([f"{o}:{p:.3f}" for o, p in zip(outcomes, prices)])
    }


def get_best_outcome_token(
    slug: str,
) -> Optional[tuple[str, float, str]]:
    """Fetch the market for *slug* and return info on the most-likely outcome.
    
    (Legacy wrapper around get_market_evaluation)
    """
    eval_data = get_market_evaluation(slug)
    if eval_data:
        return (eval_data["best_token_id"], eval_data["best_price"], eval_data["best_outcome"])
    return None


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
