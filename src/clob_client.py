"""CLOB client wrapper for placing Polymarket orders."""

import httpx
from typing import Any, Optional

from py_clob_client.client import ClobClient
from py_clob_client.clob_types import OrderArgs, OrderType
from py_clob_client.order_builder.constants import BUY, SELL

from .config import (
    CHAIN_ID,
    CLOB_HOST,
    FUNDER,
    PRIVATE_KEY,
    SIGNATURE_TYPE,
)
from .logging_config import get_logger

logger = get_logger(__name__)

# Common error reasons for debugging
ERROR_REASONS = {
    "INVALID_ORDER_MIN_TICK_SIZE": "Price breaks minimum tick size rules",
    "INVALID_ORDER_MIN_SIZE": "Order size below minimum threshold",
    "INVALID_ORDER_DUPLICATED": "Duplicate order already placed",
    "INVALID_ORDER_NOT_ENOUGH_BALANCE": "Insufficient balance or allowance",
    "INVALID_ORDER_EXPIRATION": "Order expiration is in the past",
    "INVALID_ORDER_ERROR": "System error inserting order",
    "INVALID_POST_ONLY_ORDER_TYPE": "Post-only only allowed with GTC/GTD",
    "INVALID_POST_ONLY_ORDER": "Post-only order would cross the book",
    "EXECUTION_ERROR": "System error during execution",
    "ORDER_DELAYED": "Order match delayed due to market conditions",
    "FOK_ORDER_NOT_FILLED_ERROR": "FOK order could not be fully filled",
    "MARKET_NOT_READY": "Market not yet accepting orders",
}


_client_cache: Optional[ClobClient] = None


def _get_live_tick_size(token_id: str) -> float:
    """Fetch the current minimum tick size for a token directly from the CLOB API.

    Returns 0.01 as a safe fallback if the request fails.
    """
    try:
        resp = httpx.get(
            f"{CLOB_HOST}/tick-size",
            params={"token_id": token_id},
            timeout=5.0,
        )
        resp.raise_for_status()
        return float(resp.json()["minimum_tick_size"])
    except Exception:
        logger.warning("Could not fetch tick size for %s – defaulting to 0.01", token_id)
        return 0.01


def _clamp_price(price: float, token_id: str) -> float:
    """Clamp *price* to the valid range ``[tick_size, 1 - tick_size]``.

    Polymarket allows 0.999 only when the market tick size is 0.001.
    When tick is 0.01, the maximum valid price is 0.99.  This helper
    fetches the live tick size and snaps the price accordingly so the
    order is never rejected by the client-side ``price_valid()`` check.
    """
    tick = _get_live_tick_size(token_id)
    min_price = round(tick, 10)
    max_price = round(1.0 - tick, 10)

    if price < min_price or price > max_price:
        clamped = max(min_price, min(price, max_price))
        logger.warning(
            "Price %.4f out of valid range [%.4f, %.4f] for tick_size=%.4f "
            "– clamping to %.4f (token=%s)",
            price, min_price, max_price, tick, clamped, token_id[:20],
        )
        return clamped

    return price


def create_clob_client() -> Optional[ClobClient]:
    """Return a cached CLOB client, clearing its internal tick-size cache on
    each call so it always re-fetches the current minimum_tick_size."""
    global _client_cache
    if not PRIVATE_KEY or not FUNDER:
        logger.error("PRIVATE_KEY and FUNDER must be set in .env")
        return None

    try:
        if _client_cache is None:
            client = ClobClient(
                CLOB_HOST,
                key=PRIVATE_KEY,
                chain_id=CHAIN_ID,
                signature_type=SIGNATURE_TYPE,
                funder=FUNDER,
            )
            client.set_api_creds(client.create_or_derive_api_creds())
            _client_cache = client
            logger.info("CLOB client initialized (host=%s)", CLOB_HOST)

        # Clear the internal tick-size cache so each order re-fetches the
        # current minimum_tick_size from the API.
        tick_cache = _client_cache.__dict__.get("_ClobClient__tick_sizes")
        if tick_cache is not None:
            tick_cache.clear()
        return _client_cache
    except Exception:
        logger.exception("Failed to create CLOB client")
        return None


def place_limit_order(
    token_id: str,
    price: float,
    size: float = 1.0,
    side: str = "BUY",
) -> Optional[dict[str, Any]]:
    """
    Place a limit order on the CLOB.

    Args:
        token_id: CLOB token ID (ERC1155)
        price: Limit price (0.0 - 1.0)
        size: Order size in shares
        side: 'BUY' or 'SELL'

    Returns:
        API response dict with success, orderId, errorMsg, status; or None on client error.
    """
    client = create_clob_client()
    if client is None:
        return None

    side_const = BUY if side.upper() == "BUY" else SELL

    logger.info(
        "Placing order: token_id=%s, price=%.4f, size=%.2f, side=%s",
        token_id,
        price,
        size,
        side,
    )

    try:
        # Clamp price to the live valid range before the client validates it.
        # e.g. 0.999 is only valid when tick_size=0.001; with tick_size=0.01
        # the max is 0.99.  _clamp_price fetches the current tick and adjusts.
        price = _clamp_price(price, token_id)

        order_args = OrderArgs(
            price=price,
            size=size,
            side=side_const,
            token_id=token_id,
        )
        signed_order = client.create_order(order_args)
        resp = client.post_order(signed_order, OrderType.GTC)

        success = resp.get("success", False)
        error_msg = resp.get("errorMsg", "")
        order_id = resp.get("orderId", "")
        status = resp.get("status", "")

        if success:
            logger.info(
                "Order placed: orderId=%s, status=%s",
                order_id,
                status,
            )
        else:
            reason = ERROR_REASONS.get(error_msg, error_msg or "Unknown error")
            logger.warning(
                "Order failed: success=%s, errorMsg=%s, reason=%s, orderId=%s",
                success,
                error_msg,
                reason,
                order_id,
            )

        logger.debug("Raw API response: %s", resp)
        return resp

    except Exception:
        logger.exception(
            "Order placement failed: token_id=%s, price=%s, size=%s",
            token_id,
            price,
            size,
        )
        return None
