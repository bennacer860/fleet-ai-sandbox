#!/usr/bin/env python3
"""Collect full order book depth data for realistic ladder backtesting.

What this collects:
1. Full order book depth (20 levels) for BTC 5-min markets
2. Every trade (fill) with price, size, side
3. BTC spot price from Binance WebSocket
4. Market outcomes (which side won)

Output: Parquet file with per-tick snapshots including:
  - Full order book (bids/asks arrays)
  - BTC spot price
  - Trade data

Usage:
    python3 scripts/collect_ladder_data.py --n-events 100 --book-depth 20
    python3 scripts/collect_ladder_data.py --n-events 1000 --book-depth 20 --output data/backtest_data
"""

import argparse
import asyncio
import gc
import json
import os
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

import numpy as np
import pyarrow as pa
import pyarrow.parquet as pq

_ROOT = Path(__file__).resolve().parents[1]
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

from src.core.event_bus import EventBus
from src.gateway.market_ws import MarketWebSocket
from src.gateway.crypto_ws import CryptoWebSocket
from src.logging_config import get_logger
from src.markets.fifteen_min import (
    get_current_interval_utc,
    get_market_slug,
    get_next_interval_utc,
)

logger = get_logger(__name__)

# ---------------------------------------------------------------------------
# Data structures
# ---------------------------------------------------------------------------

# PyArrow schema for the output
SCHEMA = pa.schema([
    # Timestamp
    pa.field("ts", pa.float64()),           # unix timestamp (seconds)
    pa.field("ts_utc", pa.string()),        # ISO format

    # Market info
    pa.field("slug", pa.string()),
    pa.field("market", pa.string()),
    pa.field("window_start_ts", pa.int64()),
    pa.field("window_end_ts", pa.int64()),
    pa.field("offset_s", pa.float64()),     # seconds since window start

    # Order book for each outcome
    pa.field("outcome", pa.string()),
    pa.field("token_id", pa.string()),
    pa.field("best_bid", pa.float64()),
    pa.field("best_ask", pa.float64()),
    pa.field("best_bid_size", pa.float64()),
    pa.field("best_ask_size", pa.float64()),
    pa.field("bid_depth", pa.float64()),    # total size at all bid levels
    pa.field("ask_depth", pa.float64()),    # total size at all ask levels

    # Full order book depth (serialized as JSON string)
    pa.field("bids_json", pa.string()),     # [[price, size], ...]
    pa.field("asks_json", pa.string()),     # [[price, size], ...]

    # BTC spot price
    pa.field("binance_price", pa.float64()),
    pa.field("coinbase_price", pa.float64()),
    pa.field("kraken_price", pa.float64()),

    # Trade data (if applicable)
    pa.field("trade_seq", pa.int64()),
    pa.field("trade_price", pa.float64()),
    pa.field("trade_size", pa.float64()),
    pa.field("trade_side", pa.string()),
])


def _utc_now():
    return datetime.now(timezone.utc)


def _format_utc(ts):
    return datetime.fromtimestamp(ts, tz=timezone.utc).isoformat()


def _trim_levels(levels, depth):
    return [[float(price), float(size)] for price, size in levels[:depth]]


def _depth_size(levels, depth):
    return round(sum(float(size) for _, size in levels[:depth]), 6)


# ---------------------------------------------------------------------------
# Collector
# ---------------------------------------------------------------------------

