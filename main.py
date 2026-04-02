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
import os

# Intercept --profile early to ensure config.py loads it correctly
for i, arg in enumerate(sys.argv):
    if arg == "--profile" and i + 1 < len(sys.argv):
        os.environ["ACTIVE_PROFILE"] = sys.argv[i + 1]

from src.logging_config import setup_logging

CATEGORY_DISCOVERY_LEAD_S = 30 * 60


def cmd_run(args: argparse.Namespace) -> int:
    from src.bot import Bot, LAZY_SUB_MIN_DURATION, LAZY_SUB_LEAD_S
    from src.markets.fifteen_min import SUPPORTED_DURATIONS
    from src.utils.slug_helpers import slugs_for_timestamp
    from src.markets.discovery import discover_slugs
    from src.markets.fifteen_min import (
        get_current_interval_utc,
        get_next_interval_utc,
    )
    import time as _time

    setup_logging()

    durations = args.durations or sorted(SUPPORTED_DURATIONS)
    categories = args.categories or []
    markets = args.markets if args.markets is not None else ([] if categories else ["BTC"])
    now = int(_time.time())

    initial_slugs: list[str] = []
    if markets:
        for dur in durations:
            cur_ts = get_current_interval_utc(dur)
            nxt_ts = get_next_interval_utc(dur)

            # Apply lazy subscription: skip long-duration markets that are
            # too far from expiry (they'll be added later by the sub manager).
            if dur >= LAZY_SUB_MIN_DURATION:
                interval_s = dur * 60
                for ts in (cur_ts, nxt_ts):
                    end_time = ts + interval_s
                    if end_time - now <= LAZY_SUB_LEAD_S:
                        initial_slugs.extend(slugs_for_timestamp(markets, dur, ts))
            else:
                initial_slugs.extend(slugs_for_timestamp(markets, dur, cur_ts))
                initial_slugs.extend(slugs_for_timestamp(markets, dur, nxt_ts))

    for category in categories:
        initial_slugs.extend(
            discover_slugs(
                category,
                durations=durations,
                lead_time_seconds=CATEGORY_DISCOVERY_LEAD_S,
            )
        )

    # Deduplicate while preserving order.
    initial_slugs = list(dict.fromkeys(initial_slugs))

    if not initial_slugs:
        print("ERROR: No initial slugs generated. Check market/duration settings.")
        return 1

    # NOTE: Do NOT create SweepStrategy here — Bot creates it internally
    # so the shared hot_tokens set is wired between MarketWS and strategy.

    persist = args.persist.lower() in ("true", "1", "yes")

    bot = Bot(
        slugs=initial_slugs,
        strategy_name=args.strategy,
        dry_run=args.dry_run,
        db_path=args.db_path,
        dashboard_enabled=args.dashboard,
        price_threshold=args.price_threshold,
        early_tick_threshold=args.early_tick_threshold,
        market_selections=markets,
        durations=durations,
        category_paths=categories,
        discovery_refresh_s=args.discovery_refresh_s,
        claim_min_value=args.claim,
        claim_interval=args.claim_interval,
        persist=persist,
        fill_mode=args.fill_mode,
        tag=args.tag,
    )

    bot.run_sync()
    return 0


def _profile_path(base: str, ext: str, profile: int | None) -> str:
    if profile is not None:
        return base.replace(ext, f"_p{profile}{ext}")
    return base


def cmd_health(args: argparse.Namespace) -> int:
    from src.monitoring.health import HealthMonitor

    path = args.heartbeat_path
    if path == "/tmp/polymarket_bot_heartbeat.json" and args.profile is not None:
        path = _profile_path(path, ".json", args.profile)

    hb = HealthMonitor.read_heartbeat(path)
    if hb is None:
        print(f"No heartbeat file found at {path}. Is the bot running?")
        return 1
    print(json.dumps(hb, indent=2, default=str))
    return 0


