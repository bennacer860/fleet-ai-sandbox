"""Trade fetcher for retrieving wallet trade history from Polymarket Data API."""

import csv
import time
from datetime import datetime
from typing import Any, Optional

import requests
from pytz import timezone as pytz_timezone

from .logging_config import get_logger
from .markets.fifteen_min import detect_duration_from_slug, duration_label

logger = get_logger(__name__)

# Polymarket Data API base URL
DATA_API_BASE = "https://data-api.polymarket.com"

# Pagination settings
DEFAULT_LIMIT = 10000
RATE_LIMIT_DELAY = 0.5  # seconds between paginated requests

# Known crypto prefixes for slug formatting
CRYPTO_PREFIXES = {"btc", "eth", "sol", "xrp"}

# CSV columns for output
CSV_COLUMNS = [
    "id",
    "timestamp",
    "timestamp_iso",
    "timestamp_est",
    "wallet",
    "side",
    "price",
    "size",
    "usdc_value",
    "asset",
    "condition_id",
    "outcome",
    "event_slug",
    "transaction_hash",
    "fee_rate",
]


def format_slug_with_est_time(slug: str, timestamp_ms: Optional[int] = None) -> str:
    """
    Format event slug with EST date and time.

    Converts raw API slugs to human-readable formatted slugs:
      "btc-updown-15m-1707523200" -> "btc-15min-up-or-down-2026-02-20-16:15"
      "btc-updown-5m-1707523200"  -> "btc-5min-up-or-down-2026-02-20-16:05"

    The duration (5min/15min) is auto-detected from the raw slug.
    Including the date eliminates ambiguity between same-time different-day markets,
    which is critical for reliable cross-referencing with sweeper data.

    This is a standalone version of MultiEventMonitor._format_slug_with_est_time()
    so both the monitor and trade fetcher produce identical event_slug values.

    Args:
        slug: Original event slug (e.g. "btc-updown-15m-1707523200")
        timestamp_ms: Optional timestamp in milliseconds (fallback if slug has no timestamp)

    Returns:
        Formatted slug with EST date+time, e.g. "btc-15min-up-or-down-2026-02-20-16:15"
    """
    slug_lower = slug.lower()

    # Detect duration from raw slug (defaults to 15min if undetectable)
    detected_dur = detect_duration_from_slug(slug)
    dur_label = duration_label(detected_dur if detected_dur is not None else 15)

    # Detect crypto prefix
    crypto = None
    for prefix in CRYPTO_PREFIXES:
        if slug_lower.startswith(prefix):
            crypto = prefix
            break

    # Try to extract Unix timestamp from last segment of slug
    timestamp = None
    parts = slug.split("-")
    if len(parts) >= 2:
        try:
            timestamp = int(parts[-1])
        except (ValueError, TypeError):
            pass

    # Fallback: use provided timestamp_ms or current time
    if timestamp is None:
        if timestamp_ms:
            timestamp = timestamp_ms // 1000
        else:
            timestamp = int(datetime.now(pytz_timezone("UTC")).timestamp())

    # Convert to EST
    est_tz = pytz_timezone("US/Eastern")
    try:
        dt = datetime.fromtimestamp(timestamp, tz=est_tz)
    except (OSError, ValueError):
        dt = datetime.fromtimestamp(
            timestamp, tz=pytz_timezone("UTC")
        ).astimezone(est_tz)

    date_str = dt.strftime("%Y-%m-%d")
    time_str = dt.strftime("%H:%M")

    if crypto:
        return f"{crypto}-{dur_label}-up-or-down-{date_str}-{time_str}"

    # Fallback: strip numeric tail and append date+time
    if parts and parts[-1].isdigit():
        prefix = "-".join(parts[:-1])
    else:
        prefix = slug

    return f"{prefix}-{date_str}-{time_str}"


