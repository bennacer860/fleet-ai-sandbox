"""CLOB client wrapper for placing Polymarket orders (v2 API)."""

import time
from dataclasses import dataclass

import httpx
from typing import Any, Optional

from py_clob_client_v2 import (
    ClobClient,
    ApiCreds,
    OrderArgs,
    OrderType,
    PartialCreateOrderOptions,
    TickSize,
    BalanceAllowanceParams,
    OpenOrderParams,
    Side,
)
from py_clob_client_v2.exceptions import PolyApiException

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

# Map float tick sizes → the TickSize literal strings the library expects
_TICK_SIZE_MAP: dict[float, TickSize] = {
    0.1: "0.1",
    0.01: "0.01",
    0.001: "0.001",
    0.0001: "0.0001",
}


_client_cache: Optional[ClobClient] = None
_http_patched = False


def _patch_http_helpers() -> None:
    """Monkey-patch py_clob_client_v2's HTTP helper to preserve real error details.

    The library's ``request()`` function catches ``httpx.RequestError`` and
    replaces it with the generic "Request exception!" string, losing the
    actual network error (e.g. timeout, DNS failure, connection refused).
    """
    global _http_patched
    if _http_patched:
        return

    try:
        import py_clob_client_v2.http_helpers.helpers as http_mod
        logger.debug("[PATCH] py_clob_client_v2 HTTP helper available for patching")
    except ImportError:
        logger.debug("[PATCH] py_clob_client_v2 HTTP helper not patchable")
        return

    _http_patched = True


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
    """Return a cached CLOB v2 client.

    The internal tick-size cache is intentionally *not* cleared on each
    call.  Our hot path (``place_limit_order``) already receives the
    authoritative tick size from the WebSocket ``tick_size_change`` event
    and passes it via ``PartialCreateOrderOptions`` so the library never
    needs its own HTTP round-trip.
    """
    global _client_cache
    if not PRIVATE_KEY or not FUNDER:
        logger.error("PRIVATE_KEY and FUNDER must be set in .env")
        return None

    try:
        if _client_cache is None:
            # Step 1: Create client for L1 auth (derive API key)
            temp_client = ClobClient(
                host=CLOB_HOST,
                chain_id=CHAIN_ID,
                key=PRIVATE_KEY,
                signature_type=SIGNATURE_TYPE,
                funder=FUNDER,
            )
            creds = temp_client.create_or_derive_api_key()
            
            # Step 2: Create full client with L2 auth (API creds)
            client = ClobClient(
                host=CLOB_HOST,
                chain_id=CHAIN_ID,
                key=PRIVATE_KEY,
                creds=creds,
                signature_type=SIGNATURE_TYPE,
                funder=FUNDER,
            )
            _client_cache = client
            logger.info("CLOB v2 client initialized (host=%s)", CLOB_HOST)

            _patch_http_helpers()

        return _client_cache
    except Exception:
        logger.exception("Failed to create CLOB v2 client")
        return None


@dataclass
class BookSnapshot:
    """REST-fetched book state for a single token."""
    best_bid: float
    best_ask: float
    bids: tuple[tuple[float, float], ...]
    asks: tuple[tuple[float, float], ...]


def precache_token_data(token_ids: list[str]) -> dict[str, BookSnapshot]:
    """Pre-fetch and cache neg_risk, fee_rate, AND min_order_size at market-add time.

    This populates the py_clob_client internal caches so that
    ``create_order()`` never needs to make HTTP calls for these values
    during the latency-critical order-placement path.

    Also caches ``min_order_size`` from the order book so the strategy
    can look it up instantly instead of making a blocking CLOB call.

    Returns a dict mapping token_id -> BookSnapshot from the REST book
    response, so callers can seed best_prices and publish initial events.
    """
    client = create_clob_client()
    if client is None:
        return {}

    snapshots: dict[str, BookSnapshot] = {}

    for token_id in token_ids:
        try:
            neg_risk = client.get_neg_risk(token_id)
            fee_rate = client.get_fee_rate_bps(token_id)

            try:
                book = client.get_order_book(token_id)
                if book.min_order_size:
                    mos = float(book.min_order_size)
                    _min_order_size_cache[token_id] = mos
                raw_bids = book.bids or []
                raw_asks = book.asks or []
                bids = tuple(
                    (float(b.price), float(b.size))
                    for b in sorted(raw_bids, key=lambda x: float(x.price), reverse=True)[:10]
                )
                asks = tuple(
                    (float(a.price), float(a.size))
                    for a in sorted(raw_asks, key=lambda x: float(x.price))[:10]
                )
                best_bid = max((p for p, _ in bids), default=0.0)
                best_ask = min((p for p, _ in asks), default=0.0)
                if best_bid > 0 or best_ask > 0:
                    snapshots[token_id] = BookSnapshot(
                        best_bid=best_bid, best_ask=best_ask,
                        bids=bids, asks=asks,
                    )
            except Exception:
                logger.debug("[PRECACHE] Order book fetch failed for %s…", token_id[:20])

            logger.debug(
                "[PRECACHE] token=%s… neg_risk=%s, fee_rate=%s, min_order_size=%s",
                token_id[:20], neg_risk, fee_rate,
                _min_order_size_cache.get(token_id, "?"),
            )
        except Exception:
            logger.warning("[PRECACHE] Failed for token %s…", token_id[:20])

    return snapshots


# ── Min order size cache ──────────────────────────────────────────────────

_min_order_size_cache: dict[str, float] = {}

FALLBACK_MIN_ORDER_SIZE = 5.0


