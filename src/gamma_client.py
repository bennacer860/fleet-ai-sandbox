"""Gamma API client for fetching Polymarket events and markets."""

import json
import re
import time
from typing import Any, Optional

import requests

from .config import GAMMA_API
from .logging_config import get_logger

logger = get_logger(__name__)


def fetch_event_by_slug(slug: str) -> Optional[dict[str, Any]]:
    """
    Fetch an event by its slug from the Gamma API.

    Args:
        slug: Event slug (e.g. from polymarket.com/event/{slug})

    Returns:
        Event dict with markets, endDate, etc., or None if not found.
    """
    url = f"{GAMMA_API}/events/slug/{slug}"
    logger.debug("Fetching event: slug=%s", slug)
    try:
        t0 = time.perf_counter()
        resp = requests.get(url, timeout=30)
        resp.raise_for_status()
        logger.debug("Raw API response: %s", resp.text)
        data = resp.json()
        elapsed_ms = (time.perf_counter() - t0) * 1000
        markets = data.get("markets") or []
        end_date = data.get("endDate")
        logger.debug(
            "Event fetched: slug=%s, endDate=%s, market_count=%d, latency_ms=%.0f",
            slug,
            end_date,
            len(markets),
            elapsed_ms,
        )
        return data
    except requests.exceptions.HTTPError as e:
        if e.response is not None and e.response.status_code == 404:
            logger.error("Event not found: slug=%s. Check the Polymarket URL.", slug)
        else:
            logger.exception("Failed to fetch event: slug=%s", slug)
        return None
    except Exception:
        logger.exception("Failed to fetch event: slug=%s", slug)
        return None


def fetch_markets_page(params: dict[str, Any], timeout: float = 30.0) -> list[dict[str, Any]]:
    """Fetch one Gamma ``/markets`` page with arbitrary query parameters."""
    url = f"{GAMMA_API}/markets"
    try:
        resp = requests.get(url, params=params, timeout=timeout)
        resp.raise_for_status()
        data = resp.json()
        if isinstance(data, list):
            return [row for row in data if isinstance(row, dict)]
        logger.warning("Unexpected /markets response type: %s", type(data).__name__)
        return []
    except Exception:
        logger.exception("Failed to fetch /markets page params=%s", params)
        return []


def fetch_events_page(params: dict[str, Any], timeout: float = 30.0) -> list[dict[str, Any]]:
    """Fetch one Gamma ``/events`` page with arbitrary query parameters."""
    url = f"{GAMMA_API}/events"
    try:
        resp = requests.get(url, params=params, timeout=timeout)
        resp.raise_for_status()
        data = resp.json()
        if isinstance(data, list):
            return [row for row in data if isinstance(row, dict)]
        logger.warning("Unexpected /events response type: %s", type(data).__name__)
        return []
    except Exception:
        logger.exception("Failed to fetch /events page params=%s", params)
        return []


def _stringify_values(value: Any) -> list[str]:
    """Flatten nested values into lowercase strings for fuzzy matching."""
    out: list[str] = []
    if value is None:
        return out
    if isinstance(value, str):
        out.append(value.lower())
        return out
    if isinstance(value, (int, float, bool)):
        out.append(str(value).lower())
        return out
    if isinstance(value, dict):
        for v in value.values():
            out.extend(_stringify_values(v))
        return out
    if isinstance(value, list):
        for v in value:
            out.extend(_stringify_values(v))
        return out
    return out


def _market_matches_category(market: dict[str, Any], category_path: str) -> bool:
    """Return True when market category/tag fields match the target path."""
    target = _normalize_category_path(category_path)
    if not target:
        return True

    target_segments = _path_segments(target)
    if not target_segments:
        return False

    # Common Gamma fields observed across market/event payloads.
    candidate_fields = [
        "category",
        "categorySlug",
        "category_slug",
        "tag",
        "tagSlug",
        "tag_slug",
        "tags",
        "event",
        "eventCategory",
        "eventTags",
        "eventMetadata",
    ]
    haystack: list[str] = []
    for key in candidate_fields:
        if key in market:
            haystack.extend(_stringify_values(market.get(key)))

    event_obj = market.get("event")
    if isinstance(event_obj, dict):
        for key in candidate_fields:
            if key in event_obj:
                haystack.extend(_stringify_values(event_obj.get(key)))

    found_category_metadata = False
    metadata_terms: set[str] = set()
    for raw in haystack:
        candidate = _normalize_category_path(raw)
        if not candidate:
            continue
        found_category_metadata = True
        metadata_terms.add(candidate)
        metadata_terms.update(_path_segments(candidate))
        metadata_terms.update(_keyword_tokens(candidate))
        candidate_segments = _path_segments(candidate)
        if _contains_path_segments(candidate_segments, target_segments):
            return True

    if found_category_metadata and _metadata_matches_target_segments(
        metadata_terms, target_segments
    ):
        return True

    # Gamma /markets often returns null category/tag fields. In that case,
    # fall back to keyword matching against human-facing text fields.
    if not found_category_metadata:
        return _market_matches_category_keywords(market, target_segments)

    return False


