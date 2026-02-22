#!/usr/bin/env python3
"""Latency test for Polymarket CLOB order placement and cancellation.

Measures end-to-end latency:
  1. Order signing time
  2. REST API round-trip (post_order → API response)
  3. Market WebSocket latency (post_order → order visible in book)
  4. User WebSocket latency (post_order → user channel notification)
  5. Cancel REST API round-trip (cancel → API response)
  6. Cancel WebSocket latency (cancel → user channel notification)

Usage:
    python latency_test.py --slug <event-slug> [--tests 10] [--no-user-ws]

The script automatically selects the token with the highest current price
(the most likely winner) and places unfillable orders at 0.01 to measure
latency without risking real money.  Orders are cancelled immediately.
"""

import argparse
import asyncio
import csv
import json
import sys
import time
from datetime import datetime, timezone
from typing import Any, Optional

import websockets

from py_clob_client.client import ClobClient
from py_clob_client.clob_types import OpenOrderParams, OrderArgs, OrderType
from py_clob_client.order_builder.constants import BUY

from src.config import (
    CHAIN_ID,
    CLOB_HOST,
    FUNDER,
    PRIVATE_KEY,
    SIGNATURE_TYPE,
)
from src.gamma_client import (
    fetch_event_by_slug,
    get_market_token_ids,
    get_outcomes,
    get_outcome_prices,
)
from src.logging_config import setup_logging

# ── Constants ────────────────────────────────────────────────────────────────

# WebSocket endpoints
MARKET_WS_URL = "wss://ws-subscriptions-clob.polymarket.com/ws/market"
USER_WS_URL = "wss://ws-subscriptions-clob.polymarket.com/ws/user"

# Unfillable price — a bid so low nobody will ever sell into it
UNFILLABLE_PRICE = 0.01

# CSV output file for raw latency data
LATENCY_CSV = "latency_results.csv"


# ── Market Discovery ─────────────────────────────────────────────────────────


def find_best_token(
    client: ClobClient, slug: str
) -> tuple[str, str, float, float]:
    """Find the best token for testing from an event slug.

    Uses the CLOB order book to get live best-bid/best-ask so we can pick
    the token that is most actively traded (highest mid price).

    Returns:
        (token_id, outcome_label, mid_price, min_order_size)
    """
    event = fetch_event_by_slug(slug)
    if not event:
        print(f"Error: Could not fetch event for slug: {slug}")
        sys.exit(1)

    markets = event.get("markets", [])
    if not markets:
        print(f"Error: No markets found for slug: {slug}")
        sys.exit(1)

    market = markets[0]
    token_ids = get_market_token_ids(market)
    outcomes = get_outcomes(market)

    if len(token_ids) < 2:
        print(f"Error: Expected 2 tokens, got {len(token_ids)}")
        sys.exit(1)

    print(f"\nMarket: {slug}")
    print(f"  Outcomes: {outcomes}")
    print(f"  Token IDs:")

    best_idx = 0
    best_mid = 0.0
    min_order_size = 1.0  # default fallback

    for i, (tid, outcome) in enumerate(zip(token_ids, outcomes)):
        try:
            book = client.get_order_book(tid)
            best_bid = float(book.bids[0].price) if book.bids else 0.0
            best_ask = float(book.asks[0].price) if book.asks else 1.0
            mid = (best_bid + best_ask) / 2.0
            mos = float(book.min_order_size) if book.min_order_size else 1.0
            tick = book.tick_size or "0.01"
            print(
                f"    [{i}] {outcome}: bid={best_bid:.4f} ask={best_ask:.4f} "
                f"mid={mid:.4f} min_size={mos} tick={tick} token={tid[:20]}…"
            )
            if mid > best_mid:
                best_mid = mid
                best_idx = i
                min_order_size = mos
        except Exception as e:
            print(f"    [{i}] {outcome}: ⚠ book fetch failed ({e})")
            # Fall back to Gamma prices
            prices = get_outcome_prices(market)
            if i < len(prices) and prices[i] > best_mid:
                best_mid = prices[i]
                best_idx = i

    selected_token = token_ids[best_idx]
    selected_outcome = outcomes[best_idx] if best_idx < len(outcomes) else "?"

    print(f"\n  → Selected: {selected_outcome} (mid={best_mid:.4f}) for testing")
    print(f"    Token: {selected_token}")
    print(f"    Min order size: {min_order_size}")

    if best_mid < 0.5:
        print(
            f"  ⚠ Warning: mid price is only {best_mid:.4f}. "
            "Consider a market with a clearer favorite."
        )

    return selected_token, selected_outcome, best_mid, min_order_size


