#!/usr/bin/env python3
"""Unified CLI for the Polymarket HFT Bot v1.

Usage:
    python main.py run
    python main.py run --dry-run
    python main.py run --dry-run --dashboard
    python main.py run --markets BTC ETH --durations 5 15 --price-threshold 0.95
    python main.py health
    python main.py stats
"""

from __future__ import annotations

import argparse
import json
import sys

from src.logging_config import setup_logging


def cmd_run(args: argparse.Namespace) -> int:
    from src.bot import Bot
    from src.markets.fifteen_min import SUPPORTED_DURATIONS
    from src.strategy.sweep import SweepStrategy
    from src.utils.slug_helpers import slugs_for_timestamp
    from src.markets.fifteen_min import (
        get_current_interval_utc,
        get_next_interval_utc,
    )

    setup_logging()

    durations = args.durations or sorted(SUPPORTED_DURATIONS)
    markets = args.markets

    initial_slugs: list[str] = []
    for dur in durations:
        cur_ts = get_current_interval_utc(dur)
        nxt_ts = get_next_interval_utc(dur)
        initial_slugs.extend(slugs_for_timestamp(markets, dur, cur_ts))
        initial_slugs.extend(slugs_for_timestamp(markets, dur, nxt_ts))

    if not initial_slugs:
        print("ERROR: No initial slugs generated. Check market/duration settings.")
        return 1

    strategy = SweepStrategy(
        price_threshold=args.price_threshold,
        early_tick_threshold=args.early_tick_threshold,
    )

    bot = Bot(
        slugs=initial_slugs,
        strategies=[strategy],
        dry_run=args.dry_run,
        db_path=args.db_path,
        dashboard_enabled=args.dashboard,
        price_threshold=args.price_threshold,
        market_selections=markets,
        durations=durations,
    )

    bot.run_sync()
    return 0


def cmd_health(args: argparse.Namespace) -> int:
    from src.monitoring.health import HealthMonitor

    hb = HealthMonitor.read_heartbeat(args.heartbeat_path)
    if hb is None:
        print("No heartbeat file found. Is the bot running?")
        return 1
    print(json.dumps(hb, indent=2, default=str))
    return 0


