"""Utilities for recurring crypto up/down markets (5-min, 15-min, etc.)."""

import time
from typing import Literal, Optional

from ..logging_config import get_logger

logger = get_logger(__name__)

# ── Supported durations ──────────────────────────────────────────────────────

SUPPORTED_DURATIONS: set[int] = {5, 15, 60, 240, 1440}

# Polymarket API slug fragment for each duration (e.g. "5m", "15m")
_DURATION_SLUG: dict[int, str] = {
    5: "5m",
    15: "15m",
    60: "1h",
    240: "4h",
    1440: "1d",
}

# Human-readable label used in formatted slugs (e.g. "5min", "15min")
_DURATION_LABEL: dict[int, str] = {
    5: "5min",
    15: "15min",
    60: "1hour",
    240: "4hour",
    1440: "daily",
}

MarketSelection = Literal["BTC", "ETH", "SOL", "XRP", "DOGE", "HYPE", "BNB"]

# Mapping for 1-hour human-readable slugs
_ASSET_NAME_MAP: dict[str, str] = {
    "BTC": "bitcoin",
    "ETH": "ethereum",
    "SOL": "solana",
    "XRP": "xrp",
    "DOGE": "dogecoin",
    "HYPE": "hyperliquid",
    "BNB": "bnb",
}


_SLUG_PREFIX_TO_MARKET: dict[str, str] = {
    "btc": "BTC",
    "eth": "ETH",
    "sol": "SOL",
    "xrp": "XRP",
    "doge": "DOGE",
    "hype": "HYPE",
    "bnb": "BNB",
    "bitcoin": "BTC",
    "ethereum": "ETH",
    "solana": "SOL",
    "dogecoin": "DOGE",
    "hyperliquid": "HYPE",
}


def extract_market_from_slug(slug: str) -> str:
    """Extract the crypto asset name from a market slug.

    Handles both short-form (btc-updown-5m-...) and long-form
    (bitcoin-up-or-down-...) slug formats.

    Returns uppercase ticker (e.g. "BTC") or the raw prefix if unknown.
    """
    prefix = slug.split("-")[0].lower()
    return _SLUG_PREFIX_TO_MARKET.get(prefix, prefix.upper())


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
    Also handles 1h human-readable slugs containing '-up-or-down-'.

    Args:
        slug: Raw slug, e.g. "btc-updown-5m-1707523200"

    Returns:
        Duration in minutes (5, 15, or 60), or None if not detectable.
    """
    slug_lower = slug.lower()

    # 1h: ends with "-et" (e.g. bitcoin-up-or-down-march-9-2026-10pm-et)
    if "-up-or-down-" in slug_lower and slug_lower.endswith("-et"):
        return 60

    # Daily: has "-up-or-down-on-" (e.g. bitcoin-up-or-down-on-march31-2026)
    # Must be checked before the generic "-up-or-down-" fallback.
    if "-up-or-down-on-" in slug_lower:
        return 1440

    # Check longer/more-specific patterns first to avoid substring false matches
    if "-4h-" in slug_lower or slug_lower.endswith("-4h"):
        return 240
    if "-15m-" in slug_lower or slug_lower.endswith("-15m"):
        return 15
    if "-5m-" in slug_lower or slug_lower.endswith("-5m"):
        return 5

    # Generic 1h fallback (human-readable without -et suffix)
    if "-up-or-down-" in slug_lower:
        return 60

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
        duration_minutes: Market duration in minutes (5, 15, or 60)
        timestamp: Optional Unix timestamp (if None, uses current interval)

    Returns:
        Market slug.
        5/15m format: "{crypto}-updown-{5m|15m}-{timestamp}"
        1h format: "{asset}-up-or-down-{month}-{day}-{hour_est}-et"

    Raises:
        ValueError: If market_selection or duration_minutes is invalid.
    """
    if duration_minutes not in SUPPORTED_DURATIONS:
        raise ValueError(f"Unsupported duration: {duration_minutes}")

    if timestamp is None:
        timestamp = get_current_interval_utc(duration_minutes)

    # Daily markets: {asset}-up-or-down-on-{month}{day}-{year}
    if duration_minutes == 1440:
        import pytz
        from datetime import datetime

        asset = _ASSET_NAME_MAP.get(market_selection.upper(), market_selection.lower())
        est_tz = pytz.timezone("US/Eastern")
        dt = datetime.fromtimestamp(timestamp, tz=pytz.utc).astimezone(est_tz)

        month = dt.strftime("%B").lower()
        day = dt.day
        year = dt.year
        return f"{asset}-up-or-down-on-{month}{day}-{year}"

    # 1-hour markets use a human-readable slug format
    if duration_minutes == 60:
        import pytz
        from datetime import datetime
        
        asset = _ASSET_NAME_MAP.get(market_selection.upper(), market_selection.lower())
        
        # Polymarket 1h slugs use US/Eastern time strings
        est_tz = pytz.timezone("US/Eastern")
        dt = datetime.fromtimestamp(timestamp, tz=pytz.utc).astimezone(est_tz)
        
        month = dt.strftime("%B").lower()
        day = dt.day
        year = dt.year
        hour_int = dt.hour
        
        # Hour formatting: 11am, 12pm, 1pm, etc.
        if hour_int == 0:
            hour_str = "12am"
        elif hour_int < 12:
            hour_str = f"{hour_int}am"
        elif hour_int == 12:
            hour_str = "12pm"
        else:
            hour_str = f"{hour_int - 12}pm"
            
        return f"{asset}-up-or-down-{month}-{day}-{year}-{hour_str}-et"

    # Default to numeric-timestamp format for 5m/15m/4h
    base = _market_base(market_selection, duration_minutes)
    slug = f"{base}-{timestamp}"
    logger.debug("Generated market slug: %s", slug)
    return slug