# ── CLOB Client ──────────────────────────────────────────────────────────────


def create_client() -> ClobClient:
    """Create and initialize the CLOB client (once, reused for all tests)."""
    if not PRIVATE_KEY or not FUNDER:
        print("Error: PRIVATE_KEY and FUNDER must be set in .env")
        sys.exit(1)

    client = ClobClient(
        CLOB_HOST,
        key=PRIVATE_KEY,
        chain_id=CHAIN_ID,
        signature_type=SIGNATURE_TYPE,
        funder=FUNDER,
    )
    client.set_api_creds(client.create_or_derive_api_creds())
    return client


# ── Latency Tests ────────────────────────────────────────────────────────────


async def _drain_ws(ws, timeout: float = 1.0):
    """Drain any buffered messages from a WebSocket."""
    while True:
        try:
            await asyncio.wait_for(ws.recv(), timeout=timeout)
        except (asyncio.TimeoutError, websockets.exceptions.ConnectionClosed):
            break


async def _wait_for_book_at_price(
    ws: websockets.WebSocketClientProtocol, 
    token_id: str, 
    target_price: float, 
    timeout_s: float = 8.0
) -> Optional[int]:
    """
    Wait for a 'book' or 'price_change' event that includes our target_price.
    Returns the counter_ns timestamp of receipt, or None on timeout.
    """
    deadline = time.perf_counter_ns() + (timeout_s * 1_000_000_000)

    while True:
        now = time.perf_counter_ns()
        if now >= deadline:
            break
        
        remaining_s = (deadline - now) / 1_000_000_000
        try:
            msg = await asyncio.wait_for(ws.recv(), timeout=remaining_s)
            t_recv = time.perf_counter_ns()
        except (asyncio.TimeoutError, websockets.exceptions.ConnectionClosed):
            break

        if not isinstance(msg, str):
            continue
        try:
            data = json.loads(msg)
            # DEBUG: Uncomment to see all market messages
            # print(f"  [Mkt WS] {msg[:100]}...")
        except json.JSONDecodeError:
            continue
        if not isinstance(data, dict):
            continue
        
        event_type = data.get("event_type")

        # 1. Handle 'book' updates
        if event_type == "book":
            bids = data.get("bids", [])
            for bid in bids:
                try:
                    price = float(bid.get("price", 0))
                    if abs(price - target_price) < 0.0001:
                        return t_recv
                except (ValueError, TypeError):
                    pass

        # 2. Handle 'price_change' updates
        price_changes = data.get("price_changes")
        if event_type == "price_change" or price_changes:
            if not price_changes and "price" in data:
                price_changes = [data]
            
            if price_changes:
                for pc in price_changes:
                    try:
                        pc_asset = pc.get("asset_id")
                        if pc_asset and pc_asset != token_id:
                            continue
                        price = float(pc.get("price", 0))
                        if abs(price - target_price) < 0.0001:
                            return t_recv
                    except (ValueError, TypeError):
                        pass
    return None