def fetch_trades_for_wallet(
    wallet: str,
    start_ts: int,
    end_ts: int,
    min_price: Optional[float] = None,
) -> list[dict[str, Any]]:
    """
    Fetch all trades for a wallet address within a date range from the Polymarket Data API.

    Paginates through the /trades endpoint, applies client-side date filtering,
    and enriches each record with derived fields (ISO/EST timestamps, USDC value,
    formatted event_slug).

    Args:
        wallet: Polymarket proxy wallet address (0x...)
        start_ts: Start of date range as Unix timestamp (inclusive)
        end_ts: End of date range as Unix timestamp (inclusive)
        min_price: Optional minimum price filter (e.g. 0.95 for sweep detection)

    Returns:
        List of enriched trade dicts ready for CSV output.
    """
    all_trades: list[dict[str, Any]] = []
    offset = 0
    est_tz = pytz_timezone("US/Eastern")
    utc_tz = pytz_timezone("UTC")

    logger.info(
        "Fetching trades: wallet=%s, start=%d, end=%d, min_price=%s",
        wallet,
        start_ts,
        end_ts,
        min_price,
    )

    while True:
        url = f"{DATA_API_BASE}/trades"
        params = {
            "user": wallet,
            "limit": DEFAULT_LIMIT,
            "offset": offset,
            "start": start_ts,
            "end": end_ts,
            "takerOnly": "false",
        }

        logger.info("Requesting trades: offset=%d, limit=%d, start=%d, end=%d", offset, DEFAULT_LIMIT, start_ts, end_ts)
        t0 = time.perf_counter()

        try:
            resp = requests.get(url, params=params, timeout=60)
            resp.raise_for_status()
        except requests.exceptions.RequestException:
            logger.exception("API request failed at offset=%d", offset)
            break

        data = resp.json()
        elapsed_ms = (time.perf_counter() - t0) * 1000
        logger.info(
            "Received %d trades (offset=%d, latency=%.0fms)",
            len(data),
            offset,
            elapsed_ms,
        )

        if not data:
            logger.info("No more trades returned. Pagination complete.")
            break

        for trade in data:
            # Parse timestamp — API returns ISO string or Unix seconds
            raw_ts = trade.get("matchTime") or trade.get("timestamp") or trade.get("createdAt")
            if raw_ts is None:
                continue

            # Handle both numeric and ISO-format timestamps
            if isinstance(raw_ts, (int, float)):
                trade_ts = int(raw_ts)
            elif isinstance(raw_ts, str):
                try:
                    # Try parsing ISO format
                    dt_parsed = datetime.fromisoformat(raw_ts.replace("Z", "+00:00"))
                    trade_ts = int(dt_parsed.timestamp())
                except ValueError:
                    try:
                        trade_ts = int(raw_ts)
                    except ValueError:
                        logger.warning("Unparseable timestamp: %s", raw_ts)
                        continue
            else:
                continue

            # Date range filter
            if trade_ts < start_ts or trade_ts > end_ts:
                continue

            price = float(trade.get("price", 0))

            # Optional min-price filter
            if min_price is not None and price < min_price:
                continue

            size = float(trade.get("size", 0))

            # Build ISO and EST timestamps
            dt_utc = datetime.fromtimestamp(trade_ts, tz=utc_tz)
            dt_est = dt_utc.astimezone(est_tz)

            # Format event slug to match sweeper_analysis.csv
            raw_slug = trade.get("market_slug") or trade.get("slug") or ""
            event_slug = format_slug_with_est_time(raw_slug) if raw_slug else ""

            enriched = {
                "id": trade.get("id", ""),
                "timestamp": trade_ts,
                "timestamp_iso": dt_utc.strftime("%Y-%m-%d %H:%M:%S"),
                "timestamp_est": dt_est.strftime("%Y-%m-%d %H:%M:%S"),
                "wallet": wallet,
                "side": trade.get("side", ""),
                "price": price,
                "size": size,
                "usdc_value": round(price * size, 6),
                "asset": trade.get("asset", ""),
                "condition_id": trade.get("conditionId", ""),
                "outcome": trade.get("outcome", ""),
                "event_slug": event_slug,
                "transaction_hash": trade.get("transactionHash", ""),
                "fee_rate": trade.get("feeRateBps", ""),
            }
            all_trades.append(enriched)

        # If fewer results than limit, we've reached the end
        if len(data) < DEFAULT_LIMIT:
            logger.info("Last page received (%d < %d). Done.", len(data), DEFAULT_LIMIT)
            break

        offset += DEFAULT_LIMIT
        time.sleep(RATE_LIMIT_DELAY)

    logger.info("Total trades fetched and filtered: %d", len(all_trades))
    return all_trades


