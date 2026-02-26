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

    strategy = SweepStrategy(price_threshold=args.price_threshold)

    bot = Bot(
        slugs=initial_slugs,
        strategies=[strategy],
        dry_run=args.dry_run,
        db_path=args.db_path,
        dashboard_enabled=args.dashboard,
        price_threshold=args.price_threshold,
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

    print()
    print("  Recent decisions (last 10):")
    rows = conn.execute(
        "SELECT timestamp, strategy, slug, decision, reason FROM decisions ORDER BY id DESC LIMIT 10"
    ).fetchall()
    for r in rows:
        from datetime import datetime
        ts = datetime.fromtimestamp(r[0]).strftime("%m-%d %H:%M:%S")
        print(f"    {ts}  [{r[1]}] {r[2]:30s}  {r[3]:8s}  {r[4]}")

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
        "--price-threshold", type=float, default=0.95,
        help="Min outcome price to trigger sweep order (default: 0.95)",
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
