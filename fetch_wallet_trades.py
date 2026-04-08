#!/usr/bin/env python3
"""CLI for fetching Polymarket wallet trade history and exporting to CSV.

Usage:
    python fetch_wallet_trades.py \
        --wallet 0xABC... \
        --start 2026-02-20 \
        --end 2026-02-21 \
        --output wallet_trades.csv \
        --min-price 0.95
"""

import argparse
import os
import shutil
import subprocess
import sys
import time
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

from pytz import timezone as pytz_timezone

from src.logging_config import setup_logging
from src.trade_fetcher import (
    fetch_trades_for_wallet_with_meta,
    print_summary,
    write_trades_csv,
)

DEFAULT_S3_PREFIX = "research/wallet-trades"


def parse_date(date_str: str) -> int:
    """
    Parse a YYYY-MM-DD date string into a Unix timestamp (start of day, EST).

    Args:
        date_str: Date in YYYY-MM-DD format.

    Returns:
        Unix timestamp (seconds) for start of the given day in EST.
    """
    est_tz = pytz_timezone("US/Eastern")
    dt = datetime.strptime(date_str, "%Y-%m-%d")
    dt_est = est_tz.localize(dt)
    return int(dt_est.timestamp())


def parse_date_end(date_str: str) -> int:
    """
    Parse a YYYY-MM-DD date string into a Unix timestamp for end of day (23:59:59 EST).

    Args:
        date_str: Date in YYYY-MM-DD format.

    Returns:
        Unix timestamp (seconds) for end of the given day in EST.
    """
    est_tz = pytz_timezone("US/Eastern")
    dt = datetime.strptime(date_str, "%Y-%m-%d").replace(hour=23, minute=59, second=59)
    dt_est = est_tz.localize(dt)
    return int(dt_est.timestamp())


def normalize_user_identifier(user: str) -> str:
    """Normalize wallet/handle/profile URL into a Data API user identifier."""
    raw = user.strip()
    if raw.startswith("http://") or raw.startswith("https://"):
        parsed = urlparse(raw)
        path = parsed.path.strip("/")
        if path.startswith("@"):
            return path
        if path:
            return path
    return raw


def resolve_date_range(args: argparse.Namespace) -> tuple[str, str]:
    """Resolve start/end date strings (YYYY-MM-DD) from flags."""
    est_tz = pytz_timezone("US/Eastern")
    if args.days is not None:
        if args.days <= 0:
            raise ValueError("--days must be > 0")
        if args.start:
            raise ValueError("Use either --start/--end or --days, not both")
        end_date = args.end if args.end else datetime.now(est_tz).strftime("%Y-%m-%d")
        end_dt = datetime.strptime(end_date, "%Y-%m-%d")
        start_dt = end_dt - timedelta(days=args.days - 1)
        return start_dt.strftime("%Y-%m-%d"), end_dt.strftime("%Y-%m-%d")

    if not args.start:
        raise ValueError("Either --start or --days is required")
    end_date = args.end if args.end else args.start
    return args.start, end_date


def _resolve_bucket(explicit: str) -> str:
    if explicit:
        return explicit
    for key in ("COLLECTOR_S3_BUCKET", "LOG_SYNC_S3_BUCKET", "S3_BUCKET"):
        val = os.getenv(key, "").strip()
        if val:
            return val
    return ""


def _resolve_region(explicit: str) -> str:
    if explicit:
        return explicit
    for key in ("COLLECTOR_S3_REGION", "LOG_SYNC_REGION", "AWS_REGION"):
        val = os.getenv(key, "").strip()
        if val:
            return val
    return "eu-west-1"


def _require_aws_cli() -> None:
    if shutil.which("aws"):
        return
    raise RuntimeError("AWS CLI not found in PATH; required for S3 upload")


def _build_s3_key(prefix: str, file_name: str) -> str:
    now = datetime.now(pytz_timezone("UTC"))
    date_prefix = now.strftime("%Y/%m/%d")
    base = prefix.strip("/")
    return f"{base}/{date_prefix}/{file_name}"


def _upload_with_retries(local_path: str, bucket: str, key: str, region: str, retries: int = 3) -> str:
    cmd = [
        "aws",
        "s3",
        "cp",
        local_path,
        f"s3://{bucket}/{key}",
        "--region",
        region,
        "--only-show-errors",
    ]
    delay_s = 2.0
    for attempt in range(1, retries + 1):
        result = subprocess.run(cmd, capture_output=True, text=True)
        if result.returncode == 0:
            return f"s3://{bucket}/{key}"
        if attempt == retries:
            stderr = result.stderr.strip() or "unknown upload error"
            raise RuntimeError(f"S3 upload failed after {retries} attempts: {stderr}")
        time.sleep(delay_s)
        delay_s *= 2.0
    raise RuntimeError("unreachable")


