"""Slug-generation helpers shared by monitors and bots.

Wraps ``src.markets.fifteen_min.get_market_slug`` with error handling so
callers don't each need their own try/except loop.
"""

from ..logging_config import get_logger
from ..markets.fifteen_min import (
    MarketSelection,
    get_market_slug,
)

logger = get_logger(__name__)


def slugs_for_timestamp(
    selections: list[MarketSelection],
    duration: int,
    timestamp: int,
) -> list[str]:
    """Generate market slugs for every *selection* at a given *timestamp*.

    Args:
        selections: Crypto asset keys (e.g. ``["BTC", "ETH"]``).
        duration: Market duration in minutes (5 or 15).
        timestamp: Unix timestamp aligned to the interval boundary.

    Returns:
        List of successfully generated slugs (failures are logged and skipped).
    """
    slugs: list[str] = []
    for sel in selections:
        try:
            slugs.append(get_market_slug(sel, duration, timestamp))
        except ValueError as exc:
            logger.error(
                "Failed to generate slug for %s/%dm at %d: %s",
                sel,
                duration,
                timestamp,
                exc,
            )
    return slugs