async def _wait_for_user_event(
    ws: websockets.WebSocketClientProtocol, 
    order_id: str, 
    timeout_s: float = 10.0
) -> Optional[tuple[int, str]]:
    """
    Wait for a user-channel message referencing order_id.
    Returns (t_recv, event_type) or None.
    """
    deadline = time.perf_counter_ns() + (timeout_s * 1_000_000_000)

    while True:
        now = time.perf_counter_ns()
        if now >= deadline:
            break
        
        remaining_s = (deadline - now) / 1_000_000_000
        try:
            msg = await asyncio.wait_for(ws.recv(), timeout=remaining_s)
            t_recv = time.perf_counter_ns()
            # print(f"  [Usr WS] {msg[:100]}...")
        except (asyncio.TimeoutError, websockets.exceptions.ConnectionClosed):
            break

        if not isinstance(msg, str):
            continue
        try:
            data = json.loads(msg)
        except json.JSONDecodeError:
            continue

        items = data if isinstance(data, list) else [data]
        for item in items:
            if not isinstance(item, dict):
                continue
            
            oid = (
                item.get("id", "")
                or item.get("order_id", "")
                or item.get("orderId", "")
                or item.get("orderID", "")
            )
            if oid == order_id:
                evt = item.get("type", item.get("event_type", "unknown"))
                return t_recv, evt

    return None


async def _cleanup_leftover_orders(client: ClobClient, token_id: str):
    """List and cancel any remaining open orders for the test token."""
    print(f"\n── Cleanup: checking for leftover orders ──")
    try:
        params = OpenOrderParams(asset_id=token_id)
        open_orders = client.get_orders(params)

        # get_orders may return a list or a paginated object
        orders = open_orders if isinstance(open_orders, list) else []

        if not orders:
            print("  No leftover orders found ✓")
            return

        print(f"  Found {len(orders)} leftover order(s) — cancelling…")
        for order in orders:
            oid = (
                order.get("id", "")
                or order.get("order_id", "")
                or order.get("orderID", "")
            )
            if oid:
                try:
                    client.cancel(oid)
                    print(f"    Cancelled: {oid[:16]}…")
                except Exception as e:
                    print(f"    ⚠ Failed to cancel {oid[:16]}…: {e}")

        # As a safety net, also call cancel_market_orders for this asset
        try:
            client.cancel_market_orders(asset_id=token_id)
            print("  Bulk cancel_market_orders completed ✓")
        except Exception as e:
            print(f"  ⚠ Bulk cancel_market_orders failed: {e}")

    except Exception as e:
        # If listing fails, fall back to bulk cancel
        print(f"  ⚠ Could not list orders ({e}), attempting bulk cancel…")
        try:
            client.cancel_market_orders(asset_id=token_id)
            print("  Bulk cancel_market_orders completed ✓")
        except Exception as e2:
            print(f"  ⚠ Bulk cancel also failed: {e2}")


