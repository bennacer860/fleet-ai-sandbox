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
    compute_and_write_positions_csv,
    closed_positions_pnl_index,
    fetch_closed_positions,
    fetch_market_outcomes,
    fetch_trades_for_wallet_with_meta,
    print_summary,
    write_closed_positions_csv,
    write_trades_csv,
)

DEFAULT_S3_PREFIX = "research/wallet-trades"

# Known handles → proxy wallets. Profile-page scraping (NEXT_DATA) is unreliable
# on EC2 (CloudFront blocks, no JS). Keep this map up to date when tracking new users.
KNOWN_WALLETS: dict[str, str] = {
    "certova": "0x8d1d5d1c6041b13fc708b5d9f668070e1724ed4a",
    "ivy56": "0xddb062ade7d4e92ef636a3bfb94a4e2feab30310",
}


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


def _resolve_handle_to_wallet(handle: str) -> str:
    """Resolve a Polymarket @handle to a proxy wallet address.

    Checks the KNOWN_WALLETS map first (reliable on EC2 where profile-page
    scraping fails), then falls back to scraping the Next.js payload.
    """
    import json
    import re

    import requests as _requests

    slug = handle.lstrip("@").lower()

    known = KNOWN_WALLETS.get(slug)
    if known:
        print(f"Resolved @{slug} → {known} (known wallet)")
        return known

    url = f"https://polymarket.com/@{slug}"
    print(f"Resolving @{slug} → wallet via {url}")
    try:
        resp = _requests.get(url, timeout=15, allow_redirects=True)
        resp.raise_for_status()
    except _requests.RequestException as exc:
        print(f"Failed to fetch profile page for @{slug}: {exc}")
        return handle

    m = re.search(
        r'<script id="__NEXT_DATA__"[^>]*>(.*?)</script>', resp.text
    )
    if not m:
        print(f"No __NEXT_DATA__ in profile page for @{slug}")
        return handle

    try:
        data = json.loads(m.group(1))
    except json.JSONDecodeError:
        print(f"Failed to parse __NEXT_DATA__ JSON for @{slug}")
        return handle

    props = data.get("props", {}).get("pageProps", {})

    for key in ("proxyAddress", "baseAddress", "primaryAddress"):
        addr = props.get(key, "")
        if addr and addr.startswith("0x"):
            print(f"Resolved @{slug} → {addr} (via pageProps.{key})")
            return addr

    def _find_wallet(obj):
        if isinstance(obj, dict):
            pw = obj.get("proxyWallet", "")
            if pw and pw.startswith("0x"):
                return pw
            for v in obj.values():
                result = _find_wallet(v)
                if result:
                    return result
        elif isinstance(obj, list):
            for v in obj:
                result = _find_wallet(v)
                if result:
                    return result
        return None

    wallet = _find_wallet(data)
    if wallet:
        print(f"Resolved @{slug} → {wallet} (via nested proxyWallet)")
        return wallet

    print(f"Could not resolve @{slug} to a wallet address")
    return handle


def normalize_user_identifier(user: str) -> str:
    """Normalize wallet/handle/profile URL into a Data API wallet address.

    The Polymarket Data API ``user`` param only filters correctly when given a
    ``0x…`` proxy-wallet address.  Passing an ``@handle`` silently returns
    **all** market trades.  This function detects handles and profile URLs and
    resolves them to wallet addresses via the profile page.
    """
    raw = user.strip()

    if raw.startswith("http://") or raw.startswith("https://"):
        parsed = urlparse(raw)
        path = parsed.path.strip("/")
        if path.startswith("@"):
            raw = path
        elif path.startswith("0x"):
            return path
        elif path:
            raw = path

    if raw.startswith("0x") and len(raw) == 42:
        return raw

    if raw.startswith("@") or (len(raw) > 2 and not raw.startswith("0x")):
        return _resolve_handle_to_wallet(raw)

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


