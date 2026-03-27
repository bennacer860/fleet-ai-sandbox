"""Reusable timestamp helpers.

Provides UTC/EST timestamp formatting used by monitors, strategies,
and analysis scripts.
"""

from datetime import datetime
from typing import Optional

from pytz import timezone as pytz_timezone

from ..logging_config import get_logger
from ..markets.fifteen_min import detect_duration_from_slug, duration_label

logger = get_logger(__name__)

# Timezone objects (created once)
_UTC = pytz_timezone("UTC")
_EST = pytz_timezone("US/Eastern")

# Known crypto prefixes for slug formatting (short and long names)
_CRYPTO_PREFIXES = ("btc", "eth", "sol", "xrp", "doge", "hype", "bnb")
_LONG_TO_SHORT = {
    "bitcoin": "btc",
    "ethereum": "eth",
    "solana": "sol",
    "dogecoin": "doge",
    "hyperliquid": "hype",
}


# ── Basic timestamp helpers ──────────────────────────────────────────────────


def get_timestamps() -> tuple[int, str, str]:
    """Return ``(timestamp_ms, timestamp_iso, timestamp_est)`` for *now*.

    * ``timestamp_ms``  – Unix epoch in milliseconds.
    * ``timestamp_iso`` – ISO-style UTC string  ``YYYY-MM-DD HH:MM:SS``.
    * ``timestamp_est`` – Same format converted to US/Eastern.
    """
    now_utc = datetime.now(_UTC)
    timestamp_ms = int(now_utc.timestamp() * 1000)
    timestamp_iso = now_utc.strftime("%Y-%m-%d %H:%M:%S")
    timestamp_est = now_utc.astimezone(_EST).strftime("%Y-%m-%d %H:%M:%S")
    return timestamp_ms, timestamp_iso, timestamp_est


def ts_to_est(ts_sec: int) -> str:
    """Convert a Unix timestamp (seconds) to an EST datetime string.

    Args:
        ts_sec: Unix timestamp in seconds.

    Returns:
        ``YYYY-MM-DD HH:MM:SS`` in US/Eastern.
    """
    return datetime.fromtimestamp(ts_sec, tz=_EST).strftime("%Y-%m-%d %H:%M:%S")


# ── Slug formatting ──────────────────────────────────────────────────────────


def format_slug_with_est_time(slug: str, timestamp_ms: Optional[int] = None) -> str:
    """Format a raw API slug into a human-readable slug with EST date+time.

    Conversions::

        "btc-updown-15m-1707523200" -> "btc-15min-up-or-down-2026-02-20-16:15"
        "btc-updown-5m-1707523200"  -> "btc-5min-up-or-down-2026-02-20-16:05"

    Args:
        slug: Original raw event slug.
        timestamp_ms: Optional timestamp in **milliseconds**.  If *None* the
            function tries to extract a timestamp from the slug itself, falling
            back to the current time.

    Returns:
        Formatted slug, e.g. ``"btc-15min-up-or-down-2026-02-20-16:15"``.
    """
    slug_lower = slug.lower()

    # Detect duration from slug (defaults to 15min if undetectable)
    detected_dur = detect_duration_from_slug(slug)
    dur_label = duration_label(detected_dur if detected_dur is not None else 15)

    # 1h slugs are already human-readable (e.g. bitcoin-up-or-down-march-4-5pm-et)
    # Just normalize the crypto prefix and return.
    if detected_dur == 60 and "-up-or-down-" in slug_lower:
        # Normalize long asset names (bitcoin→btc, ethereum→eth, solana→sol)
        first_seg = slug_lower.split("-")[0]
        short = _LONG_TO_SHORT.get(first_seg, first_seg)
        # Strip the original asset prefix and rebuild
        rest = slug_lower[len(first_seg):]  # e.g. "-up-or-down-march-4-5pm-et"
        return f"{short}-{dur_label}{rest}"

    # Identify crypto prefix
    crypto: Optional[str] = None
    for prefix in _CRYPTO_PREFIXES:
        if slug_lower.startswith(prefix):
            crypto = prefix
            break

    # Try to extract a Unix timestamp from the last segment of the slug
    parts = slug.split("-")
    timestamp: Optional[int] = None
    if len(parts) >= 2:
        try:
            timestamp = int(parts[-1])
        except (ValueError, TypeError):
            pass

    # Fallback: use provided timestamp_ms or current time
    if timestamp is None:
        if timestamp_ms:
            timestamp = timestamp_ms // 1000
        else:
            timestamp = int(datetime.now(_UTC).timestamp())

    # Convert to EST
    try:
        dt = datetime.fromtimestamp(timestamp, tz=_EST)
    except (OSError, ValueError):
        dt = datetime.fromtimestamp(timestamp, tz=_UTC).astimezone(_EST)

    date_str = dt.strftime("%Y-%m-%d")
    time_str = dt.strftime("%H:%M")

    if crypto:
        return f"{crypto}-{dur_label}-up-or-down-{date_str}-{time_str}"

    # Fallback: strip trailing numeric timestamp and append date+time
    if parts and parts[-1].isdigit():
        prefix = "-".join(parts[:-1])
    else:
        prefix = slug

    return f"{prefix}-{date_str}-{time_str}"