def cmd_stats(args: argparse.Namespace) -> int:
    from src.storage.database import get_readonly_connection

    db = args.db_path
    if db == "data/bot.db" and args.profile is not None:
        db = _profile_path(db, ".db", args.profile)

    try:
        conn = get_readonly_connection(db)
    except Exception as e:
        print(f"Cannot open database: {e}")
        return 1

    profile_str = f" (Profile {args.profile})" if args.profile is not None else " (Default Profile)"
    print("=" * 50)
    print(f"  STRATEGY STATS SUMMARY{profile_str}")
    print(f"  Database: {db}")
    print("=" * 50)

    # Get list of strategies
    strategy_rows = conn.execute("SELECT DISTINCT strategy FROM orders UNION SELECT DISTINCT strategy FROM trades").fetchall()
    strategies = [r[0] for r in strategy_rows if r[0]]
    if not strategies:
        strategies = ["sweep"] # fallback if empty

    for strategy in strategies:
        print(f"\n  === Strategy: {strategy.upper()} ===")
        row = conn.execute("SELECT COUNT(*) FROM orders WHERE strategy = ?", (strategy,)).fetchone()
        total_orders = row[0] if row else 0

        row = conn.execute(
            "SELECT COUNT(*) FROM orders WHERE final_status = 'FILLED' AND strategy = ?", (strategy,)
        ).fetchone()
        filled = row[0] if row else 0

        row = conn.execute(
            "SELECT COUNT(*) FROM orders WHERE final_status IN ('REJECTED', 'FAILED') AND strategy = ?", (strategy,)
        ).fetchone()
        rejected = row[0] if row else 0

        print(f"  Total orders : {total_orders}")
        print(f"  Filled       : {filled}")
        print(f"  Rejected     : {rejected}")
        print(f"  Fill rate    : {filled / total_orders * 100:.1f}%" if total_orders > 0 else "  Fill rate    : N/A")

        row = conn.execute(
            "SELECT COALESCE(SUM(net_pnl), 0) FROM trades WHERE strategy = ?", (strategy,)
        ).fetchone()
        pnl = row[0] if row else 0
        print(f"  Total P&L    : ${pnl:.4f}")

        row = conn.execute(
            "SELECT COUNT(*) FROM trades WHERE net_pnl >= 0 AND strategy = ?", (strategy,)
        ).fetchone()
        wins = row[0] if row else 0
        row = conn.execute("SELECT COUNT(*) FROM trades WHERE strategy = ?", (strategy,)).fetchone()
        total_trades = row[0] if row else 0
        print(f"  Trades       : {total_trades}")
        print(f"  Win rate     : {wins / total_trades * 100:.1f}%" if total_trades > 0 else "  Win rate     : N/A")

        # 1. Rejection Breakdown
        if rejected > 0:
            print("\n  REJECTION BREAKDOWN")
            print("  " + "-" * 20)
            rejection_rows = conn.execute(
                "SELECT rejection_reason, COUNT(*) as count FROM orders WHERE final_status IN ('REJECTED', 'FAILED') AND strategy = ? GROUP BY rejection_reason ORDER BY count DESC", (strategy,)
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
            "FROM orders WHERE final_status = 'FILLED' AND strategy = ?", (strategy,)
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
            "SELECT slug, side, price, size, time_to_expiry_s, order_id FROM orders WHERE time_to_expiry_s < 0 AND final_status = 'FILLED' AND strategy = ? ORDER BY time_to_expiry_s ASC", (strategy,)
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
    parser.add_argument(
        "--profile", type=int, default=None,
        help="Profile number to use (overrides .env defaults with P1_, P2_ etc.)",
    )
    sub = parser.add_subparsers(dest="command", help="Available commands")

    # ── run ────────────────────────────────────────────────────────────────
    run_parser = sub.add_parser("run", help="Start the trading bot")
    run_parser.add_argument(
        "--strategy", type=str, default="sweep",
        choices=["sweep", "post_expiry", "aggressive_post_expiry", "gabagool", "gabagool_dual"],
        help="Trading strategy to use (default: sweep)",
    )
    run_parser.add_argument(
        "--profile", type=int, default=None,
        help="Profile number to use (overrides .env defaults with P1_, P2_ etc.)",
    )
    run_parser.add_argument(
        "--markets", nargs="+", default=None,
        help="Crypto markets to monitor (default: BTC unless --categories is set)",
    )
    run_parser.add_argument(
        "--categories", nargs="+", default=None,
        help="Category paths to auto-discover (example: weather/temperature)",
    )
    run_parser.add_argument(
        "--discovery-refresh-s", type=float, default=60.0,
        help="Category discovery refresh interval in seconds (default: 60)",
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
    run_parser.add_argument(
        "--persist", type=str, default="true",
        help="Enable database persistence (default: true). Use 'false' to run fully in-memory.",
    )
    run_parser.add_argument(
        "--claim", type=float, default=None, metavar="AMOUNT",
        help="Enable auto-claiming winnings >= AMOUNT USD (disabled if omitted)",
    )
    run_parser.add_argument(
        "--claim-interval", type=float, default=60.0,
        help="How often to check for redeemable positions in seconds (default: 60)",
    )
    run_parser.add_argument(
        "--fill-mode", type=str, default="book", choices=["book", "instant"],
        help="Fill simulation mode for dry-run (default: book). Only used with --dry-run.",
    )
    run_parser.add_argument(
        "--tag", type=str, default="",
        help="Session tag applied to all orders, trades, and decisions for later filtering.",
    )

    # ── health ─────────────────────────────────────────────────────────────
    health_parser = sub.add_parser("health", help="Print current health status")
    health_parser.add_argument(
        "--profile", type=int, default=None,
        help="Profile number (uses profile-namespaced heartbeat path)",
    )
    health_parser.add_argument(
        "--heartbeat-path",
        default="/tmp/polymarket_bot_heartbeat.json",
        help="Path to heartbeat file",
    )

    # ── stats ──────────────────────────────────────────────────────────────
    stats_parser = sub.add_parser("stats", help="Show strategy stats from SQLite")
    stats_parser.add_argument(
        "--profile", type=int, default=None,
        help="Profile number (uses profile-namespaced DB path)",
    )
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