def _trade_dedupe_key(trade: dict[str, Any]) -> tuple[str, int, str, float, float, str]:
    return (
        str(trade.get("id", "")),
        int(trade.get("timestamp", 0)),
        str(trade.get("asset", "")),
        float(trade.get("price", 0)),
        float(trade.get("size", 0)),
        str(trade.get("side", "")),
    )


def _fetch_trades_adaptive(
    wallet: str,
    start_ts: int,
    end_ts: int,
    min_price: float | None,
    min_window_seconds: int,
    allow_partial: bool,
    depth: int = 0,
) -> tuple[list[dict[str, Any]], dict[str, int]]:
    """
    Fetch trades and recursively split windows when API pagination truncates results.
    """
    trades, meta = fetch_trades_for_wallet_with_meta(
        wallet=wallet,
        start_ts=start_ts,
        end_ts=end_ts,
        min_price=min_price,
    )
    stats = {
        "windows_fetched": 1,
        "windows_split": 0,
        "windows_truncated": 0,
        "max_depth": depth,
    }

    if not meta.get("possible_truncation"):
        return trades, stats

    stats["windows_truncated"] += 1
    window_seconds = end_ts - start_ts + 1
    if window_seconds <= min_window_seconds:
        msg = (
            "Result appears truncated by Data API pagination cap even at minimum window size: "
            f"window={window_seconds}s ({start_ts}..{end_ts}), "
            f"last_offset={meta.get('last_offset_attempted')}, status={meta.get('api_error_status')}."
        )
        if allow_partial:
            print(f"WARNING: {msg}")
            return trades, stats
        raise RuntimeError(f"{msg} Rerun with --allow-partial to keep partial output.")

    mid = (start_ts + end_ts) // 2
    if mid < start_ts or mid >= end_ts:
        msg = f"Unable to split window further: {start_ts}..{end_ts}."
        if allow_partial:
            print(f"WARNING: {msg}")
            return trades, stats
        raise RuntimeError(f"{msg} Rerun with --allow-partial to keep partial output.")

    est_tz = pytz_timezone("US/Eastern")
    left_dt = datetime.fromtimestamp(start_ts, tz=est_tz)
    right_dt = datetime.fromtimestamp(end_ts, tz=est_tz)
    print(
        "Splitting truncated window: "
        f"{left_dt:%Y-%m-%d %H:%M:%S} — {right_dt:%Y-%m-%d %H:%M:%S} EST "
        f"into [{start_ts}..{mid}] and [{mid + 1}..{end_ts}]"
    )

    left_trades, left_stats = _fetch_trades_adaptive(
        wallet=wallet,
        start_ts=start_ts,
        end_ts=mid,
        min_price=min_price,
        min_window_seconds=min_window_seconds,
        allow_partial=allow_partial,
        depth=depth + 1,
    )
    right_trades, right_stats = _fetch_trades_adaptive(
        wallet=wallet,
        start_ts=mid + 1,
        end_ts=end_ts,
        min_price=min_price,
        min_window_seconds=min_window_seconds,
        allow_partial=allow_partial,
        depth=depth + 1,
    )

    merged: list[dict[str, Any]] = []
    seen: set[tuple[str, int, str, float, float, str]] = set()
    for t in left_trades + right_trades:
        key = _trade_dedupe_key(t)
        if key in seen:
            continue
        seen.add(key)
        merged.append(t)

    stats["windows_split"] += 1
    stats["windows_fetched"] += left_stats["windows_fetched"] + right_stats["windows_fetched"]
    stats["windows_split"] += left_stats["windows_split"] + right_stats["windows_split"]
    stats["windows_truncated"] += left_stats["windows_truncated"] + right_stats["windows_truncated"]
    stats["max_depth"] = max(stats["max_depth"], left_stats["max_depth"], right_stats["max_depth"])
    return merged, stats


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Fetch Polymarket wallet trade history and export to CSV.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Examples:
  # Fetch all trades for a wallet on a specific day
  python fetch_wallet_trades.py --wallet 0xABC... --start 2026-02-20

  # Fetch trades in a date range with min price filter
  python fetch_wallet_trades.py --wallet 0xABC... --start 2026-02-19 --end 2026-02-20 --min-price 0.95

  # Fetch the last 7 days for a profile handle
  python fetch_wallet_trades.py --wallet @pbot-6 --days 7

  # Save to a custom output file
  python fetch_wallet_trades.py --wallet 0xABC... --start 2026-02-20 --output my_trades.csv
        """,
    )
    parser.add_argument(
        "--wallet",
        required=True,
        help="Polymarket user identifier: wallet (0x...), handle (@name), or profile URL",
    )
    parser.add_argument(
        "--start",
        default=None,
        help="Start date (inclusive, YYYY-MM-DD in EST)",
    )
    parser.add_argument(
        "--days",
        type=int,
        default=None,
        help="Lookback window in days ending at --end (or today if --end omitted). Example: --days 7",
    )
    parser.add_argument(
        "--end",
        default=None,
        help="End date (inclusive, YYYY-MM-DD in EST). Defaults to same as start date (or today with --days).",
    )
    parser.add_argument(
        "--output",
        default="wallet_trades.csv",
        help="Output CSV file path (default: wallet_trades.csv)",
    )
    parser.add_argument(
        "--min-price",
        type=float,
        default=None,
        help="Only include trades at or above this price (e.g. 0.95 for sweep analysis)",
    )
    parser.add_argument("--s3-prefix", default=DEFAULT_S3_PREFIX, help="S3 key prefix for uploaded CSV.")
    parser.add_argument("--s3-bucket", default="", help="S3 bucket override.")
    parser.add_argument("--s3-region", default="", help="S3 region override.")
    parser.add_argument("--no-upload", action="store_true", help="Disable S3 upload after CSV export.")
    parser.add_argument(
        "--allow-partial",
        action="store_true",
        help="Allow potentially incomplete results when Data API pagination cap blocks full lookback.",
    )
    parser.add_argument(
        "--min-window-minutes",
        type=int,
        default=15,
        help="Smallest adaptive split window when pagination truncates (default: 15).",
    )

    args = parser.parse_args()

    setup_logging()
    user_identifier = normalize_user_identifier(args.wallet)

    # Parse date range
    try:
        start_date, end_date = resolve_date_range(args)
    except ValueError as exc:
        parser.error(str(exc))

    start_ts = parse_date(start_date)
    end_ts = parse_date_end(end_date)

    est_tz = pytz_timezone("US/Eastern")
    start_dt = datetime.fromtimestamp(start_ts, tz=est_tz)
    end_dt = datetime.fromtimestamp(end_ts, tz=est_tz)

    print(f"User:       {user_identifier}")
    print(f"Date range: {start_dt:%Y-%m-%d %H:%M:%S} — {end_dt:%Y-%m-%d %H:%M:%S} EST")
    if args.min_price is not None:
        print(f"Min price:  {args.min_price}")
    print(f"Output:     {args.output}")
    print(f"S3 upload:  {'disabled' if args.no_upload else 'enabled'}")
    print()

    if args.min_window_minutes <= 0:
        parser.error("--min-window-minutes must be > 0")

    # Fetch trades with adaptive recursive splitting if API pagination truncates.
    trades, adaptive_stats = _fetch_trades_adaptive(
        wallet=user_identifier,
        start_ts=start_ts,
        end_ts=end_ts,
        min_price=args.min_price,
        min_window_seconds=args.min_window_minutes * 60,
        allow_partial=args.allow_partial,
    )
    trades.sort(key=lambda t: int(t.get("timestamp", 0)))
    print(f"Fetched {len(trades)} filtered trades.")
    print(
        "Adaptive fetch stats: "
        f"windows_fetched={adaptive_stats['windows_fetched']}, "
        f"windows_split={adaptive_stats['windows_split']}, "
        f"truncated_windows={adaptive_stats['windows_truncated']}, "
        f"max_depth={adaptive_stats['max_depth']}"
    )

    # Write CSV
    write_trades_csv(trades, args.output)
    print(f"Saved CSV:   {Path(args.output).resolve()}")

    if not args.no_upload:
        bucket = _resolve_bucket(args.s3_bucket)
        if not bucket:
            raise RuntimeError(
                "S3 upload enabled but no bucket configured. Set --s3-bucket or env "
                "COLLECTOR_S3_BUCKET/LOG_SYNC_S3_BUCKET/S3_BUCKET."
            )
        _require_aws_cli()
        region = _resolve_region(args.s3_region)
        s3_key = _build_s3_key(args.s3_prefix, Path(args.output).name)
        s3_uri = _upload_with_retries(args.output, bucket, s3_key, region)
        print(f"Uploaded:    {s3_uri}")

    # Print summary
    print_summary(trades)

    return 0


if __name__ == "__main__":
    sys.exit(main())
