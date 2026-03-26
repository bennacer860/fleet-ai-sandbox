"""CLOB client wrapper for placing Polymarket orders."""

import time

import httpx
from typing import Any, Optional

from py_clob_client.client import ClobClient
from py_clob_client.clob_types import (
    OrderArgs,
    OrderType,
    PartialCreateOrderOptions,
    TickSize,
    BalanceAllowanceParams,
)
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
    """Monkey-patch py_clob_client's HTTP helper to preserve real error details.

    The library's ``request()`` function catches ``httpx.RequestError`` and
    replaces it with the generic "Request exception!" string, losing the
    actual network error (e.g. timeout, DNS failure, connection refused).

    We also improve the non-200 path to include the HTTP status code and
    response body in the exception message.
    """
    global _http_patched
    if _http_patched:
        return

    import py_clob_client.http_helpers.helpers as http_mod

    _original_request = http_mod.request

    def _patched_request(endpoint: str, method: str, headers=None, data=None):
        try:
            headers = http_mod.overloadHeaders(method, headers)
            if isinstance(data, str):
                resp = http_mod._http_client.request(
                    method=method,
                    url=endpoint,
                    headers=headers,
                    content=data.encode("utf-8"),
                )
            else:
                resp = http_mod._http_client.request(
                    method=method,
                    url=endpoint,
                    headers=headers,
                    json=data,
                )

            if resp.status_code != 200:
                # Preserve the full response body instead of just the status
                try:
                    body = resp.json()
                except Exception:
                    body = resp.text
                raise PolyApiException(
                    error_msg=f"HTTP {resp.status_code}: {body}"
                )

            try:
                return resp.json()
            except ValueError:
                return resp.text

        except PolyApiException:
            raise  # Don't wrap our own exceptions
        except Exception as exc:
            # Preserve the real error details instead of "Request exception!"
            raise PolyApiException(
                error_msg=f"{type(exc).__name__}: {exc}"
            ) from exc

    http_mod.request = _patched_request
    _http_patched = True
    logger.debug("[PATCH] py_clob_client HTTP helper patched for detailed errors")


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
    and passes it via ``PartialCreateOrderOptions`` so the library never
    needs its own HTTP round-trip.
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

            # ── Monkey-patch the library's HTTP helper to preserve real errors ─
            # The library catches httpx.RequestError and replaces it with
            # the unhelpful "Request exception!" string, losing the actual
            # network error.  We wrap the function to keep the detail.
            _patch_http_helpers()

        return _client_cache
    except Exception:
        logger.exception("Failed to create CLOB client")
        return None


def precache_token_data(token_ids: list[str]) -> None:
    """Pre-fetch and cache neg_risk, fee_rate, AND min_order_size at market-add time.

    This populates the py_clob_client internal caches so that
    ``create_order()`` never needs to make HTTP calls for these values
    during the latency-critical order-placement path.

    Also caches ``min_order_size`` from the order book so the strategy
    can look it up instantly instead of making a blocking CLOB call.
    """
    client = create_clob_client()
    if client is None:
        return

    for token_id in token_ids:
        try:
            # These calls hit the CLOB API and cache the result internally
            # in client.__neg_risk[token_id] and client.__fee_rates[token_id]
            neg_risk = client.get_neg_risk(token_id)
            fee_rate = client.get_fee_rate_bps(token_id)

            # Also fetch order book to cache min_order_size
            try:
                book = client.get_order_book(token_id)
                if book.min_order_size:
                    mos = float(book.min_order_size)
                    _min_order_size_cache[token_id] = mos
            except Exception:
                logger.debug("[PRECACHE] Order book fetch failed for %s…", token_id[:20])

            logger.debug(
                "[PRECACHE] token=%s… neg_risk=%s, fee_rate=%s, min_order_size=%s",
                token_id[:20], neg_risk, fee_rate,
                _min_order_size_cache.get(token_id, "?"),
            )
        except Exception:
            logger.warning("[PRECACHE] Failed for token %s…", token_id[:20])


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
        resp = client.cancel(order_id)
        # py_clob_client returns a dict with 'canceled' list on success
        if isinstance(resp, dict) and resp.get("canceled"):
            logger.info("[ORDER] Cancelled order %s", order_id[:16])
            return True
        logger.warning("[ORDER] Cancel returned unexpected response for %s: %s", order_id[:16], resp)
        return False
    except Exception:
        logger.warning("[ORDER] Cancel failed for %s", order_id[:16], exc_info=True)
        return False


def get_usdc_balance() -> float:
    """Fetch the USDC balance (collateral) for the current funder.
    
    Returns balance as a float (e.g. 10.50).
    """
    client = create_clob_client()
    if client is None:
        return 0.0

    try:
        params = BalanceAllowanceParams(
            asset_type="COLLATERAL",
            signature_type=SIGNATURE_TYPE
        )
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

    side_const = BUY if side.upper() == "BUY" else SELL

    # Build PartialCreateOrderOptions when we have an authoritative tick_size.
    # This tells the library: "use THIS tick_size for validation and rounding,
    # don't fetch your own from the API (which may still return the old value)."
    options: Optional[PartialCreateOrderOptions] = None
    if tick_size is not None:
        ts_str = _TICK_SIZE_MAP.get(tick_size)
        if ts_str is not None:
            options = PartialCreateOrderOptions(tick_size=ts_str)
            # Force-update the library's internal tick-size cache AND its
            # TTL timestamp so get_tick_size() returns immediately instead
            # of making a blocking HTTP call on every order.
            client._ClobClient__tick_sizes[token_id] = ts_str
            client._ClobClient__tick_size_timestamps[token_id] = time.monotonic()
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
        signed_order = client.create_order(order_args, options)
        t1 = time.perf_counter_ns()
        resp = client.post_order(signed_order, OrderType.GTC)
        t2 = time.perf_counter_ns()

        resp["_sign_ms"] = (t1 - t0) / 1_000_000
        resp["_post_ms"] = (t2 - t1) / 1_000_000

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
