"""Slug generation and parsing for daily stock/ETF up-or-down markets.

Polymarket lists two daily markets per stock ticker:
  - "{ticker}-opens-up-or-down-on-{month}-{day}-{year}"  (expires 9:30 AM EST)
  - "{ticker}-up-or-down-on-{month}-{day}-{year}"        (expires 4:00 PM EST)

All functions are ticker-agnostic — pass any ticker string and the
correct slug or timestamp is produced.
"""

from __future__ import annotations

import re
import calendar
from datetime import date, datetime, time as dt_time
from typing import Optional

import pytz

from ..logging_config import get_logger

logger = get_logger(__name__)

_EST = pytz.timezone("US/Eastern")

_MARKET_OPEN_TIME = dt_time(9, 30)
_MARKET_CLOSE_TIME = dt_time(16, 0)

_KNOWN_CRYPTO_PREFIXES: frozenset[str] = frozenset({
    "bitcoin", "ethereum", "solana", "xrp", "dogecoin", "hyperliquid", "bnb",
    "btc", "eth", "sol", "doge", "hype",
})

# Slug format: {ticker}-opens-up-or-down-on-{month}-{day}-{year}
_OPENS_RE = re.compile(
    r"^([a-z]+)-opens-up-or-down-on-([a-z]+)-(\d{1,2})-(\d{4})$"
)

# Slug format: {ticker}-up-or-down-on-{month}-{day}-{year}
_CLOSE_RE = re.compile(
    r"^([a-z]+)-up-or-down-on-([a-z]+)-(\d{1,2})-(\d{4})$"
)

_MONTH_NUM: dict[str, int] = {
    name.lower(): num
    for num, name in enumerate(calendar.month_name)
    if num
}


def generate_stock_slugs_for_date(ticker: str, dt: date) -> list[str]:
    """Return the two daily slugs for *ticker* on date *dt*.

    Returns [opens_slug, close_slug].  Skips weekends (returns []).
    """
    if dt.weekday() >= 5:
        return []

    t = ticker.lower()
    month = dt.strftime("%B").lower()
    day = dt.day
    year = dt.year

    return [
        f"{t}-opens-up-or-down-on-{month}-{day}-{year}",
        f"{t}-up-or-down-on-{month}-{day}-{year}",
    ]


def is_stock_slug(slug: str) -> bool:
    """Return True if *slug* looks like a stock daily market (not crypto)."""
    s = slug.lower()

    m = _OPENS_RE.match(s)
    if m and m.group(1) not in _KNOWN_CRYPTO_PREFIXES:
        return True

    m = _CLOSE_RE.match(s)
    if m and m.group(1) not in _KNOWN_CRYPTO_PREFIXES:
        return True

    return False


def parse_stock_slug_end_ts(slug: str) -> Optional[int]:
    """Parse expiration timestamp from a stock daily slug.

    - ``*-opens-up-or-down-on-*`` -> 9:30 AM EST on that date
    - ``*-up-or-down-on-*``       -> 4:00 PM EST on that date

    Returns unix timestamp or None if unparseable.
    """
    s = slug.lower()

    m = _OPENS_RE.match(s)
    if m:
        return _resolve_ts(m.group(2), int(m.group(3)), int(m.group(4)), _MARKET_OPEN_TIME)

    m = _CLOSE_RE.match(s)
    if m:
        return _resolve_ts(m.group(2), int(m.group(3)), int(m.group(4)), _MARKET_CLOSE_TIME)

    return None


def extract_ticker_from_stock_slug(slug: str) -> Optional[str]:
    """Return the uppercase ticker from a stock slug, e.g. 'SPX'."""
    s = slug.lower()
    m = _OPENS_RE.match(s) or _CLOSE_RE.match(s)
    if m:
        return m.group(1).upper()
    return None


def _resolve_ts(month_name: str, day: int, year: int, t: dt_time) -> Optional[int]:
    month_num = _MONTH_NUM.get(month_name)
    if month_num is None:
        return None
    try:
        naive = datetime(year, month_num, day, t.hour, t.minute, t.second)
        local_dt = _EST.localize(naive)
    except Exception:
        return None
    return int(local_dt.timestamp())
