"""Discovery for non-crypto up-or-down markets."""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

import pytz
import requests

from ..config import GAMMA_API
from ..logging_config import get_logger

logger = get_logger(__name__)

_EST = pytz.timezone("US/Eastern")

# Explicitly excluded: existing crypto path is stable and unchanged.
_CRYPTO_PREFIXES = {
    "bitcoin",
    "ethereum",
    "solana",
    "xrp",
    "dogecoin",
    "hyperliquid",
    "bnb",
    "btc",
    "eth",
    "sol",
    "doge",
    "hype",
}


def _parse_end_ts(end_date: str | None) -> int | None:
    if not end_date:
        return None
    try:
        return int(datetime.fromisoformat(end_date.replace("Z", "+00:00")).timestamp())
    except Exception:
        return None


def _parse_volume(v: Any) -> float:
    try:
        return float(v)
    except Exception:
        return 0.0


def _bucket_key(ticker: str, variant: str, end_ts: int | None) -> tuple[str, str, str]:
    if end_ts is None:
        date_key = "unknown"
    else:
        dt_est = datetime.fromtimestamp(end_ts, tz=timezone.utc).astimezone(_EST)
        date_key = dt_est.strftime("%Y-%m-%d")
    return ticker.upper(), variant, date_key


def _pick_best(existing: dict[str, Any], candidate: dict[str, Any]) -> dict[str, Any]:
    """Pick deterministic winner: lower timestamp, tie-break highest volume."""
    ex_ts = existing.get("end_ts")
    ca_ts = candidate.get("end_ts")
    ex_vol = _parse_volume(existing.get("volume"))
    ca_vol = _parse_volume(candidate.get("volume"))

    # Prefer records with a timestamp.
    if ex_ts is None and ca_ts is not None:
        return candidate
    if ca_ts is None and ex_ts is not None:
        return existing

    # Lower timestamp first.
    if ca_ts is not None and ex_ts is not None:
        if ca_ts < ex_ts:
            return candidate
        if ca_ts > ex_ts:
            return existing

    # Tie-break on highest volume.
    if ca_vol > ex_vol:
        return candidate
    return existing


def discover_non_crypto_up_or_down_markets(
    *,
    ticker_filter: set[str] | None = None,
    max_pages: int = 20,
    page_size: int = 100,
) -> list[dict[str, Any]]:
    """Discover active non-crypto up-or-down markets from Gamma.

    Returns selected markets after deterministic per-bucket ranking:
    - primary: lower end_ts
    - secondary: higher volume
    """
    wanted = {t.upper() for t in ticker_filter} if ticker_filter else None
    selected_by_bucket: dict[tuple[str, str, str], dict[str, Any]] = {}

    for page in range(max_pages):
        offset = page * page_size
        try:
            resp = requests.get(
                f"{GAMMA_API}/events",
                params={"active": "true", "limit": page_size, "offset": offset},
                timeout=30,
            )
            resp.raise_for_status()
            events = resp.json()
        except Exception:
            logger.exception("[STOCK_DISCOVERY] Failed fetching events page offset=%d", offset)
            break

        if not events:
            break

        for event in events:
            slug = str(event.get("slug", ""))
            slug_lower = slug.lower()
            if "-up-or-down-" not in slug_lower:
                continue

            prefix = slug_lower.split("-", 1)[0]
            if prefix in _CRYPTO_PREFIXES:
                continue

            ticker = prefix.upper()
            if wanted and ticker not in wanted:
                continue

            variant = "opens" if "-opens-up-or-down-" in slug_lower else "close"
            end_date = event.get("endDate")
            end_ts = _parse_end_ts(end_date)
            market = (event.get("markets") or [{}])[0]
            volume = market.get("volume", event.get("volume", 0))

            candidate = {
                "slug": slug,
                "ticker": ticker,
                "variant": variant,
                "title": event.get("title", ""),
                "end_date": end_date,
                "end_ts": end_ts,
                "volume": volume,
                "active": bool(event.get("active", False)),
                "closed": bool(event.get("closed", False)),
            }

            bucket = _bucket_key(ticker, variant, end_ts)
            current = selected_by_bucket.get(bucket)
            selected_by_bucket[bucket] = candidate if current is None else _pick_best(current, candidate)

        if len(events) < page_size:
            break

    out = list(selected_by_bucket.values())
    out.sort(key=lambda m: ((m.get("end_ts") or 0), -_parse_volume(m.get("volume")), m.get("slug", "")))
    return out