def _normalize_category_path(raw: str) -> str:
    """Normalize category-like strings for robust path comparisons."""
    lowered = raw.strip().lower().replace("\\", "/")
    lowered = re.sub(r"\s+", "-", lowered)
    lowered = re.sub(r"/+", "/", lowered)
    lowered = lowered.strip("/")
    return lowered


def _path_segments(path: str) -> list[str]:
    return [segment for segment in path.split("/") if segment]


def _contains_path_segments(candidate: list[str], target: list[str]) -> bool:
    """Return True if target path segments appear contiguously in candidate."""
    if len(candidate) < len(target):
        return False
    target_len = len(target)
    for idx in range(len(candidate) - target_len + 1):
        if candidate[idx : idx + target_len] == target:
            return True
    return False


def _market_matches_category_keywords(
    market: dict[str, Any], target_segments: list[str]
) -> bool:
    """Keyword fallback used when category metadata is missing from payload."""
    if not target_segments:
        return False

    # Require the leaf segment keywords (e.g. "temperature" from weather/temperature).
    leaf_keywords = _keyword_tokens(target_segments[-1])
    if not leaf_keywords:
        return False

    text_words = _market_text_words(market)
    if not text_words:
        return False

    return all(keyword in text_words for keyword in leaf_keywords)


def _keyword_tokens(segment: str) -> set[str]:
    return {tok for tok in re.findall(r"[a-z0-9]+", segment.lower()) if len(tok) >= 3}


def _market_text_words(market: dict[str, Any]) -> set[str]:
    candidates: list[str] = []
    for key in ("slug", "marketSlug", "event_slug", "eventSlug", "question", "title", "description"):
        value = market.get(key)
        if isinstance(value, str) and value:
            candidates.append(value.lower())
    event_obj = market.get("event")
    if isinstance(event_obj, dict):
        for key in ("slug", "title", "question", "description"):
            value = event_obj.get(key)
            if isinstance(value, str) and value:
                candidates.append(value.lower())
    words: set[str] = set()
    for text in candidates:
        words.update(re.findall(r"[a-z0-9]+", text))
    return words


def _segment_aliases(segment: str) -> set[str]:
    base = _normalize_category_path(segment)
    aliases: dict[str, set[str]] = {
        "temperature": {"temperature", "temp", "global-temp", "globaltemp"},
        "weather": {"weather", "weather-science"},
    }
    out = {base}
    out.update(aliases.get(base, set()))
    normalized: set[str] = set()
    for item in out:
        norm = _normalize_category_path(item)
        if not norm:
            continue
        normalized.add(norm)
        normalized.update(_path_segments(norm))
        normalized.update(_keyword_tokens(norm))
    return normalized


def _metadata_matches_target_segments(
    metadata_terms: set[str], target_segments: list[str]
) -> bool:
    """Allow split matching across tag/category terms (e.g. weather + global-temp)."""
    if not target_segments:
        return False
    if not metadata_terms:
        return False
    for segment in target_segments:
        aliases = _segment_aliases(segment)
        if not any(alias in metadata_terms for alias in aliases):
            return False
    return True


def _extract_market_slug(market: dict[str, Any]) -> str:
    """Extract best-available slug from a market/event payload."""
    event_obj = market.get("event")
    if isinstance(event_obj, dict):
        for key in ("slug", "event_slug", "eventSlug", "ticker"):
            raw = event_obj.get(key)
            if isinstance(raw, str) and raw:
                return raw
    for key in ("slug", "market_slug", "marketSlug", "event_slug", "eventSlug"):
        raw = market.get(key)
        if isinstance(raw, str) and raw:
            return raw
    return ""


def _is_market_active(market: dict[str, Any]) -> bool:
    """Best-effort active/open filter for discovery results."""
    # explicit closed/ended/archived flags override active hints
    for key in ("closed", "ended", "archived", "isArchived"):
        if bool(market.get(key)):
            return False
    if "active" in market:
        return bool(market.get("active"))
    return True