def get_cached_min_order_size(token_id: str) -> float:
    """Return cached min_order_size for a token, or FALLBACK_MIN_ORDER_SIZE.

    This is an instant dict lookup — zero HTTP calls.  The cache is
    populated by ``precache_token_data()`` at market-init time.
    """
    return _min_order_size_cache.get(token_id, FALLBACK_MIN_ORDER_SIZE)


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


def cancel_order(order_id: str) -> bool:
    """Cancel a live order by ID via the CLOB API.

    Returns True on success, False on failure.
    """
    client = create_clob_client()
    if client is None:
        return False

    try:
        from py_clob_client_v2.clob_types import OrderPayload
        resp = client.cancel_order(OrderPayload(orderID=order_id))
        # v2 API returns a dict with 'canceled' list on success
        if isinstance(resp, dict) and resp.get("canceled"):
            logger.info("[ORDER] Cancelled order %s", order_id[:16])
            return True
        logger.warning("[ORDER] Cancel returned unexpected response for %s: %s", order_id[:16], resp)
        return False
    except Exception:
        logger.warning("[ORDER] Cancel failed for %s", order_id[:16], exc_info=True)
        return False


def get_open_orders(market: str | None = None, asset_id: str | None = None) -> list[dict[str, Any]]:
    """Return currently open orders for this API key."""
    client = create_clob_client()
    if client is None:
        return []

    try:
        params = OpenOrderParams(market=market, asset_id=asset_id)
        orders = client.get_open_orders(params=params)
        if not isinstance(orders, list):
            return []
        return [o for o in orders if isinstance(o, dict)]
    except Exception:
        logger.warning("[ORDER] Failed to fetch open orders", exc_info=True)
        return []


def cancel_orders(order_ids: list[str]) -> int:
    """Cancel many orders by ID, returning number cancelled."""
    if not order_ids:
        return 0

    client = create_clob_client()
    if client is None:
        return 0

    try:
        resp = client.cancel_orders(order_ids)
        if isinstance(resp, dict):
            cancelled = resp.get("canceled") or resp.get("cancelled") or []
            if isinstance(cancelled, list):
                return len(cancelled)
        logger.warning("[ORDER] cancel_orders unexpected response: %s", resp)
        return 0
    except Exception:
        logger.warning("[ORDER] cancel_orders failed for %d orders", len(order_ids), exc_info=True)
        return 0


def get_usdc_balance() -> float:
    """Fetch the USDC balance (collateral) for the current funder.
    
    Returns balance as a float (e.g. 10.50).
    """
    client = create_clob_client()
    if client is None:
        return 0.0

    try:
        from py_clob_client_v2 import AssetType
        params = BalanceAllowanceParams(asset_type=AssetType.COLLATERAL)
        resp = client.get_balance_allowance(params)
        raw_balance = int(resp.get("balance", "0"))
        # USDC has 6 decimals on Polygon
        return raw_balance / 1_000_000.0
    except Exception:
        logger.exception("Failed to fetch account balance")
        return 0.0


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
        tick_size: Known tick size from WebSocket event. When provided,
                   passed as ``PartialCreateOrderOptions(tick_size=...)``
                   so the library uses it directly instead of its stale
                   internal cache.  This is critical for 0.999 orders
                   where the tick size has just changed to 0.001.

    Returns:
        API response dict with success, orderId, errorMsg, status; or None on client error.
    """
    client = create_clob_client()
    if client is None:
        return None

    side_const = Side.BUY if side.upper() == "BUY" else Side.SELL

    # Build PartialCreateOrderOptions when we have an authoritative tick_size.
    # This tells the library: "use THIS tick_size for validation and rounding,
    # don't fetch your own from the API (which may still return the old value)."
    options: Optional[PartialCreateOrderOptions] = None
    if tick_size is not None:
        ts_str = _TICK_SIZE_MAP.get(tick_size)
        if ts_str is not None:
            options = PartialCreateOrderOptions(tick_size=ts_str)
            # Force-update the library's internal tick-size cache so
            # get_tick_size() returns immediately instead of HTTP call.
            client._ClobClient__tick_sizes[token_id] = ts_str
            logger.debug(
                "[ORDER] Using authoritative tick_size=%s for token %s…",
                ts_str, token_id[:20],
            )
        else:
            logger.warning(
                "[ORDER] Unknown tick_size=%.4f — not in known map, "
                "falling back to library resolution",
                tick_size,
            )

    logger.info(
        "Placing order: token_id=%s, price=%.4f, size=%.2f, side=%s, tick=%s",
        token_id,
        price,
        size,
        side,
        tick_size,
    )

    try:
        order_args = OrderArgs(
            price=price,
            size=size,
            side=side_const,
            token_id=token_id,
        )
        t0 = time.perf_counter_ns()
        # v2 API: create_and_post_order combines signing + posting
        resp = client.create_and_post_order(order_args, options, order_type=OrderType.GTC)
        t1 = time.perf_counter_ns()

        resp["_total_ms"] = (t1 - t0) / 1_000_000

        # v2 API returns orderID on success, error dict on failure
        order_id = resp.get("orderID") or resp.get("orderId") or ""
        error_msg = resp.get("error") or resp.get("errorMsg") or ""
        success = bool(order_id) and not error_msg

        if success:
            logger.info(
                "Order placed: orderId=%s",
                order_id,
            )
        else:
            reason = ERROR_REASONS.get(error_msg, error_msg or "Unknown error")
            logger.warning(
                "Order failed: errorMsg=%s, reason=%s",
                error_msg,
                reason,
            )

        logger.debug("Raw API response: %s", resp)
        return {
            "success": success,
            "orderID": order_id,
            "orderId": order_id,
            "errorMsg": error_msg,
            **resp,
        }

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