def cmd_stats(args: argparse.Namespace) -> int:
    from src.storage.database import get_readonly_connection

    try:
        conn = get_readonly_connection(args.db_path)
    except Exception as e:
        print(f"Cannot open database: {e}")
        return 1

    print("=" * 50)
    print("  STRATEGY STATS SUMMARY")
    print("=" * 50)

    row = conn.execute("SELECT COUNT(*) FROM orders").fetchone()
    total_orders = row[0] if row else 0

    row = conn.execute(
        "SELECT COUNT(*) FROM orders WHERE final_status = 'FILLED'"
    ).fetchone()
    filled = row[0] if row else 0

    row = conn.execute(
        "SELECT COUNT(*) FROM orders WHERE final_status IN ('REJECTED', 'FAILED')"
    ).fetchone()
    rejected = row[0] if row else 0

    print(f"  Total orders : {total_orders}")
    print(f"  Filled       : {filled}")
    print(f"  Rejected     : {rejected}")
    print(f"  Fill rate    : {filled / total_orders * 100:.1f}%" if total_orders > 0 else "  Fill rate    : N/A")

    row = conn.execute(
        "SELECT COALESCE(SUM(net_pnl), 0) FROM trades"
    ).fetchone()
    pnl = row[0] if row else 0
    print(f"  Total P&L    : ${pnl:.4f}")

    row = conn.execute(
        "SELECT COUNT(*) FROM trades WHERE net_pnl >= 0"
    ).fetchone()
    wins = row[0] if row else 0
    row = conn.execute("SELECT COUNT(*) FROM trades").fetchone()
    total_trades = row[0] if row else 0
    print(f"  Trades       : {total_trades}")
    print(f"  Win rate     : {wins / total_trades * 100:.1f}%" if total_trades > 0 else "  Win rate     : N/A")

    # 1. Rejection Breakdown
    if rejected > 0:
        print("\n  REJECTION BREAKDOWN")
        print("  " + "-" * 20)
        rejection_rows = conn.execute(
            "SELECT rejection_reason, COUNT(*) as count FROM orders WHERE final_status IN ('REJECTED', 'FAILED') GROUP BY rejection_reason ORDER BY count DESC"
        ).fetchall()
        for r in rejection_rows:
            reason = r[0] if r[0] else "Unknown/None"
            # Truncate and clean up HTML blobs
            if "<html>" in reason.lower() or "<!doctype" in reason.lower():
                reason = "HTML Error Page (e.g. 502 Bad Gateway)"
            elif len(reason) > 100:
                reason = reason[:97] + "..."
            print(f"    {r[1]:3d}x  {reason}")

    # 2. Timing Analysis (for FILLED orders)
    timing_row = conn.execute(
        "SELECT AVG(time_to_expiry_s), MIN(time_to_expiry_s), MAX(time_to_expiry_s), "
        "AVG(signal_to_rest_ms), AVG(signal_to_fill_ms) "
        "FROM orders WHERE final_status = 'FILLED'"
    ).fetchone()
    if timing_row and timing_row[0] is not None:
        print("\n  TIMING ANALYSIS (Filled Orders)")
        print("  " + "-" * 32)
        print(f"    Avg Time to Expiry : {timing_row[0]:.2f}s")
        print(f"    Min Time to Expiry : {timing_row[1]:.2f}s")
        print(f"    Max Time to Expiry : {timing_row[2]:.2f}s")
        if timing_row[3] is not None:
            print(f"    Avg Placement Lat. : {timing_row[3]:.0f}ms")
        if timing_row[4] is not None:
            print(f"    Avg Fill Latency   : {timing_row[4]:.0f}ms")

    # 3. Late Trades (after expiration)
    late_rows = conn.execute(
        "SELECT slug, side, price, size, time_to_expiry_s, order_id FROM orders WHERE time_to_expiry_s < 0 AND final_status = 'FILLED' ORDER BY time_to_expiry_s ASC"
    ).fetchall()
    if late_rows:
        from src.utils.timestamps import format_slug_with_est_time
        print(f"\n  LATE TRADES ({len(late_rows)} filled after Expiry)")
        print("  " + "-" * 60)
        for r in late_rows:
            raw_slug = r[0]
            display_slug = format_slug_with_est_time(raw_slug)
            seconds_late = abs(r[4])
            print(f"    {display_slug:<45s}  {r[1]} {r[2]:.4f} x {r[3]:<6.1f}  {seconds_late:.1f}s LATE")

    print("\n  Recent decisions (last 10):")
    rows = conn.execute(
        "SELECT timestamp, strategy, slug, decision, reason FROM decisions ORDER BY id DESC LIMIT 10"
    ).fetchall()
    
    from src.utils.timestamps import format_slug_with_est_time
    from datetime import datetime

    for r in rows:
        ts = datetime.fromtimestamp(r[0]).strftime("%m-%d %H:%M:%S")
        raw_slug = r[2]
        display_slug = format_slug_with_est_time(raw_slug)
        print(f"    {ts}  [{r[1]}] {display_slug}")
        print(f"    {'':17}  {r[3]:8s}  {r[4]}")

    conn.close()
    print("=" * 50)
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Polymarket HFT Bot v1",
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    sub = parser.add_subparsers(dest="command", help="Available commands")

    # ── run ────────────────────────────────────────────────────────────────
    run_parser = sub.add_parser("run", help="Start the trading bot")
    run_parser.add_argument(
        "--markets", nargs="+", default=["BTC"],
        help="Crypto markets to monitor (default: BTC)",
    )
    run_parser.add_argument(
        "--durations", nargs="+", type=int, default=None,
        help="Market durations in minutes (default: all supported)",
    )
    run_parser.add_argument(
        "--price-threshold", type=float, default=0.99,
        help="Min outcome price to trigger sweep order (default: 0.99)",
    )
    run_parser.add_argument(
        "--early-tick-threshold", type=float, default=0.995,
        help="Stricter price threshold for markets where tick changed too early (default: 0.995)",
    )
    run_parser.add_argument(
        "--dry-run", action="store_true",
        help="Monitor and evaluate but never submit orders",
    )
    run_parser.add_argument(
        "--dashboard", action="store_true",
        help="Show live terminal dashboard instead of log output",
    )
    run_parser.add_argument(
        "--db-path", default=None,
        help="SQLite database path (default: data/bot.db)",
    )

    # ── health ─────────────────────────────────────────────────────────────
    health_parser = sub.add_parser("health", help="Print current health status")
    health_parser.add_argument(
        "--heartbeat-path",
        default="/tmp/polymarket_bot_heartbeat.json",
        help="Path to heartbeat file",
    )

    # ── stats ──────────────────────────────────────────────────────────────
    stats_parser = sub.add_parser("stats", help="Show strategy stats from SQLite")
    stats_parser.add_argument(
        "--db-path", default="data/bot.db",
        help="SQLite database path (default: data/bot.db)",
    )

    args = parser.parse_args()

    if args.command == "run":
        return cmd_run(args)
    elif args.command == "health":
        return cmd_health(args)
    elif args.command == "stats":
        return cmd_stats(args)
    else:
        parser.print_help()
        return 0


if __name__ == "__main__":
    sys.exit(main())
