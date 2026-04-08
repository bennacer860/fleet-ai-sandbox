"""Trade fetcher for retrieving wallet trade history from Polymarket Data API."""

import csv
import time
from datetime import datetime
from typing import Any, Optional

import requests
from pytz import timezone as pytz_timezone

from .logging_config import get_logger
from .markets.fifteen_min import detect_duration_from_slug, duration_label, extract_market_end_ts

logger = get_logger(__name__)

# Polymarket Data API base URL
DATA_API_BASE = "https://data-api.polymarket.com"

# Pagination settings
DEFAULT_LIMIT = 1000
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
    "expiry_ts",
    "is_post_expiry",
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
    Backward-compatible wrapper returning only trades.

    For pagination/error metadata, use fetch_trades_for_wallet_with_meta().
    """
    trades, _ = fetch_trades_for_wallet_with_meta(
        wallet=wallet,
        start_ts=start_ts,
        end_ts=end_ts,
        min_price=min_price,
    )
    return trades


def fetch_trades_for_wallet_with_meta(
    wallet: str,
    start_ts: int,
    end_ts: int,
    min_price: Optional[float] = None,
) -> tuple[list[dict[str, Any]], dict[str, Any]]:
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
        Tuple of:
          - List of enriched trade dicts ready for CSV output.
          - Metadata dict with pagination/error details.
    """
    all_trades: list[dict[str, Any]] = []
    offset = 0
    est_tz = pytz_timezone("US/Eastern")
    utc_tz = pytz_timezone("UTC")
    pages_fetched = 0
    oldest_payload_ts: int | None = None
    newest_payload_ts: int | None = None
    api_error_status: int | None = None
    api_error_message: str | None = None
    api_error_offset: int | None = None

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
        except requests.exceptions.RequestException as exc:
            logger.exception("API request failed at offset=%d", offset)
            api_error_offset = offset
            api_error_status = (
                exc.response.status_code if getattr(exc, "response", None) is not None else None
            )
            api_error_message = str(exc)
            break

        data = resp.json()
        pages_fetched += 1
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

            # Track payload bounds before client-side filters.
            if oldest_payload_ts is None or trade_ts < oldest_payload_ts:
                oldest_payload_ts = trade_ts
            if newest_payload_ts is None or trade_ts > newest_payload_ts:
                newest_payload_ts = trade_ts

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

            # Format event_slug and check for post-expiry
            raw_slug = trade.get("market_slug") or trade.get("slug") or ""
            event_slug = ""
            expiry_ts = None
            is_post_expiry = False
            
            if raw_slug:
                event_slug = format_slug_with_est_time(raw_slug)
                expiry_ts = extract_market_end_ts(raw_slug)
                if expiry_ts and trade_ts > expiry_ts:
                    is_post_expiry = True

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
                "expiry_ts": expiry_ts,
                "is_post_expiry": is_post_expiry,
            }
            all_trades.append(enriched)

        # If fewer results than limit, we've reached the end
        if len(data) < DEFAULT_LIMIT:
            logger.info("Last page received (%d < %d). Done.", len(data), DEFAULT_LIMIT)
            break

        offset += DEFAULT_LIMIT
        time.sleep(RATE_LIMIT_DELAY)

    possible_truncation = (
        api_error_status == 400 and api_error_offset is not None and api_error_offset >= 3000
    )
    meta = {
        "pages_fetched": pages_fetched,
        "last_offset_attempted": api_error_offset if api_error_offset is not None else offset,
        "api_error_status": api_error_status,
        "api_error_message": api_error_message,
        "api_error_offset": api_error_offset,
        "possible_truncation": possible_truncation,
        "oldest_payload_ts": oldest_payload_ts,
        "newest_payload_ts": newest_payload_ts,
    }
    logger.info("Total trades fetched and filtered: %d", len(all_trades))
    return all_trades, meta


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
    Print summary statistics for fetched trades to stdout, including P&L.

    Args:
        trades: List of enriched trade dicts.
    """
    if not trades:
        print("\nNo trades found for the given wallet and date range.")
        return

    # Sort trades by timestamp (oldest first) for accurate P&L calculation
    sorted_trades = sorted(trades, key=lambda x: x["timestamp"])

    total = len(trades)
    buys = [t for t in trades if t["side"] == "BUY"]
    sells = [t for t in trades if t["side"] == "SELL"]
    total_volume = sum(t["usdc_value"] for t in trades)
    prices = [t["price"] for t in trades]

    # Price distribution buckets relevant to sweep detection
    above_99 = len([p for p in prices if p >= 0.99])
    above_95 = len([p for p in prices if p >= 0.95])
    below_95 = len([p for p in prices if p < 0.95])

    # P&L and Position Tracking
    positions = {}
    
    for t in sorted_trades:
        key = (t["event_slug"], t["asset"], t["outcome"])
        if key not in positions:
            positions[key] = {
                "quantity": 0.0,
                "avg_price": 0.0,
                "realized_pnl": 0.0,
                "buy_vol": 0.0,
                "sell_vol": 0.0,
                "buy_count": 0,
                "sell_count": 0,
                "last_timestamp": t["timestamp"]
            }
        
        pos = positions[key]
        price = t["price"]
        size = t["size"]
        value = t["usdc_value"]
        pos["last_timestamp"] = max(pos["last_timestamp"], t["timestamp"])

        if t["side"] == "BUY":
            current_cost = pos["quantity"] * pos["avg_price"]
            pos["quantity"] += size
            if pos["quantity"] > 0:
                pos["avg_price"] = (current_cost + value) / pos["quantity"]
            pos["buy_vol"] += value
            pos["buy_count"] += 1
        else:
            # Handle SELL trades
            # If we don't have current quantity (buy was before start_ts), 
            # we cant calculate exact PnL, but we can assume a reasonable entry or flag it
            if pos["quantity"] > 0:
                realized = size * (price - pos["avg_price"])
                pos["realized_pnl"] += realized
            else:
                # Fallback for "Sell before Buy" (outside range):
                # We record the sell vol but don't add to realized_pnl since basis is unknown
                pass
            
            pos["quantity"] -= size
            pos["sell_vol"] += value
            pos["sell_count"] += 1
            
            if pos["quantity"] <= 1e-9:
                pos["quantity"] = 0.0
                pos["avg_price"] = 0.0

    total_realized_pnl = sum(p["realized_pnl"] for p in positions.values())
    total_buy_vol = sum(p["buy_vol"] for p in positions.values())
    total_sell_vol = sum(p["sell_vol"] for p in positions.values())
    
    # Estimate settlement P&L for "Open" positions that are likely resolved
    # For a sweeper bot, almost all of these will settle at $1.00
    now_ts = int(time.time())
    total_estimated_settlement_pnl = 0.0
    settled_count = 0

    for key, pos in positions.items():
        if pos["quantity"] > 0.01:
            # Check market resolution (heuristic: if trade was > 1 hour ago, it's likely finished)
            if (now_ts - pos["last_timestamp"]) > 3600:
                # Assume $1.00 payout for winners (99% chance in sweep strategies)
                potential_pnl = pos["quantity"] * (1.0 - pos["avg_price"])
                total_estimated_settlement_pnl += potential_pnl
                settled_count += 1

    total_open_cost = sum(v["quantity"] * v["avg_price"] for v in positions.values() if v["quantity"] > 0.01)

    # Date range analysis
    timestamps = [t["timestamp"] for t in trades]
    first_trade = min(timestamps)
    last_trade = max(timestamps)
    est_tz = pytz_timezone("US/Eastern")
    first_dt = datetime.fromtimestamp(first_trade, tz=est_tz)
    last_dt = datetime.fromtimestamp(last_trade, tz=est_tz)

    print("\n" + "=" * 100)
    print(f"DETAILED TRADE CHRONOLOGY: {trades[0]['wallet']}")
    print("=" * 100)
    print(f"{'Time (EST)':<20} | {'Side':<5} | {'Size':>10} | {'Price':>8} | {'Value':>10} | {'Market/Asset'}")
    print("-" * 100)
    
    for t in sorted_trades:
        val = t["usdc_value"]
        side = t["side"]
        size = t["size"]
        price = t["price"]
        time_str = t["timestamp_est"]
        slug = t["event_slug"][:40]
        outcome = t["outcome"]
        print(f"{time_str:<20} | {side:<5} | {size:>10,.0f} | {price:>8.4f} | ${val:>9,.2f} | {slug} ({outcome})")

    print("\n" + "=" * 100)
    print("MARKET-BY-MARKET P&L BREAKDOWN")
    print("=" * 100)
    # Header: Market | Buys | Sells | Realized | Est. Settle | Total P&L
    print(f"{'Market (Asset/Outcome)':<50} | {'Buys':>8} | {'Sells':>8} | {'Realized':>10} | {'Settled*':>10}")
    print("-" * 100)

    for key, pos in sorted(positions.items(), key=lambda x: x[0][0]):
        slug, asset, outcome = key
        short_id = f"{slug[:35]} ({outcome})"
        realized = pos["realized_pnl"]
        
        # Est settle if holding > 0.01 and trade was > 1hr ago
        now_ts = int(time.time())
        est_settle = 0.0
        if pos["quantity"] > 0.01 and (now_ts - pos["last_timestamp"]) > 3600:
            est_settle = pos["quantity"] * (1.0 - pos["avg_price"])
            
        print(f"{short_id:<50} | ${pos['buy_vol']:>7.2f} | ${pos['sell_vol']:>7.2f} | {realized:>+10.4f} | {est_settle:>+10.4f}")

    # Financial Totals (CLOB Trades)
    total_realized_pnl = sum(p["realized_pnl"] for p in positions.values())
    total_buy_vol = sum(p["buy_vol"] for p in positions.values())
    total_sell_vol = sum(p["sell_vol"] for p in positions.values())
    
    # Financial Totals (Estimated)
    now_ts = int(time.time())
    total_est_settle = 0.0
    for pos in positions.values():
        if pos["quantity"] > 0.01 and (now_ts - pos["last_timestamp"]) > 3600:
            total_est_settle += (pos["quantity"] * (1.0 - pos["avg_price"]))

    total_open_cost = sum(v["quantity"] * v["avg_price"] for v in positions.values() if v["quantity"] > 0.01)

    unique_slugs = set(t["event_slug"] for t in trades if t["event_slug"])
    unique_assets = set(t["asset"] for t in trades if t["asset"])

    # Post-expiry analysis
    post_expiry_trades = [t for t in sorted_trades if t.get("is_post_expiry")]
    
    if post_expiry_trades:
        print("\n" + "=" * 100)
        print(f"TRADES EXECUTED AFTER MARKET EXPIRY (Likely Late Sweeps)")
        print("=" * 100)
        print(f"{'Time (EST)':<20} | {'Side':<5} | {'Size':>10} | {'Price':>8} | {'Value':>10} | {'Delay':>10} | {'Market'}")
        print("-" * 100)
        
        for t in post_expiry_trades:
            val = t["usdc_value"]
            side = t["side"]
            size = t["size"]
            price = t["price"]
            time_str = t["timestamp_est"]
            slug = t["event_slug"][:40]
            
            # Calculate delay
            delay_sec = t["timestamp"] - t["expiry_ts"]
            if delay_sec < 60:
                delay_str = f"{delay_sec}s"
            else:
                delay_str = f"{delay_sec // 60}m {delay_sec % 60}s"
                
            print(f"{time_str:<20} | {side:<5} | {size:>10,.0f} | {price:>8.4f} | ${val:>9,.2f} | {delay_str:>10} | {slug}")
        print("-" * 100)
        print(f"  Total post-expiry trades: {len(post_expiry_trades)}")
        print(f"  Post-expiry volume:      ${sum(t['usdc_value'] for t in post_expiry_trades):,.2f}")

    print("\n" + "=" * 100)
    print("FINANCIAL SUMMARY")
    print("=" * 100)
    print(f"  Wallet:           {trades[0]['wallet']}")
    print(f"  Range:            {first_dt:%Y-%m-%d %H:%M} — {last_dt:%Y-%m-%d %H:%M} EST")
    print(f"  Trades:           {total} ({len(buys)} Buys, {len(sells)} Sells)")
    print(f"  Markets:          {len(unique_slugs)}")
    print(f"  Unique Tokens:    {len(unique_assets)}")
    print()
    print(f"  CLOB Buy Volume:  ${float(total_buy_vol):,.2f}")
    print(f"  CLOB Sell Volume: ${float(total_sell_vol):,.2f}")
    print(f"  Realized P&L:     ${float(total_realized_pnl):+,.4f}")
    print()
    print(f"  Est Settle P&L:   ${total_est_settle:+,.4f} (@ $1.00 payout)")
    print(f"  Combined Gain:    ${(total_realized_pnl + total_est_settle):+,.4f}")
    print()
    print(f"  Open Position:    ${total_open_cost:,.2f} (cost basis of held/unsettled)")
    print(f"  Net Cash Flow:    ${(total_sell_vol - total_buy_vol):+,.2f}")
    print()
    print("  Price distribution:")
    print(f"    >= 0.99:        {above_99:>6d}  ({above_99 / total * 100:.1f}%)")
    print(f"    >= 0.95:        {above_95:>6d}  ({above_95 / total * 100:.1f}%)")
    print(f"    <  0.95:        {below_95:>6d}  ({below_95 / total * 100:.1f}%)")

    if above_95 / total > 0.5:
        print()
        print("  ⚠  >50% of trades at price >= 0.95 — consistent with endgame sweep pattern")
    
    print("-" * 100)
    print("  * Settled P&L assumes a $1.00 payout for winners. Only calculated for trades >1hr old.")
    print("=" * 100 + "\n")