def discover_markets_by_category(
    category_path: str,
    *,
    only_active: bool = True,
    max_pages: int = 10,
    page_size: int = 200,
) -> list[dict[str, Any]]:
    """Discover market payloads under a category/tag path.

    Tries server-side filtering first (different Gamma deployments expose
    different parameter names), then falls back to client-side filtering.
    """
    target = category_path.strip("/")
    if not target:
        return []

    filter_key_candidates = ("tag_slug", "category_slug", "category", "tag")
    results: list[dict[str, Any]] = []

    # Try server-side category/tag filters first.
    for filter_key in filter_key_candidates:
        server_rows: list[dict[str, Any]] = []
        for page in range(max_pages):
            params: dict[str, Any] = {
                "limit": page_size,
                "offset": page * page_size,
                filter_key: target,
            }
            if only_active:
                params.update({"active": "true", "closed": "false"})
            rows = fetch_markets_page(params)
            if not rows:
                break
            server_rows.extend(rows)
            if len(rows) < page_size:
                break
        if server_rows:
            filtered_server_rows = [
                row for row in server_rows if _market_matches_category(row, target)
            ]
            if filtered_server_rows:
                results = filtered_server_rows
                break
            logger.warning(
                "Server-side category filter %s=%s returned rows but none matched client validation; trying next strategy",
                filter_key,
                target,
            )

    # Fallback: fetch active markets pages and filter client-side.
    if not results:
        fallback_rows: list[dict[str, Any]] = []
        for page in range(max_pages):
            params = {"limit": page_size, "offset": page * page_size}
            if only_active:
                params.update({"active": "true", "closed": "false"})
            rows = fetch_markets_page(params)
            if not rows:
                break
            fallback_rows.extend(rows)
            if len(rows) < page_size:
                break
        results = [row for row in fallback_rows if _market_matches_category(row, target)]

    seen: set[str] = set()
    normalized: list[dict[str, Any]] = []
    for row in results:
        if not isinstance(row, dict):
            continue
        slug = _extract_market_slug(row)
        if not slug or slug in seen:
            continue
        if only_active and not _is_market_active(row):
            continue
        seen.add(slug)
        normalized.append(row)

    logger.info(
        "Discovered %d markets for category=%s",
        len(normalized),
        target,
    )
    return normalized


def discover_event_slugs_by_category(
    category_path: str,
    *,
    only_active: bool = True,
    max_pages: int = 10,
    page_size: int = 200,
) -> list[str]:
    """Discover unique event/market slugs under a category/tag path."""
    markets = discover_markets_by_category(
        category_path,
        only_active=only_active,
        max_pages=max_pages,
        page_size=page_size,
    )
    slugs: list[str] = []
    seen: set[str] = set()
    for market in markets:
        slug = _extract_market_slug(market)
        if not slug or slug in seen:
            continue
        seen.add(slug)
        slugs.append(slug)
    return slugs


def discover_events_by_category(
    category_path: str,
    *,
    only_active: bool = True,
    max_pages: int = 10,
    page_size: int = 200,
) -> list[dict[str, Any]]:
    """Discover event payloads under a category/tag path."""
    target = category_path.strip("/")
    if not target:
        return []

    filter_key_candidates = ("tag_slug", "category_slug", "category", "tag")
    results: list[dict[str, Any]] = []

    for filter_key in filter_key_candidates:
        server_rows: list[dict[str, Any]] = []
        for page in range(max_pages):
            params: dict[str, Any] = {
                "limit": page_size,
                "offset": page * page_size,
                filter_key: target,
            }
            if only_active:
                params.update({"active": "true", "closed": "false"})
            rows = fetch_events_page(params)
            if not rows:
                break
            server_rows.extend(rows)
            if len(rows) < page_size:
                break
        if server_rows:
            filtered_server_rows = [
                row for row in server_rows if _market_matches_category(row, target)
            ]
            if filtered_server_rows:
                results = filtered_server_rows
                break

    if not results:
        fallback_rows: list[dict[str, Any]] = []
        for page in range(max_pages):
            params = {"limit": page_size, "offset": page * page_size}
            if only_active:
                params.update({"active": "true", "closed": "false"})
            rows = fetch_events_page(params)
            if not rows:
                break
            fallback_rows.extend(rows)
            if len(rows) < page_size:
                break
        results = [row for row in fallback_rows if _market_matches_category(row, target)]

    seen: set[str] = set()
    normalized: list[dict[str, Any]] = []
    for row in results:
        slug = _extract_market_slug(row)
        if not slug or slug in seen:
            continue
        if only_active and not _is_market_active(row):
            continue
        seen.add(slug)
        normalized.append(row)

    logger.info(
        "Discovered %d events for category=%s",
        len(normalized),
        target,
    )
    return normalized