def _label_for_wallet(raw: str) -> str:
    """Return a short, filename-safe label for a wallet identifier.

    @ivy56          → ivy56
    0xddb062...     → 0xddb062 (first 8 chars)
    https://…/@ivy56 → ivy56
    """
    clean = raw.strip()
    # Profile URL — extract the trailing handle or address segment
    if clean.startswith("http://") or clean.startswith("https://"):
        segment = clean.rstrip("/").split("/")[-1]
        clean = segment

    if clean.startswith("@"):
        return clean[1:]
    if clean.startswith("0x"):
        return clean[:8]
    return clean


def _build_output_path(output_dir: str, label: str, start_date: str, end_date: str) -> Path:
    """Return  <output_dir>/<label>_<start>_<end>.csv"""
    stem = f"{label}_{start_date}_{end_date}"
    return Path(output_dir) / f"{stem}.csv"


def _fetch_and_write(
    wallet_raw: str,
    start_date: str,
    end_date: str,
    start_ts: int,
    end_ts: int,
    output_dir: str,
    min_price: float | None,
    min_window_seconds: int,
    allow_partial: bool,
    with_pnl: bool,
    no_upload: bool,
    s3_bucket: str,
    s3_prefix: str,
    s3_region: str,
) -> int:
    """Fetch trades for one wallet and write CSV(s). Returns 0 on success."""
    label = _label_for_wallet(wallet_raw)
    wallet = normalize_user_identifier(wallet_raw)

    trades_path = _build_output_path(output_dir, label, start_date, end_date)
    trades_path.parent.mkdir(parents=True, exist_ok=True)

    est_tz = pytz_timezone("US/Eastern")
    start_dt = datetime.fromtimestamp(start_ts, tz=est_tz)
    end_dt = datetime.fromtimestamp(end_ts, tz=est_tz)

    print(f"\n{'='*60}")
    print(f"User:       {wallet_raw}  →  {wallet}")
    print(f"Date range: {start_dt:%Y-%m-%d} — {end_dt:%Y-%m-%d} EST")
    if min_price is not None:
        print(f"Min price:  {min_price}")
    print(f"Output:     {trades_path}")

    trades, adaptive_stats = _fetch_trades_adaptive(
        wallet=wallet,
        start_ts=start_ts,
        end_ts=end_ts,
        min_price=min_price,
        min_window_seconds=min_window_seconds,
        allow_partial=allow_partial,
    )
    trades.sort(key=lambda t: int(t.get("timestamp", 0)))
    print(
        f"Fetched {len(trades)} trades "
        f"(windows={adaptive_stats['windows_fetched']}, "
        f"splits={adaptive_stats['windows_split']})"
    )

    write_trades_csv(trades, str(trades_path))
    print(f"Saved:      {trades_path.resolve()}")

    if with_pnl:
        positions_path = trades_path.with_stem(trades_path.stem + "_positions")
        closed_path = trades_path.with_stem(trades_path.stem + "_closed_positions")

        print(f"Fetching closed-positions P&L for {wallet}...")
        closed = fetch_closed_positions(wallet, start_ts, end_ts)
        closed_total = write_closed_positions_csv(closed, str(closed_path))
        print(
            f"  Closed positions: {len(closed)}  "
            f"realizedPnl=${closed_total:,.2f}  -> {closed_path.resolve()}"
        )
        closed_idx = closed_positions_pnl_index(closed)

        # Gamma only for trade positions missing from closed-positions
        missing_cids = {
            t["condition_id"]
            for t in trades
            if t.get("condition_id")
            and (t["condition_id"], str(t.get("asset") or "")) not in closed_idx
        }
        outcomes: dict = {}
        if missing_cids:
            print(
                f"Fetching Gamma outcomes for {len(missing_cids)} markets "
                f"not in closed-positions..."
            )
            outcomes = fetch_market_outcomes(
                [t for t in trades if t.get("condition_id") in missing_cids]
            )
            resolved = sum(1 for v in outcomes.values() if v["resolved"])
            print(
                f"  Gamma resolved: {resolved}  "
                f"Unresolved: {len(outcomes) - resolved}"
            )
        else:
            print("  All trade positions covered by closed-positions; skipping Gamma")

        n, with_pnl_count = compute_and_write_positions_csv(
            trades, outcomes, str(positions_path), closed_pnl=closed_idx
        )
        print(
            f"  Positions CSV: {positions_path.resolve()} "
            f"({n} rows, {with_pnl_count} with P&L)"
        )

    if not no_upload:
        bucket = _resolve_bucket(s3_bucket)
        if not bucket:
            raise RuntimeError(
                "S3 upload enabled but no bucket configured. Set --s3-bucket or env "
                "COLLECTOR_S3_BUCKET/LOG_SYNC_S3_BUCKET/S3_BUCKET."
            )
        _require_aws_cli()
        region = _resolve_region(s3_region)
        s3_key = _build_s3_key(s3_prefix, trades_path.name)
        s3_uri = _upload_with_retries(str(trades_path), bucket, s3_key, region)
        print(f"Uploaded:   {s3_uri}")
        if with_pnl:
            for extra in (
                trades_path.with_stem(trades_path.stem + "_positions"),
                trades_path.with_stem(trades_path.stem + "_closed_positions"),
            ):
                if extra.exists():
                    pos_key = _build_s3_key(s3_prefix, extra.name)
                    pos_uri = _upload_with_retries(str(extra), bucket, pos_key, region)
                    print(f"Uploaded:   {pos_uri}")

    print_summary(trades)
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Fetch Polymarket wallet trade history and export to CSV.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Examples:
  # Single wallet, last 10 days
  python fetch_wallet_trades.py --wallet @ivy56 --days 10

  # Multiple wallets, explicit date range, saved to data/
  python fetch_wallet_trades.py --wallet @ivy56 @certova --start 2026-05-16 --end 2026-05-25 --output data/

  # Also write per-market P&L files
  python fetch_wallet_trades.py --wallet @ivy56 @certova --days 10 --output data/ --with-pnl --no-upload
        """,
    )
    parser.add_argument(
        "--wallet",
        required=True,
        nargs="+",
        help="One or more Polymarket identifiers: wallet (0x...), handle (@name), or profile URL",
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
        help="Lookback window in days ending at --end (or today if omitted). Example: --days 10",
    )
    parser.add_argument(
        "--end",
        default=None,
        help="End date (inclusive, YYYY-MM-DD in EST). Defaults to today when using --days.",
    )
    parser.add_argument(
        "--output",
        default="data/",
        help=(
            "Output directory for CSV files (default: data/). "
            "Files are named <label>_<start>_<end>.csv automatically."
        ),
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
    parser.add_argument(
        "--with-pnl",
        action="store_true",
        help=(
            "Fetch market outcomes from the Gamma API and write a separate "
            "<label>_<start>_<end>_positions.csv with one row per condition_id "
            "containing resolved P&L (pnl, winner, buy_cost, net_shares, etc.). "
            "The trades CSV is unchanged."
        ),
    )

    args = parser.parse_args()
    setup_logging()

    if args.min_window_minutes <= 0:
        parser.error("--min-window-minutes must be > 0")

    try:
        start_date, end_date = resolve_date_range(args)
    except ValueError as exc:
        parser.error(str(exc))

    start_ts = parse_date(start_date)
    end_ts = parse_date_end(end_date)

    print(f"Date range: {start_date} — {end_date} EST")
    print(f"Output dir: {Path(args.output).resolve()}")
    print(f"S3 upload:  {'disabled' if args.no_upload else 'enabled'}")

    exit_code = 0
    for wallet_raw in args.wallet:
        try:
            _fetch_and_write(
                wallet_raw=wallet_raw,
                start_date=start_date,
                end_date=end_date,
                start_ts=start_ts,
                end_ts=end_ts,
                output_dir=args.output,
                min_price=args.min_price,
                min_window_seconds=args.min_window_minutes * 60,
                allow_partial=args.allow_partial,
                with_pnl=args.with_pnl,
                no_upload=args.no_upload,
                s3_bucket=args.s3_bucket,
                s3_prefix=args.s3_prefix,
                s3_region=args.s3_region,
            )
        except Exception as exc:
            print(f"\nERROR processing {wallet_raw}: {exc}")
            exit_code = 1

    return exit_code


if __name__ == "__main__":
    sys.exit(main())