async def collect(args):
    duration_minutes = args.duration
    duration_seconds = duration_minutes * 60
    n_events = args.n_events
    interval_s = args.interval
    book_depth = args.book_depth
    markets = args.markets
    output_dir = Path(args.output_dir)
    startup_wait = args.startup_wait

    # Plan windows
    first = get_current_interval_utc(duration_minutes)
    plans = []
    for i in range(n_events):
        ts = first + i * duration_seconds
        plans.append({
            "timestamp": ts,
            "slugs": {m: get_market_slug(m, duration_minutes, ts) for m in markets},
            "start_utc": _format_utc(ts),
            "end_utc": _format_utc(ts + duration_seconds),
        })

    if args.dry_run:
        print(json.dumps({"dry_run": True, "windows": plans}, indent=2))
        return

    output_dir.mkdir(parents=True, exist_ok=True)

    # Build output path
    start_str = _utc_now().strftime("%Y%m%dT%H%M%SZ")
    output_path = output_dir / f"ladder_data_{start_str}_N{n_events}_depth{book_depth}.parquet"
    output_path_tmp = output_path.with_suffix(".tmp.parquet")

    # Initialize WebSocket connections
    event_bus = EventBus()
    market_ws = MarketWebSocket(
        event_bus=event_bus,
        initial_slugs=[],
        book_event_filter=None,
    )
    crypto_ws = CryptoWebSocket(assets=list(markets))

    bus_task = asyncio.create_task(event_bus.run())
    ws_task = asyncio.create_task(market_ws.run())
    crypto_task = asyncio.create_task(crypto_ws.run())

    # Track active windows
    active_indices = list(range(min(2, len(plans))))
    next_index = len(active_indices)
    completed_indices = []

    # Subscribe to initial slugs
    for i in active_indices:
        for slug in plans[i]["slugs"].values():
            await market_ws.add_markets([slug])

    writer = None
    row_count = 0
    trade_cursor = 0
    sample_seq = 0
    start_time = time.time()

    try:
        await asyncio.sleep(startup_wait)

        # Create parquet writer
        writer = pq.ParquetWriter(output_path_tmp, SCHEMA, compression="zstd")

        while len(completed_indices) < len(plans):
            now = time.time()
            now_int = int(now)

            # Rotate completed windows
            rotate_out = []
            for idx in list(active_indices):
                if now_int >= plans[idx]["timestamp"] + duration_seconds:
                    rotate_out.append(idx)
            for idx in rotate_out:
                if idx in active_indices:
                    active_indices.remove(idx)
                if idx not in completed_indices:
                    completed_indices.append(idx)
                for slug in plans[idx]["slugs"].values():
                    await market_ws.remove_markets([slug])
                if next_index < len(plans):
                    active_indices.append(next_index)
                    for slug in plans[next_index]["slugs"].values():
                        await market_ws.add_markets([slug])
                    next_index += 1

            # Get BTC spot prices
            btc_price = crypto_ws.latest_prices.get("BTC")
            coinbase_p = crypto_ws.latest_prices.get("BTC_COINBASE")
            kraken_p = crypto_ws.latest_prices.get("BTC_KRAKEN")

            # Write trades first
            trades = list(market_ws.last_trade_prices)
            for trade_seq, trade in enumerate(trades[trade_cursor:], start=trade_cursor):
                slug = trade.get("slug", "")
                # Find which window this trade belongs to
                window_ts = 0
                for p in plans:
                    if slug in p["slugs"].values():
                        window_ts = p["timestamp"]
                        break
                offset = now - window_ts if window_ts else 0

                row = {
                    "ts": now,
                    "ts_utc": _format_utc(now),
                    "slug": slug,
                    "market": trade.get("market", ""),
                    "window_start_ts": window_ts,
                    "window_end_ts": window_ts + duration_seconds if window_ts else 0,
                    "offset_s": offset,
                    "outcome": trade.get("outcome", ""),
                    "token_id": trade.get("token_id", ""),
                    "best_bid": None,
                    "best_ask": None,
                    "best_bid_size": None,
                    "best_ask_size": None,
                    "bid_depth": None,
                    "ask_depth": None,
                    "bids_json": "[]",
                    "asks_json": "[]",
                    "binance_price": float(btc_price) if btc_price else None,
                    "coinbase_price": float(coinbase_p) if coinbase_p else None,
                    "kraken_price": float(kraken_p) if kraken_p else None,
                    "trade_seq": trade_seq,
                    "trade_price": trade.get("price"),
                    "trade_size": trade.get("size"),
                    "trade_side": trade.get("side"),
                }
                table = pa.Table.from_pydict({k: [v] for k, v in row.items()}, schema=SCHEMA)
                writer.write_table(table)
                row_count += 1
            trade_cursor = len(market_ws.last_trade_prices)

            # Write order book snapshots for each active window
            for idx in sorted(active_indices):
                plan = plans[idx]
                for market, slug in plan["slugs"].items():
                    token_ids = tuple(market_ws.token_ids.get(slug, ()))
                    if not token_ids:
                        continue

                    offset = now - plan["timestamp"]

                    for token_id in token_ids:
                        top = market_ws.best_prices.get(token_id, {})
                        book = market_ws.order_books.get(token_id, {})

                        if not book:
                            continue

                        bids = book.get("bids", ())
                        asks = book.get("asks", ())
                        outcome = market_ws.token_outcomes.get(token_id, "")

                        row = {
                            "ts": now,
                            "ts_utc": _format_utc(now),
                            "slug": slug,
                            "market": market,
                            "window_start_ts": plan["timestamp"],
                            "window_end_ts": plan["timestamp"] + duration_seconds,
                            "offset_s": offset,
                            "outcome": outcome,
                            "token_id": token_id,
                            "best_bid": float(top.get("bid", 0)) if top.get("bid") else None,
                            "best_ask": float(top.get("ask", 0)) if top.get("ask") else None,
                            "best_bid_size": float(bids[0][1]) if bids else None,
                            "best_ask_size": float(asks[0][1]) if asks else None,
                            "bid_depth": _depth_size(bids, book_depth),
                            "ask_depth": _depth_size(asks, book_depth),
                            "bids_json": json.dumps(_trim_levels(bids, book_depth)),
                            "asks_json": json.dumps(_trim_levels(asks, book_depth)),
                            "binance_price": float(btc_price) if btc_price else None,
                            "coinbase_price": float(coinbase_p) if coinbase_p else None,
                            "kraken_price": float(kraken_p) if kraken_p else None,
                            "trade_seq": None,
                            "trade_price": None,
                            "trade_size": None,
                            "trade_side": None,
                        }
                        table = pa.Table.from_pydict({k: [v] for k, v in row.items()}, schema=SCHEMA)
                        writer.write_table(table)
                        row_count += 1

            sample_seq += 1

            # Progress
            elapsed = time.time() - start_time
            if sample_seq % 10 == 0:
                pct = len(completed_indices) / len(plans) * 100
                print(f"  Progress: {len(completed_indices)}/{len(plans)} windows ({pct:.0f}%), "
                      f"{row_count} rows, {elapsed:.0f}s", end="\r")

            await asyncio.sleep(max(0.0, interval_s))

        print(f"\nDone. Collected {row_count} rows across {len(completed_indices)} windows in {time.time() - start_time:.0f}s")

    finally:
        if writer:
            writer.close()
        await market_ws.stop()
        await crypto_ws.stop()
        await event_bus.stop()
        ws_task.cancel()
        crypto_task.cancel()
        bus_task.cancel()
        await asyncio.gather(ws_task, crypto_task, bus_task, return_exceptions=True)

        # Rename tmp to final
        if output_path_tmp.exists():
            output_path_tmp.rename(output_path)
            print(f"Output: {output_path}")

    # Summary
    print(f"\n{'='*60}")
    print(f"  COLLECTION SUMMARY")
    print(f"{'='*60}")
    print(f"  Windows:     {len(completed_indices)}")
    print(f"  Rows:        {row_count:,}")
    print(f"  Duration:    {duration_minutes} min")
    print(f"  Book depth:  {book_depth}")
    print(f"  Interval:    {interval_s}s")
    print(f"  Markets:     {', '.join(markets)}")
    print(f"  Output:      {output_path}")


def main():
    parser = argparse.ArgumentParser(
        description="Collect full order book depth data for ladder strategy backtesting"
    )
    parser.add_argument("--n-events", type=int, default=100,
                        help="Number of 5-min windows to collect")
    parser.add_argument("--duration", type=int, default=5, choices=[5, 15],
                        help="Window duration in minutes")
    parser.add_argument("--interval", type=float, default=0.25,
                        help="Sampling interval in seconds")
    parser.add_argument("--book-depth", type=int, default=20,
                        help="Number of bid/ask levels to capture")
    parser.add_argument("--markets", nargs="+", default=["BTC", "ETH"],
                        help="Markets to collect")
    parser.add_argument("--output-dir", default="data/backtest_data",
                        help="Output directory")
    parser.add_argument("--startup-wait", type=float, default=5.0,
                        help="Seconds to wait for WebSocket warmup")
    parser.add_argument("--dry-run", action="store_true",
                        help="Print planned windows and exit")
    args = parser.parse_args()
    asyncio.run(collect(args))


if __name__ == "__main__":
    main()