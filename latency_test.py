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
and places unfillable orders (at a discount to mid-price) to measure
latency without risking real money. Orders are cancelled immediately.
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

MARKET_WS_URL = "wss://ws-subscriptions-clob.polymarket.com/ws/market"
USER_WS_URL = "wss://ws-subscriptions-clob.polymarket.com/ws/user"

LATENCY_CSV = "latency_results.csv"


# ── Market Discovery ─────────────────────────────────────────────────────────


def find_best_token(
    client: ClobClient, slug: str
) -> tuple[str, str, float, float]:
    """Find the best token for testing from an event slug."""
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
    min_order_size = 1.0

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
            prices = get_outcome_prices(market)
            if i < len(prices) and prices[i] > best_mid:
                best_mid = prices[i]
                best_idx = i

    selected_token = token_ids[best_idx]
    selected_outcome = outcomes[best_idx] if best_idx < len(outcomes) else "?"

    print(f"\n  → Selected: {selected_outcome} (mid={best_mid:.4f}) for testing")
    print(f"    Token: {selected_token}")
    print(f"    Min order size: {min_order_size}")

    return selected_token, selected_outcome, best_mid, min_order_size


# ── CLOB Client ──────────────────────────────────────────────────────────────


def create_client() -> ClobClient:
    """Create and initialize the CLOB client."""
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
    ws, target_price: float, timeout_s: float = 20.0
) -> Optional[int]:
    """Wait for a book update that contains a bid at target_price.
    Returns the absolute t_recv (counter_ns).
    """
    deadline = time.perf_counter_ns() + int(timeout_s * 1_000_000_000)

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

        if not isinstance(msg, str) or msg == "INVALID OPERATION":
            continue
        try:
            data = json.loads(msg)
        except json.JSONDecodeError:
            continue
        
        if not isinstance(data, dict) or data.get("event_type") != "book":
            continue

        bids = data.get("bids", [])
        for bid in bids:
            try:
                if abs(float(bid.get("price", 0)) - target_price) < 0.0001:
                    return t_recv
            except (ValueError, TypeError):
                pass
    return None


async def _wait_for_user_event(
    ws, order_id: str, timeout_s: float = 35.0
) -> Optional[tuple[int, str]]:
    """Wait for a user-channel message referencing order_id.
    Returns (t_recv_ns, event_type).
    """
    deadline = time.perf_counter_ns() + int(timeout_s * 1_000_000_000)

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
        orders = open_orders if isinstance(open_orders, list) else []

        if not orders:
            print("  No leftover orders found ✓")
            return

        print(f"  Found {len(orders)} leftover order(s) — cancelling…")
        for order in orders:
            oid = order.get("id") or order.get("order_id") or order.get("orderID")
            if oid:
                try:
                    client.cancel(oid)
                    print(f"    Cancelled: {oid[:16]}…")
                except:
                    pass
        client.cancel_market_orders(asset_id=token_id)
    except:
        try:
            client.cancel_market_orders(asset_id=token_id)
        except:
            pass