def write_trades_csv(trades: list[dict[str, Any]], output_path: str) -> None:
    """
    Write enriched trade records to a CSV file.

    Args:
        trades: List of enriched trade dicts (from fetch_trades_for_wallet).
        output_path: File path for the output CSV.
    """
    with open(output_path, "w", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=CSV_COLUMNS)
        writer.writeheader()
        writer.writerows(trades)

    logger.info("Wrote %d trades to %s", len(trades), output_path)


def print_summary(trades: list[dict[str, Any]]) -> None:
    """
    Print summary statistics for fetched trades to stdout.

    Args:
        trades: List of enriched trade dicts.
    """
    if not trades:
        print("\nNo trades found for the given wallet and date range.")
        return

    total = len(trades)
    buys = [t for t in trades if t["side"] == "BUY"]
    sells = [t for t in trades if t["side"] == "SELL"]
    total_volume = sum(t["usdc_value"] for t in trades)
    prices = [t["price"] for t in trades]

    # Price distribution buckets relevant to sweep detection
    above_99 = len([p for p in prices if p >= 0.99])
    above_95 = len([p for p in prices if p >= 0.95])
    below_95 = len([p for p in prices if p < 0.95])

    # Unique markets
    unique_slugs = set(t["event_slug"] for t in trades if t["event_slug"])
    unique_assets = set(t["asset"] for t in trades if t["asset"])

    # Date range of actual trades
    timestamps = [t["timestamp"] for t in trades]
    first_trade = min(timestamps)
    last_trade = max(timestamps)
    est_tz = pytz_timezone("US/Eastern")
    first_dt = datetime.fromtimestamp(first_trade, tz=est_tz)
    last_dt = datetime.fromtimestamp(last_trade, tz=est_tz)

    # Market/Asset aggregation for P&L tracking
    market_stats = {} # (event_slug, asset, outcome) -> {buys: usdc, sells: usdc, buy_size: size, sell_size: size}
    for t in trades:
        key = (t["event_slug"], t["asset"], t["outcome"])
        if key not in market_stats:
            market_stats[key] = {"buy_usdc": 0.0, "sell_usdc": 0.0, "buy_size": 0.0, "sell_size": 0.0}
        
        if t["side"] == "BUY":
            market_stats[key]["buy_usdc"] += t["usdc_value"]
            market_stats[key]["buy_size"] += t["size"]
        else:
            market_stats[key]["sell_usdc"] += t["usdc_value"]
            market_stats[key]["sell_size"] += t["size"]

    print("\n" + "=" * 60)
    print("WALLET TRADE SUMMARY")
    print("=" * 60)
    print(f"  Wallet:           {trades[0]['wallet']}")
    print(f"  Date range:       {first_dt:%Y-%m-%d %H:%M} — {last_dt:%Y-%m-%d %H:%M} EST")
    print(f"  Total trades:     {total}")
    print(f"  Buys:             {len(buys)}")
    print(f"  Sells:            {len(sells)}")
    print(f"  Total volume:     ${total_volume:,.2f}")
    print(f"  Avg trade size:   ${total_volume / total:,.2f}")
    print(f"  Unique markets:   {len(unique_slugs)}")
    print(f"  Unique tokens:    {len(unique_assets)}")
    print()
    print("  Financials (Aggregate):")
    total_buy_vol = sum(s["buy_usdc"] for s in market_stats.values())
    total_sell_vol = sum(s["sell_usdc"] for s in market_stats.values())
    print(f"    Total spent:    ${total_buy_vol:,.2f}")
    print(f"    Total sold:     ${total_sell_vol:,.2f}")
    print(f"    Net spend:      ${(total_buy_vol - total_sell_vol):,.2f}")
    print()
    print("  Price distribution:")
    print(f"    >= 0.99:        {above_99:>6d}  ({above_99 / total * 100:.1f}%)")
    print(f"    >= 0.95:        {above_95:>6d}  ({above_95 / total * 100:.1f}%)")
    print(f"    <  0.95:        {below_95:>6d}  ({below_95 / total * 100:.1f}%)")

    if above_95 / total > 0.5:
        print()
        print("  ⚠  >50% of trades at price >= 0.95 — consistent with endgame sweep pattern")

    print("=" * 60)
