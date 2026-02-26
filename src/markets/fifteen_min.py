"""Utilities for recurring crypto up/down markets (5-min, 15-min, etc.)."""

import time
from typing import Literal, Optional

from ..logging_config import get_logger

logger = get_logger(__name__)

# ── Supported durations ──────────────────────────────────────────────────────

SUPPORTED_DURATIONS: set[int] = {5, 15}

# Polymarket API slug fragment for each duration (e.g. "5m", "15m")
_DURATION_SLUG: dict[int, str] = {
    5: "5m",
    15: "15m",
}

# Human-readable label used in formatted slugs (e.g. "5min", "15min")
_DURATION_LABEL: dict[int, str] = {
    5: "5min",
    15: "15min",
}

MarketSelection = Literal["BTC", "ETH", "SOL", "XRP"]


# ── Duration helpers ─────────────────────────────────────────────────────────

def duration_label(duration_minutes: int) -> str:
    """Return human-readable duration label, e.g. '5min' or '15min'.

    Args:
        duration_minutes: Market duration in minutes.

    Returns:
        Label string like "5min" or "15min". Falls back to "{n}min" for unknown durations.
    """
    return _DURATION_LABEL.get(duration_minutes, f"{duration_minutes}min")


def detect_duration_from_slug(slug: str) -> Optional[int]:
    """Detect market duration from a raw Polymarket API slug.

    Looks for '-5m-' or '-15m-' (or as a trailing segment) inside the slug.

    Args:
        slug: Raw slug, e.g. "btc-updown-5m-1707523200"

    Returns:
        Duration in minutes (5 or 15), or None if not detectable.
    """
    slug_lower = slug.lower()
    # Check longer pattern first to avoid "-15m-" matching "-5m-" substring
    if "-15m-" in slug_lower or slug_lower.endswith("-15m"):
        return 15
    if "-5m-" in slug_lower or slug_lower.endswith("-5m"):
        return 5
    return None


# ── Interval / timestamp helpers ─────────────────────────────────────────────

def get_current_interval_utc(duration_minutes: int) -> int:
    """Get current interval-aligned UTC timestamp.

    Args:
        duration_minutes: Interval size in minutes (e.g. 5 or 15).

    Returns:
        Unix timestamp rounded down to the nearest interval boundary.
    """
    interval_seconds = duration_minutes * 60
    now = int(time.time())
    return (now // interval_seconds) * interval_seconds


def get_next_interval_utc(duration_minutes: int) -> int:
    """Get the next interval-aligned UTC timestamp.

    Args:
        duration_minutes: Interval size in minutes (e.g. 5 or 15).

    Returns:
        Unix timestamp for the start of the next interval.
    """
    return get_current_interval_utc(duration_minutes) + duration_minutes * 60


# ── Slug generation ──────────────────────────────────────────────────────────

def _market_base(crypto: MarketSelection, duration_minutes: int) -> str:
    """Return the API slug prefix, e.g. 'btc-updown-5m'.

    Args:
        crypto: Crypto asset key (BTC, ETH, SOL, XRP).
        duration_minutes: Market duration in minutes.

    Returns:
        Slug prefix string.

    Raises:
        ValueError: If duration_minutes is not in SUPPORTED_DURATIONS.
    """
    slug_suffix = _DURATION_SLUG.get(duration_minutes)
    if slug_suffix is None:
        raise ValueError(
            f"Unsupported duration: {duration_minutes}m. "
            f"Supported: {sorted(SUPPORTED_DURATIONS)}"
        )
    return f"{crypto.lower()}-updown-{slug_suffix}"


def get_market_slug(
    market_selection: MarketSelection,
    duration_minutes: int = 15,
    timestamp: Optional[int] = None,
) -> str:
    """Get market slug for a crypto up/down market at any supported duration.

    Args:
        market_selection: Crypto asset to trade (BTC, ETH, SOL, XRP)
        duration_minutes: Market duration in minutes (5 or 15, default 15)
        timestamp: Optional Unix timestamp (if None, uses current interval)

    Returns:
        Market slug in format: "{market_base}-{timestamp}"
        Example: "btc-updown-5m-1707523200" or "btc-updown-15m-1707523200"

    Raises:
        ValueError: If market_selection or duration_minutes is invalid.
    """
    base = _market_base(market_selection, duration_minutes)

    if timestamp is None:
        timestamp = get_current_interval_utc(duration_minutes)

    slug = f"{base}-{timestamp}"
    logger.debug("Generated market slug: %s", slug)
    return slug


def extract_market_end_ts(slug: str) -> int | None:
    """Extract the market end unix timestamp from a slug.

    Slug format: ``{crypto}-updown-{5m|15m}-{start_ts}``
    Market end = start_ts + duration_seconds.

    Returns:
        End timestamp as int, or None if the slug cannot be parsed.
    """
    duration = detect_duration_from_slug(slug)
    if duration is None:
        return None
    parts = slug.rsplit("-", 1)
    if len(parts) != 2:
        return None
    try:
        start_ts = int(parts[1])
    except ValueError:
        return None
    return start_ts + duration * 60


# ── Backward-compatibility aliases ───────────────────────────────────────────
# These keep existing imports (continuous_15min_monitor, monitor_multi_events,
# etc.) working without modification during the transition.

MARKET_IDS: dict[str, str] = {
    sel: _market_base(sel, 15) for sel in ("BTC", "ETH", "SOL", "XRP")
}

FIFTEEN_MIN_SECONDS: int = 15 * 60


def get_current_15m_utc() -> int:
    """Backward-compat alias for get_current_interval_utc(15)."""
    return get_current_interval_utc(15)


def get_next_15m_utc() -> int:
    """Backward-compat alias for get_next_interval_utc(15)."""
    return get_next_interval_utc(15)
