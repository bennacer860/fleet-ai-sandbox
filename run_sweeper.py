#!/usr/bin/env python3
"""CLI entry point for the Polymarket endgame-sweep trading bot.

Continuously monitors 5-min and 15-min crypto up/down markets.  When the
tick size drops to 0.001 and the most-likely outcome price is >= the
configured threshold (default 0.9), a minimum-size limit BUY order is placed.

Usage examples:
    # Monitor BTC only (default)
    python run_sweeper.py

    # Monitor BTC and ETH with a 0.95 price threshold
    python run_sweeper.py --markets BTC ETH --price-threshold 0.95

    # Dry-run mode (logs decisions but does not submit orders)
    python run_sweeper.py --markets BTC ETH SOL XRP --dry-run
"""

import argparse
import sys

from src.logging_config import setup_logging
from src.markets.fifteen_min import MARKET_IDS, SUPPORTED_DURATIONS
from src.monitors.sweeper_bot import SweeperBot
from src.strategy.sweep_signal import DEFAULT_PRICE_THRESHOLD


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Polymarket Endgame-Sweep Trading Bot",
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument(
        "--markets",
        nargs="+",
        default=["BTC"],
        choices=list(MARKET_IDS.keys()),
        help="Crypto markets to monitor (default: BTC)",
    )
    parser.add_argument(
        "--durations",
        nargs="+",
        type=int,
        default=sorted(SUPPORTED_DURATIONS),
        choices=sorted(SUPPORTED_DURATIONS),
        help="Market durations in minutes to track (default: 5 15)",
    )
    parser.add_argument(
        "--price-threshold",
        type=float,
        default=DEFAULT_PRICE_THRESHOLD,
        help=f"Minimum outcome price to place an order (default: {DEFAULT_PRICE_THRESHOLD})",
    )
    parser.add_argument(
        "--output",
        default="sweeper_trades.csv",
        help="Unified CSV log file (default: sweeper_trades.csv)",
    )
    parser.add_argument(
        "--ws-url",
        default=None,
        help="WebSocket URL override",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Log order decisions without actually submitting orders",
    )
    parser.add_argument(
        "--decision-log",
        default="bot_decisions.csv",
        help="CSV file for detailed strategy decisions (default: bot_decisions.csv)",
    )

    args = parser.parse_args()

    setup_logging()

    bot = SweeperBot(
        market_selections=args.markets,
        durations=args.durations,
        price_threshold=args.price_threshold,
        output_file=args.output,
        ws_url=args.ws_url,
        dry_run=args.dry_run,
    )

    bot.run_sync()
    return 0


if __name__ == "__main__":
    sys.exit(main())