def _parse_1h_slug_start_ts(slug: str) -> int | None:
    """Parse start timestamp from a 1-hour human-readable slug.

    Expected format: ``{asset}-up-or-down-{month}-{day}-{year}-{hour}{am|pm}-et``
    Example: ``bitcoin-up-or-down-march-9-2026-10pm-et``

    Returns:
        Start Unix timestamp, or None if parsing fails.
    """
    import re
    import calendar
    from datetime import datetime

    import pytz

    slug_lower = slug.lower()
    
    # Try the new format with year: anything-up-or-down-{month}-{day}-{year}-{hour}{am/pm}-et
    m = re.search(
        r"-up-or-down-([a-z]+)-(\d+)-(\d{4})-(\d{1,2})(am|pm)-et$",
        slug_lower,
    )
    
    if m:
        month_name, day_str, year_str, hour_str, ampm = m.groups()
        year = int(year_str)
    else:
        # Fallback to old format without year
        m = re.search(
            r"-up-or-down-([a-z]+)-(\d+)-(\d{1,2})(am|pm)-et$",
            slug_lower,
        )
        if not m:
            return None
        month_name, day_str, hour_str, ampm = m.groups()
        est = pytz.timezone("US/Eastern")
        now_est = datetime.now(est)
        year = now_est.year

    # Resolve month name → number
    month_abbrevs = {v.lower(): k for k, v in enumerate(calendar.month_name) if k}
    month_full = {v.lower(): k for k, v in enumerate(calendar.month_abbr) if k}
    month_num = month_abbrevs.get(month_name) or month_full.get(month_name)
    if month_num is None:
        return None

    day = int(day_str)
    hour = int(hour_str)

    # Convert 12-hour → 24-hour
    if ampm == "am":
        if hour == 12:
            hour = 0
    else:  # pm
        if hour != 12:
            hour += 12

    est = pytz.timezone("US/Eastern")

    try:
        naive = datetime(year, month_num, day, hour, 0, 0)
        local_dt = est.localize(naive)
    except Exception:
        return None

    return int(local_dt.timestamp())


def _parse_daily_slug_start_ts(slug: str) -> int | None:
    """Parse start timestamp from a daily human-readable slug.

    Expected format: ``{asset}-up-or-down-on-{month}{day}-{year}``
    Example: ``bitcoin-up-or-down-on-march31-2026``

    The market day starts at midnight US/Eastern.

    Returns:
        Start Unix timestamp (midnight ET), or None if parsing fails.
    """
    import re
    import calendar
    from datetime import datetime

    import pytz

    slug_lower = slug.lower()

    m = re.search(
        r"-up-or-down-on-([a-z]+)(\d{1,2})-(\d{4})$",
        slug_lower,
    )
    if not m:
        return None

    month_name, day_str, year_str = m.groups()

    month_abbrevs = {v.lower(): k for k, v in enumerate(calendar.month_name) if k}
    month_full = {v.lower(): k for k, v in enumerate(calendar.month_abbr) if k}
    month_num = month_abbrevs.get(month_name) or month_full.get(month_name)
    if month_num is None:
        return None

    day = int(day_str)
    year = int(year_str)

    est = pytz.timezone("US/Eastern")
    try:
        naive = datetime(year, month_num, day, 0, 0, 0)
        local_dt = est.localize(naive)
    except Exception:
        return None

    return int(local_dt.timestamp())


def extract_market_end_ts(slug: str) -> int | None:
    """Extract the market end unix timestamp from a slug.

    Handles all formats:
    - 5m/15m/4h: ``{crypto}-updown-{5m|15m|4h}-{start_ts}``
    - 1h: ``{asset}-up-or-down-{month}-{day}-{year}-{hour}{am|pm}-et``
    - daily: ``{asset}-up-or-down-on-{month}{day}-{year}``

    Market end = start_ts + duration_seconds.

    Returns:
        End timestamp as int, or None if the slug cannot be parsed.
    """
    duration = detect_duration_from_slug(slug)
    if duration is None:
        return None

    # Daily human-readable slugs
    if duration == 1440:
        start_ts = _parse_daily_slug_start_ts(slug)
        if start_ts is None:
            return None
        return start_ts + 24 * 60 * 60

    # 1-hour human-readable slugs
    if duration == 60:
        start_ts = _parse_1h_slug_start_ts(slug)
        if start_ts is None:
            return None
        return start_ts + 60 * 60

    # 5m / 15m / 4h numeric-timestamp slugs
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
    sel: _market_base(sel, 15) for sel in ("BTC", "ETH", "SOL", "XRP", "DOGE", "HYPE", "BNB")
}

FIFTEEN_MIN_SECONDS: int = 15 * 60


def get_current_15m_utc() -> int:
    """Backward-compat alias for get_current_interval_utc(15)."""
    return get_current_interval_utc(15)


def get_next_15m_utc() -> int:
    """Backward-compat alias for get_next_interval_utc(15)."""
    return get_next_interval_utc(15)
