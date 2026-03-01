"""CLOB client wrapper for placing Polymarket orders."""

import httpx
from typing import Any, Optional

from py_clob_client.client import ClobClient
from py_clob_client.clob_types import OrderArgs, OrderType
from py_clob_client.order_builder.constants import BUY, SELL
from py_clob_client.exceptions import PolyApiException

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


def create_clob_client() -> Optional[ClobClient]:
    """Return a cached CLOB client.

    The internal tick-size cache is intentionally *not* cleared on each
    call.  Our hot path (``place_limit_order``) already receives the
    authoritative tick size from the WebSocket ``tick_size_change`` event
    and clamps the price in ``_clamp_price`` before the library ever
    touches it, so the extra HTTP round-trip was pure waste.
    """
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

        return _client_cache
    except Exception:
        logger.exception("Failed to create CLOB client")
        return None


def get_order_status(order_id: str) -> Optional[dict[str, Any]]:
    """Fetch a single order's current state from the CLOB API.

    Returns the raw order dict (with fields like ``status``,
    ``size_matched``, ``price``, etc.) or ``None`` on failure.
    """
    client = create_clob_client()
    if client is None:
        return None

    try:
        return client.get_order(order_id)
    except Exception:
        logger.debug("get_order failed for %s", order_id[:16], exc_info=True)
        return None


def place_limit_order(
    token_id: str,
    price: float,
    size: float = 1.0,
    side: str = "BUY",
    tick_size: Optional[float] = None,
) -> Optional[dict[str, Any]]:
    """
    Place a limit order on the CLOB.

    Args:
        token_id: CLOB token ID (ERC1155)
        price: Limit price (0.0 - 1.0)
        size: Order size in shares
        side: 'BUY' or 'SELL'
        tick_size: Known tick size from WebSocket event. When provided, skips
                   the HTTP fetch and uses this value directly for price clamping.

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
        order_args = OrderArgs(
            price=price,
            size=size,
            side=side_const,
            token_id=token_id,
        )
        signed_order = client.create_order(order_args)
        resp = client.post_order(signed_order, OrderType.GTC)

        success = resp.get("success", False)
        error_msg = resp.get("errorMsg") or resp.get("error_msg") or ""
        order_id = resp.get("orderId") or resp.get("orderID") or ""
        status = resp.get("status") or resp.get("order_status") or ""

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

    except PolyApiException as exc:
        # Robust extraction: PolyApiException usually stores its data in 'error_message'
        # but the string representation often contains exactly what we need.
        error_msg = str(exc)
        
        # 1. Try to get it from attributes
        raw_error = getattr(exc, "error_message", None) or getattr(exc, "message", None)
        
        # 2. Fallback to regex if we don't have a clean dict yet
        if not isinstance(raw_error, dict):
            import re
            # Look for error_message={'error': '...'} or similar in the string
            match = re.search(r"error_message=({.*?})", error_msg)
            if match:
                try:
                    import ast
                    raw_error = ast.literal_eval(match.group(1))
                except Exception:
                    pass

        # 3. Extract the final human-readable string
        if isinstance(raw_error, dict):
            error_msg = (
                raw_error.get("error") 
                or raw_error.get("errorMsg") 
                or raw_error.get("message") 
                or str(raw_error)
            )
        elif error_msg.startswith("PolyApiException"):
            # If we still have the class name, try a final cleanup
            if "error_message=" in error_msg:
                error_msg = error_msg.split("error_message=")[-1].strip(" ]")
        
        logger.warning("[ORDER] API rejection string: %s", error_msg)
        return {
            "success": False,
            "errorMsg": error_msg,
        }

    except Exception as exc:
        # Clean up the error message for the dashboard
        error_msg = str(exc)
        # Remove common technical prefixes
        if error_msg.startswith("Exception: "):
            error_msg = error_msg[11:]
        elif ": " in error_msg and error_msg.split(": ")[0].endswith("Exception"):
            error_msg = error_msg.split(": ", 1)[1]
            
        logger.warning(
            "[ORDER] Rejected by validation/exception: %s (token_id=%s, price=%s, size=%s)",
            error_msg,
            token_id,
            price,
            size,
        )
        return {
            "success": False,
            "errorMsg": error_msg,
        }
