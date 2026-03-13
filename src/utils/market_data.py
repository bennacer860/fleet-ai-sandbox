"""Reusable market-data helpers.

Provides functions that query the Gamma API / CLOB to evaluate markets
(best outcome, minimum order size, etc.).  Used by the sweep strategy,
back-tests, and ad-hoc scripts.
"""

from typing import Any, Optional

import requests

from ..clob_client import create_clob_client
from ..gamma_client import (
    fetch_event_by_slug,
    get_market_token_ids,
    get_outcomes,
    get_outcome_prices,
)
from ..logging_config import get_logger
from ..markets.fifteen_min import extract_market_from_slug

logger = get_logger(__name__)

# Fallback when the order book is unavailable
FALLBACK_MIN_ORDER_SIZE: float = 5.0

_BINANCE_KLINES = "https://api.binance.com/api/v3/klines"

_ASSET_TO_SYMBOL: dict[str, str] = {
    "BTC": "BTCUSDT",
    "ETH": "ETHUSDT",
    "SOL": "SOLUSDT",
    "XRP": "XRPUSDT",
}


def fetch_strike_price(slug: str, timeout: float = 5.0) -> float | None:
    """Fetch the crypto spot price at the market's start time from Binance klines.

    The slug encodes the start timestamp (e.g. ``xrp-updown-15m-1773304200``).
    We fetch the 1-minute candle that contains that timestamp and return
    the open price, which is the effective strike price for the market.
    """
    asset = extract_market_from_slug(slug)
    if not asset:
        return None

    symbol = _ASSET_TO_SYMBOL.get(asset)
    if not symbol:
        return None

    # Extract start timestamp from slug
    parts = slug.rsplit("-", 1)
    if len(parts) != 2:
        return None
    try:
        start_ts = int(parts[1])
    except ValueError:
        return None

    start_ms = start_ts * 1000

    try:
        resp = requests.get(
            _BINANCE_KLINES,
            params={
                "symbol": symbol,
                "interval": "1m",
                "startTime": start_ms,
                "limit": 1,
            },
            timeout=timeout,
        )
        resp.raise_for_status()
        klines = resp.json()
        if klines and len(klines) > 0:
            open_price = float(klines[0][1])
            logger.debug("[STRIKE] %s start=%d → %s open=$%.6f", slug, start_ts, symbol, open_price)
            return open_price
    except Exception:
        logger.debug("[STRIKE] Failed to fetch kline for %s", slug, exc_info=True)

    return None


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

    # priceToBeat lives in eventMetadata — check both the market object
    # and the top-level event, since the Gamma API nests it differently
    # depending on the endpoint (/events/slug/ vs /markets/).
    price_to_beat = None
    for obj in (market, event):
        metadata = obj.get("eventMetadata") or {}
        raw = metadata.get("priceToBeat")
        if raw is not None:
            try:
                price_to_beat = float(raw)
                break
            except (ValueError, TypeError):
                pass

    if price_to_beat is None:
        logger.warning("[STRIKE] priceToBeat not in Gamma response for %s, falling back to Binance kline", slug)
        price_to_beat = fetch_strike_price(slug)

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