def get_market_token_ids(market: dict[str, Any]) -> list[str]:
    """
    Extract CLOB token IDs from a market.

    clobTokenIds can be: JSON string '["id1","id2"]', list, or pipe-separated "id1|id2".
    """
    raw = market.get("clobTokenIds")
    if raw is None:
        return []
    if isinstance(raw, list):
        return [str(x) for x in raw]
    if isinstance(raw, str):
        try:
            parsed = json.loads(raw)
            if isinstance(parsed, list):
                return [str(x) for x in parsed]
        except json.JSONDecodeError:
            pass
        return [x.strip() for x in raw.split("|") if x.strip()]
    return []


def get_outcomes(market: dict[str, Any]) -> list[str]:
    """Extract outcome labels from a market (e.g. ['Up', 'Down'] or ['Yes', 'No'])."""
    raw = market.get("outcomes")
    if raw is None:
        return []
    if isinstance(raw, list):
        return [str(x) for x in raw]
    if isinstance(raw, str):
        try:
            parsed = json.loads(raw)
            if isinstance(parsed, list):
                return [str(x) for x in parsed]
        except json.JSONDecodeError:
            pass
        return [x.strip() for x in raw.split(",") if x.strip()]
    return []


def get_outcome_prices(market: dict[str, Any]) -> list[float]:
    """Extract outcome prices from a market (e.g. [1.0, 0.0] when resolved)."""
    raw = market.get("outcomePrices")
    if raw is None:
        return []
    if isinstance(raw, list):
        return [float(x) for x in raw]
    if isinstance(raw, str):
        try:
            parsed = json.loads(raw)
            if isinstance(parsed, list):
                return [float(x) for x in parsed]
        except (json.JSONDecodeError, ValueError, TypeError):
            pass
        return [float(x.strip()) for x in raw.split(",") if x.strip()]
    return []


def resolve_token_for_direction(market: dict[str, Any], direction: str) -> Optional[str]:
    """
    Map direction (up/down/yes/no) to the corresponding token_id.

    Args:
        market: Market dict with clobTokenIds and outcomes
        direction: One of 'up', 'down', 'yes', 'no'

    Returns:
        token_id for the chosen outcome, or None if invalid.
    """
    token_ids = get_market_token_ids(market)
    outcomes = get_outcomes(market)
    direction = direction.lower()

    if len(token_ids) != 2:
        logger.error("Expected 2 token IDs, got %d: outcomes=%s", len(token_ids), outcomes)
        return None
    if len(outcomes) != 2:
        logger.error("Expected 2 outcomes, got %d: outcomes=%s", len(outcomes), outcomes)
        return None

    first_outcome = outcomes[0].lower()
    second_outcome = outcomes[1].lower()

    if direction in ("up", "yes") or direction == first_outcome:
        token_id = token_ids[0]
        logger.debug("Token resolution: direction=%s -> outcomes=%s -> token_id=%s", direction, outcomes, token_id)
        return token_id
    if direction in ("down", "no") or direction == second_outcome:
        token_id = token_ids[1]
        logger.debug("Token resolution: direction=%s -> outcomes=%s -> token_id=%s", direction, outcomes, token_id)
        return token_id

    logger.error("Unknown direction=%s. Expected up/down/yes/no or outcome names %s", direction, outcomes)
    return None


def get_winning_token_id(market: dict[str, Any]) -> Optional[str]:
    """
    Get the token_id of the winning outcome from a resolved market.

    outcomePrices will be "1,0" or "0,1" when resolved. The position with 1.0 is the winner.
    """
    token_ids = get_market_token_ids(market)
    prices = get_outcome_prices(market)

    if len(token_ids) != 2 or len(prices) != 2:
        logger.error("Invalid market: token_ids=%s, prices=%s", token_ids, prices)
        return None

    for i, p in enumerate(prices):
        if p >= 0.99:  # Allow for float precision
            winner = token_ids[i]
            outcomes = get_outcomes(market)
            logger.debug("Winning outcome: index=%d, outcome=%s, token_id=%s", i, outcomes[i] if i < len(outcomes) else "?", winner)
            return winner

    logger.error("No winning outcome found: outcomePrices=%s", prices)
    return None


def is_market_ended(market: dict[str, Any]) -> bool:
    """Check if the market has ended/resolved."""
    return bool(market.get("ended") or market.get("closed"))