async def run_latency_tests(
    client: ClobClient,
    token_id: str,
    outcome: str,
    order_size: float,
    num_tests: int = 10,
    use_user_ws: bool = True,
    skip_cancel: bool = False,
):
    """Run latency tests for order placement and cancellation."""

    results: list[dict[str, Any]] = []

    # ── Connect to market WebSocket ──────────────────────────────────────
    print("\nConnecting to market WebSocket…")
    headers = {
        "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
        "Origin": "https://polymarket.com"
    }
    market_ws = await websockets.connect(
        MARKET_WS_URL, 
        ping_interval=10, 
        ping_timeout=10,
        additional_headers=headers
    )
    subscribe_msg = {
        "type": "subscribe",
        "assets_ids": [token_id],
        "custom_feature_enabled": False,
    }
    await market_ws.send(json.dumps(subscribe_msg))
    print(f"  Subscribed to market channel for {token_id[:20]}…")

    # ── Optionally connect to user WebSocket ─────────────────────────────
    user_ws: Optional[websockets.WebSocketClientProtocol] = None
    if use_user_ws:
        try:
            print("Connecting to user WebSocket…")
            user_ws = await websockets.connect(
                USER_WS_URL, 
                ping_interval=10, 
                ping_timeout=10,
                additional_headers=headers
            )
            api_creds = client.creds
            auth_msg = {
                "auth": {
                    "apiKey": api_creds.api_key,
                    "secret": api_creds.api_secret,
                    "passphrase": api_creds.api_passphrase,
                },
                "type": "subscribe",
                "channels": ["user"],
            }
            await user_ws.send(json.dumps(auth_msg))

            # Wait for auth acknowledgment
            try:
                # Some servers send multiple messages on startup (e.g. initial state)
                # so we loop briefly to find the 'auth' response
                for _ in range(5):
                    auth_resp_raw = await asyncio.wait_for(user_ws.recv(), timeout=3.0)
                    auth_data = json.loads(auth_resp_raw)
                    if isinstance(auth_data, dict) and auth_data.get("type") == "auth":
                        if auth_data.get("success"):
                            print(f"  User WS: Authentication successful ✓")
                        else:
                            print(f"  User WS: Auth FAILED: {auth_data}")
                        break
            except asyncio.TimeoutError:
                print("  User WS: No auth response received (continuing anyway)")
            except Exception as e:
                print(f"  User WS auth error: {e}")
        except Exception as e:
            print(f"  ⚠ User WebSocket failed: {e}")
            print("  Continuing with REST + market WS only")
            user_ws = None

    # Drain startup messages
    await _drain_ws(market_ws, timeout=2.0)
    if user_ws:
        await _drain_ws(user_ws, timeout=2.0)

    # ── Header ───────────────────────────────────────────────────────────
    print(f"\n{'=' * 72}")
    print(f"Starting {num_tests} latency tests")
    print(f"  Token   : {token_id[:20]}… ({outcome})")
    print(f"  Order   : BUY {order_size} @ {UNFILLABLE_PRICE} (unfillable)")
    print(f"  User WS : {'enabled' if user_ws else 'disabled'}")
    print(f"{'=' * 72}\n")

    for i in range(num_tests):
        print(f"── Test {i + 1}/{num_tests} ──")

        result: dict[str, Any] = {
            "test": i + 1,
            "timestamp_utc": datetime.now(timezone.utc).isoformat(),
            "sign_ms": None,
            "order_rest_ms": None,
            "order_market_ws_ms": None,
            "order_user_ws_ms": None,
            "order_user_ws_event": None,
            "cancel_rest_ms": None,
            "cancel_user_ws_ms": None,
            "cancel_user_ws_event": None,
            "error": None,
        }

        # ── 1. Sign the order ────────────────────────────────────────────
        order_args = OrderArgs(
            price=UNFILLABLE_PRICE,
            size=order_size,
            side=BUY,
            token_id=token_id,
        )

        t0 = time.perf_counter_ns()
        signed_order = client.create_order(order_args)
        t1 = time.perf_counter_ns()
        sign_ms = (t1 - t0) / 1_000_000
        result["sign_ms"] = round(sign_ms, 2)
        print(f"  Sign        : {sign_ms:8.1f} ms")

        # ── 2. Place the order (REST) ────────────────────────────────────
        t_submit = time.perf_counter_ns()
        resp = client.post_order(signed_order, OrderType.GTC)
        t_api = time.perf_counter_ns()

        order_rest_ms = (t_api - t_submit) / 1_000_000
        result["order_rest_ms"] = round(order_rest_ms, 2)

        order_id = resp.get("orderID", resp.get("orderId", ""))
        success = resp.get("success", False)

        if not success:
            error_msg = resp.get("errorMsg", "unknown")
            print(f"  ❌ Order failed: {error_msg}  (response: {resp})")
            result["error"] = error_msg
            results.append(result)
            await asyncio.sleep(2)
            continue

        print(f"  REST place  : {order_rest_ms:8.1f} ms  (orderId={order_id[:16]}…)")

        # ── 3. Wait for market WS confirmation ───────────────────────────
        # We measure from t_submit (when we sent the REST request)
        mkt_task = asyncio.create_task(
            _wait_for_book_at_price(market_ws, token_id, UNFILLABLE_PRICE, timeout_s=10.0)
        )

        # ── 4. Wait for user WS confirmation ─────────────────────────────
        usr_task = None
        if user_ws:
            usr_task = asyncio.create_task(
                _wait_for_user_event(user_ws, order_id, timeout_s=10.0)
            )

        # Await market WS result
        t_recv_mkt = await mkt_task
        if t_recv_mkt is not None:
            # Calculate from t_submit (when REST call started) to t_recv
            mkt_latency_ms = (t_recv_mkt - t_submit) / 1_000_000
            result["order_market_ws_ms"] = round(mkt_latency_ms, 2)
            print(f"  Market WS   : {mkt_latency_ms:8.1f} ms  (bid appeared at {UNFILLABLE_PRICE})")
        else:
            print(f"  Market WS   :   — timed out")

        # Await user WS result
        if usr_task:
            usr_result = await usr_task
            if usr_result:
                t_recv_usr, evt = usr_result
                usr_latency_ms = (t_recv_usr - t_submit) / 1_000_000
                result["order_user_ws_ms"] = round(usr_latency_ms, 2)
                result["order_user_ws_event"] = evt
                print(f"  User WS     : {usr_latency_ms:8.1f} ms  (event={evt})")
            else:
                print(f"  User WS     :   — timed out")

        # ── 5. Cancel the order (REST) ───────────────────────────────────
        if skip_cancel:
            print(f"  Cancel      : skipped (--no-cancel)")
        else:
            await asyncio.sleep(0.3)  # brief pause

            t_cancel = time.perf_counter_ns()
            try:
                client.cancel(order_id)
            except Exception as e:
                print(f"  ❌ Cancel failed: {e}")
                result["error"] = f"cancel failed: {e}"
                results.append(result)
                continue
            t_cancel_done = time.perf_counter_ns()

            cancel_rest_ms = (t_cancel_done - t_cancel) / 1_000_000
            result["cancel_rest_ms"] = round(cancel_rest_ms, 2)
            print(f"  REST cancel : {cancel_rest_ms:8.1f} ms")

            # ── 6. Wait for cancel confirmation on user WS ───────────────
            if user_ws:
                cancel_usr = await _wait_for_user_event(
                    user_ws, order_id, timeout_s=5.0
                )
                if cancel_usr:
                    t_recv_cancel, evt = cancel_usr
                    cancel_ws_ms = (t_recv_cancel - t_cancel) / 1_000_000
                    result["cancel_user_ws_ms"] = round(cancel_ws_ms, 2)
                    result["cancel_user_ws_event"] = evt
                    print(f"  Cancel WS   : {cancel_ws_ms:8.1f} ms  (event={evt})")
                else:
                    print(f"  Cancel WS   :   — timed out")

        # Drain leftover messages before next iteration
        await _drain_ws(market_ws, timeout=1.0)
        if user_ws:
            await _drain_ws(user_ws, timeout=1.0)

        results.append(result)
        print()

        # Rate-limit between tests
        if i < num_tests - 1:
            await asyncio.sleep(3)

    # ── Cleanup: cancel any leftover orders for this token ──────────────
    if skip_cancel:
        print(f"\n  ⚠ --no-cancel: {num_tests} order(s) left pending on the book")
    else:
        await _cleanup_leftover_orders(client, token_id)

    # ── Close WebSockets ─────────────────────────────────────────────────
    await market_ws.close()
    if user_ws:
        await user_ws.close()

    # ── Save CSV ─────────────────────────────────────────────────────────
    save_csv(results)

    # ── Summary ──────────────────────────────────────────────────────────
    print_summary(results)


