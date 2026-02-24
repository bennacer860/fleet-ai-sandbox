"""Gamma API client for fetching Polymarket events and markets."""

import json
import time
import requests
from typing import Any, Optional

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

