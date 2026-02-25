"""Order execution with deduplication for the endgame sweep strategy.

Keeps an in-memory set of already-traded ``(slug, token_id)`` pairs so the
same market window is never bet on twice.
"""

from typing import Any, Optional

from ..clob_client import place_limit_order
from ..logging_config import get_logger
from ..utils.timestamps import format_slug_with_est_time

logger = get_logger(__name__)

# ANSI Color Codes for console output
C_GREEN = "\033[92m"
C_YELLOW = "\033[93m"
C_RESET = "\033[0m"

# In-memory dedup set – tracks (slug, token_id) combos already ordered
_executed_orders: set[tuple[str, str]] = set()


def has_already_ordered(slug: str, token_id: str) -> bool:
    """Return *True* if we have already placed an order for this market window."""
    return (slug, token_id) in _executed_orders


def record_order(slug: str, token_id: str) -> None:
    """Mark a ``(slug, token_id)`` pair as already ordered."""
    _executed_orders.add((slug, token_id))


def reset_orders() -> None:
    """Clear the dedup set (useful for tests)."""
    _executed_orders.clear()


def execute_sweep_order(
    token_id: str,
    price: float,
    size: float,
    slug: str,
    outcome: str,
    dry_run: bool = False,
    tick_size: Optional[float] = None,
) -> Optional[dict[str, Any]]:
    """Place a limit BUY order as part of the endgame sweep strategy.

    Includes deduplication: if the same ``(slug, token_id)`` has already been
    ordered in this session, the call is silently skipped.

    Args:
        token_id: CLOB token ID for the outcome to buy.
        price: Limit price (0.0 – 1.0).
        size: Order size in shares (use minimum for testing).
        slug: Market slug (used for dedup and logging).
        outcome: Human-readable outcome label (e.g. "Up").
        dry_run: If *True*, log but do **not** submit the order.
        tick_size: Known tick size from the WebSocket event. Passed directly
                   to ``place_limit_order`` to skip the HTTP tick-size fetch.

    Returns:
        API response dict on success, or *None* on skip/failure.
    """
    formatted_slug = format_slug_with_est_time(slug)

    # ── Dedup check ───────────────────────────────────────────────────────
    if has_already_ordered(slug, token_id):
        logger.info(
            "[TRADE] DEDUP: Already ordered %s/%s for %s – skipping",
            outcome,
            token_id[:20],
            formatted_slug,
        )
        return None

    # ── Dry-run ───────────────────────────────────────────────────────────
    if dry_run:
        logger.info(
            "%s[TRADE] DRY-RUN: Would place BUY %s @ %.4f x %.2f for %s (token=%s…)%s",
            C_YELLOW,
            outcome,
            price,
            size,
            formatted_slug,
            token_id[:20],
            C_RESET,
        )
        record_order(slug, token_id)
        return {"dry_run": True, "slug": slug, "outcome": outcome}

    # ── Live order ────────────────────────────────────────────────────────
    logger.info(
        "[TRADE] SWEEP ORDER: BUY %s @ %.4f x %.2f for %s (token=%s…)",
        outcome,
        price,
        size,
        formatted_slug,
        token_id[:20],
    )

    resp = place_limit_order(
        token_id=token_id,
        price=price,
        size=size,
        side="BUY",
        tick_size=tick_size,
    )

    if resp is not None:
        success = resp.get("success", False)
        if success:
            logger.info(
                "%s[TRADE] ORDER CONFIRMED: orderId=%s for %s/%s%s",
                C_GREEN,
                resp.get("orderId", "?"),
                formatted_slug,
                outcome,
                C_RESET,
            )
        else:
            logger.warning(
                "ORDER REJECTED: %s for %s/%s",
                resp.get("errorMsg", "unknown"),
                formatted_slug,
                outcome,
            )
        # Record regardless of success to avoid retry-storm
        record_order(slug, token_id)
    else:
        logger.error("ORDER FAILED: CLOB client returned None for %s/%s", slug, outcome)

    return resp