# ── Output Helpers ───────────────────────────────────────────────────────────


def save_csv(results: list[dict[str, Any]]):
    """Save raw latency results to CSV."""
    if not results:
        return

    fieldnames = list(results[0].keys())
    with open(LATENCY_CSV, "w", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=fieldnames)
        writer.writeheader()
        writer.writerows(results)
    print(f"\nRaw results saved to {LATENCY_CSV}")


def _stats(values: list[float]) -> dict[str, float]:
    """Compute summary statistics for a list of values."""
    if not values:
        return {}
    values_sorted = sorted(values)
    n = len(values_sorted)
    return {
        "n": n,
        "avg": sum(values) / n,
        "min": values_sorted[0],
        "p50": values_sorted[n // 2],
        "p95": values_sorted[min(int(n * 0.95), n - 1)],
        "max": values_sorted[-1],
    }


def print_summary(results: list[dict[str, Any]]):
    """Print a human-friendly latency summary."""
    print(f"\n{'=' * 72}")
    print(f"LATENCY SUMMARY  ({len(results)} tests)")
    print(f"{'=' * 72}")

    metrics = [
        ("sign_ms", "Order signing"),
        ("order_rest_ms", "Order REST API"),
        ("order_market_ws_ms", "Order → Market WS"),
        ("order_user_ws_ms", "Order → User WS"),
        ("cancel_rest_ms", "Cancel REST API"),
        ("cancel_user_ws_ms", "Cancel → User WS"),
    ]

    for key, label in metrics:
        vals = [r[key] for r in results if r.get(key) is not None]
        if not vals:
            print(f"  {label:22s}:  no data")
            continue
        s = _stats(vals)
        print(
            f"  {label:22s}:  avg={s['avg']:7.1f}ms  "
            f"min={s['min']:7.1f}ms  p50={s['p50']:7.1f}ms  "
            f"p95={s['p95']:7.1f}ms  max={s['max']:7.1f}ms  (n={s['n']})"
        )

    errors = [r for r in results if r.get("error")]
    if errors:
        print(f"\n  ⚠ {len(errors)} test(s) had errors:")
        for r in errors:
            print(f"    Test {r['test']}: {r['error']}")

    print(f"{'=' * 72}")


# ── CLI ──────────────────────────────────────────────────────────────────────


def main():
    parser = argparse.ArgumentParser(
        description="Polymarket CLOB latency tester",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Examples:
    python latency_test.py --slug will-trump-win-the-2024-presidential-election
    python latency_test.py --slug btc-updown-15m-1234567890 --tests 20
    python latency_test.py --slug some-event --no-user-ws
        """,
    )
    parser.add_argument(
        "--slug",
        required=True,
        help="Event slug (from polymarket.com/event/<slug>)",
    )
    parser.add_argument(
        "--tests",
        type=int,
        default=10,
        help="Number of latency tests to run (default: 10)",
    )
    parser.add_argument(
        "--no-user-ws",
        action="store_true",
        help="Skip user WebSocket channel (REST + market WS only)",
    )
    parser.add_argument(
        "--no-cancel",
        action="store_true",
        help="Leave orders pending — skip cancel step and final cleanup",
    )
    args = parser.parse_args()

    setup_logging()

    # ── Create CLOB client once ──────────────────────────────────────────
    print("Initializing CLOB client…")
    client = create_client()
    print("  Client ready")

    # ── Find best token ──────────────────────────────────────────────────
    token_id, outcome, mid_price, min_order_size = find_best_token(
        client, args.slug
    )

    # Use the minimum order size the CLOB allows
    order_size = max(float(min_order_size), 1.0)
    locked_capital = order_size * UNFILLABLE_PRICE
    print(f"\n  Order size: {order_size} shares @ ${UNFILLABLE_PRICE}")
    print(f"  Max capital locked per test: ${locked_capital:.4f} (returned on cancel)")

    # Warm up: The first signature in Python often takes ~500ms due to 
    # lazy-loading of crypto libraries. Signing a dummy order once 
    # keeps subsequent signings fast (~10-15ms).
    print("\nWarming up signing engine…")
    dummy_args = OrderArgs(
        price=UNFILLABLE_PRICE,
        size=order_size,
        side=BUY,
        token_id=token_id,
    )
    client.create_order(dummy_args)
    print("  Signing engine hot ✓")

    # ── Run tests ────────────────────────────────────────────────────────
    asyncio.run(
        run_latency_tests(
            client=client,
            token_id=token_id,
            outcome=outcome,
            order_size=order_size,
            num_tests=args.tests,
            use_user_ws=not args.no_user_ws,
            skip_cancel=args.no_cancel,
        )
    )


if __name__ == "__main__":
    main()
