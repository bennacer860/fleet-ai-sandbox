"""Category-based market discovery helpers."""

from __future__ import annotations

import time
from datetime import datetime
from typing import Any

from ..gamma_client import (
    discover_markets_by_category,
    get_market_token_ids,
    get_outcomes,
)
from ..logging_config import get_logger
from .fifteen_min import detect_duration_from_slug

logger = get_logger(__name__)


def _extract_slug(market: dict[str, Any]) -> str:
    for key in ("slug", "market_slug", "marketSlug", "event_slug", "eventSlug"):
        raw = market.get(key)
        if isinstance(raw, str) and raw:
            return raw
    event_obj = market.get("event")
    if isinstance(event_obj, dict):
        for key in ("slug", "event_slug", "eventSlug"):
            raw = event_obj.get(key)
            if isinstance(raw, str) and raw:
                return raw
    return ""


def _is_binary_market(market: dict[str, Any]) -> bool:
    token_ids = get_market_token_ids(market)
    outcomes = get_outcomes(market)
    return len(token_ids) == 2 and len(outcomes) >= 2


def _extract_end_ts(market: dict[str, Any]) -> float | None:
    for obj in (market, market.get("event")):
        if not isinstance(obj, dict):
            continue
        raw_end = obj.get("endDate")
        if isinstance(raw_end, str) and raw_end:
            try:
                return datetime.fromisoformat(raw_end.replace("Z", "+00:00")).timestamp()
            except ValueError:
                continue
    return None


def discover_slugs(
    category_path: str,
    durations: list[int] | None = None,
    *,
    only_active: bool = True,
    lead_time_seconds: float | None = None,
    max_pages: int = 10,
    page_size: int = 200,
) -> list[str]:
    """Discover tradable slugs for a category path.

    Filters to binary markets and optionally to known durations.
    """
    rows = discover_markets_by_category(
        category_path=category_path,
        only_active=only_active,
        max_pages=max_pages,
        page_size=page_size,
    )
    allowed_durations = set(durations or [])

    slugs: list[str] = []
    seen: set[str] = set()
    for row in rows:
        if not isinstance(row, dict):
            continue
        if not _is_binary_market(row):
            continue
        slug = _extract_slug(row)
        if not slug or slug in seen:
            continue
        if allowed_durations:
            detected = detect_duration_from_slug(slug)
            if detected is not None and detected not in allowed_durations:
                continue
        if lead_time_seconds is not None:
            end_ts = _extract_end_ts(row)
            if end_ts is None:
                continue
            time_to_expiry = end_ts - time.time()
            if time_to_expiry <= 0 or time_to_expiry > lead_time_seconds:
                continue
        seen.add(slug)
        slugs.append(slug)

    logger.info(
        "Category discovery: %s -> %d slugs",
        category_path,
        len(slugs),
    )
    return slugs