async def run_latency_tests(
    client: ClobClient,
    token_id: str,
    outcome: str,
    mid_price: float,
    order_size: float,
    num_tests: int = 10,
    use_user_ws: bool = True,
    skip_cancel: bool = False,
):
    """Run latency tests for order placement and cancellation."""
    results: list[dict[str, Any]] = []

    print("\nConnecting to market WebSocket…")
    market_ws = await websockets.connect(MARKET_WS_URL, ping_interval=None, ping_timeout=60)
    await market_ws.send(json.dumps({
        "type": "subscribe",
        "assets_ids": [token_id],
        "custom_feature_enabled": False,
    }))
    print(f"  Subscribed to market channel for {token_id[:20]}…")

    user_ws = None
    if use_user_ws:
        try:
            print("Connecting to user WebSocket…")
            user_ws = await websockets.connect(USER_WS_URL, ping_interval=None, ping_timeout=60)
            api_creds = client.creds
            await user_ws.send(json.dumps({
                "auth": {
                    "apiKey": api_creds.api_key,
                    "secret": api_creds.api_secret,
                    "passphrase": api_creds.api_passphrase,
                },
                "type": "subscribe",
                "markets": [token_id],
                "assets_ids": [token_id],
                "channels": ["user"],
            }))

            # Wait for auth success
            try:
                for _ in range(5):
                    raw = await asyncio.wait_for(user_ws.recv(), timeout=3.0)
                    resp = json.loads(raw)
                    if isinstance(resp, dict) and resp.get("type") == "auth":
                        if resp.get("success"):
                            print("  User WS: Authentication successful ✓")
                        else:
                            print(f"  User WS: Auth failed: {resp}")
                        break
            except:
                print("  User WS: Auth response timeout (continuing)")
        except Exception as e:
            print(f"  ⚠ User WebSocket connection failed: {e}")
            user_ws = None

    await _drain_ws(market_ws, timeout=1.0)
    if user_ws:
        await _drain_ws(user_ws, timeout=1.0)

    print(f"\n{'=' * 72}")
    print(f"Starting {num_tests} latency tests")
    print(f"  Token   : {token_id[:20]}… ({outcome})")
    print(f"  User WS : {'enabled' if user_ws else 'disabled'}")
    print(f"{'=' * 72}\n")

    # Visible but unfillable price: 50% of mid
    test_price = round(mid_price * 0.5, 2)
    if test_price < 0.01: test_price = 0.01

    for i in range(num_tests):
        print(f"── Test {i + 1}/{num_tests} ──")
        result = {
            "test": i + 1,
            "timestamp_utc": datetime.now(timezone.utc).isoformat(),
            "sign_ms": None,
            "order_rest_ms": None,
            "order_market_ws_ms": None,
            "order_user_ws_ms": None,
            "cancel_rest_ms": None,
            "cancel_user_ws_ms": None,
        }

        # 1. Sign
        args = OrderArgs(price=test_price, size=order_size, side=BUY, token_id=token_id)
        t_start_sign = time.perf_counter_ns()
        signed = client.create_order(args)
        t_end_sign = time.perf_counter_ns()
        result["sign_ms"] = round((t_end_sign - t_start_sign) / 1_000_000, 2)
        print(f"  Sign        : {result['sign_ms']:8.1f} ms")

        # 2. Start WS tasks
        mkt_task = asyncio.create_task(_wait_for_book_at_price(market_ws, test_price, 20.0))
        usr_task = None
        if user_ws:
            usr_task = asyncio.create_task(_wait_for_user_event(user_ws, "PENDING", 30.0)) # will update after ID known

        # 3. Post (REST)
        t_submit = time.perf_counter_ns()
        resp = client.post_order(signed, OrderType.GTC)
        t_api_done = time.perf_counter_ns()
        
        result["order_rest_ms"] = round((t_api_done - t_submit) / 1_000_000, 2)
        order_id = resp.get("orderID") or resp.get("orderId")
        
        if not order_id:
            print(f"  ❌ Order failed: {resp}")
            await mkt_task
            continue
        
        print(f"  REST place  : {result['order_rest_ms']:8.1f} ms  (orderId={order_id[:16]}…)")

        # Re-target user task with actual order_id
        if usr_task:
            usr_task.cancel()
            usr_task = asyncio.create_task(_wait_for_user_event(user_ws, order_id, 20.0))

        # 4. Await WS
        t_recv_mkt = await mkt_task
        if t_recv_mkt:
            result["order_market_ws_ms"] = round((t_recv_mkt - t_submit) / 1_000_000, 2)
            print(f"  Market WS   : {result['order_market_ws_ms']:8.1f} ms  (bid appeared at {test_price})")
        else:
            print("  Market WS   :   — timed out")

        if usr_task:
            usr_res = await usr_task
            if usr_res:
                t_recv_usr, evt = usr_res
                result["order_user_ws_ms"] = round((t_recv_usr - t_submit) / 1_000_000, 2)
                print(f"  User WS     : {result['order_user_ws_ms']:8.1f} ms  (event={evt})")
            else:
                print("  User WS     :   — timed out")

        # 5. Cancel
        if skip_cancel:
            print("  Cancel      : skipped (--no-cancel)")
        else:
            await asyncio.sleep(0.5)
            t_cancel_start = time.perf_counter_ns()
            client.cancel(order_id)
            t_cancel_api = time.perf_counter_ns()
            result["cancel_rest_ms"] = round((t_cancel_api - t_cancel_start) / 1_000_000, 2)
            print(f"  REST cancel : {result['cancel_rest_ms']:8.1f} ms")

            if user_ws:
                c_res = await _wait_for_user_event(user_ws, order_id, 10.0)
                if c_res:
                    t_recv_c, evt = c_res
                    result["cancel_user_ws_ms"] = round((t_recv_c - t_cancel_start) / 1_000_000, 2)
                    print(f"  Cancel WS   : {result['cancel_user_ws_ms']:8.1f} ms  (event={evt})")
                else:
                    print("  Cancel WS   :   — timed out")

        results.append(result)
        print()
        await asyncio.sleep(1)

    await market_ws.close()
    if user_ws: await user_ws.close()
    
    save_csv(results)
    print_summary(results)


def save_csv(results: list[dict[str, Any]]):
    if not results: return
    with open(LATENCY_CSV, "w", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=results[0].keys())
        writer.writeheader()
        writer.writerows(results)
    print(f"\nRaw results saved to {LATENCY_CSV}")


def print_summary(results: list[dict[str, Any]]):
    print(f"\n{'=' * 72}\nLATENCY SUMMARY  ({len(results)} tests)\n{'=' * 72}")
    metrics = [
        ("sign_ms", "Order signing"),
        ("order_rest_ms", "Order REST API"),
        ("order_market_ws_ms", "Order → Market WS"),
        ("order_user_ws_ms", "Order → User WS"),
        ("cancel_rest_ms", "Cancel REST API"),
        ("cancel_user_ws_ms", "Cancel → User WS"),
    ]
    for key, label in metrics:
        vals = sorted([r[key] for r in results if r.get(key) is not None])
        if not vals:
            print(f"  {label:22s}:  no data")
            continue
        n = len(vals)
        avg = sum(vals) / n
        print(f"  {label:22s}:  avg={avg:7.1f}ms  min={vals[0]:7.1f}ms  p50={vals[n//2]:7.1f}ms  max={vals[-1]:7.1f}ms")
    print('=' * 72)


def main():
    parser = argparse.ArgumentParser(description="Polymarket CLOB latency tester")
    parser.add_argument("--slug", required=True, help="Event slug")
    parser.add_argument("--tests", type=int, default=10, help="Number of tests")
    parser.add_argument("--no-user-ws", action="store_true", help="Skip user WS")
    parser.add_argument("--no-cancel", action="store_true", help="Skip cancel")
    args = parser.parse_args()

    setup_logging()
    client = create_client()

    token_id, outcome, mid, min_size = find_best_token(client, args.slug)
    order_size = max(float(min_size), 5.0)

    print("\nWarming up signing engine…")
    dummy = OrderArgs(price=0.1, size=order_size, side=BUY, token_id=token_id)
    client.create_order(dummy)
    print("  Signing engine hot ✓")

    asyncio.run(run_latency_tests(
        client, token_id, outcome, mid, order_size,
        num_tests=args.tests,
        use_user_ws=not args.no_user_ws,
        skip_cancel=args.no_cancel
    ))

if __name__ == "__main__":
    main()
