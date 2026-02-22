#!/usr/bin/env python3
"""Resolve win/loss for wallet trades by querying the Polymarket Gamma API.

For each unique condition_id in the wallet trades CSV, fetches the market
from the Gamma API to determine which token won.  Then marks every position
as WIN / LOSS / UNRESOLVED and prints a summary with overall and per-duration
win rates.

Usage:
    python3 resolve_trades.py
    python3 resolve_trades.py --wallet wallet_trades.csv --output resolved_trades.csv
"""

import argparse
import csv
import sys
import time
from typing import Any, Optional

import requests

from src.utils.parsing import parse_json_list, parse_float_list
from src.utils.timestamps import ts_to_est

GAMMA_API = "https://gamma-api.polymarket.com"
RATE_LIMIT_DELAY = 0.3  # seconds between API calls to stay under rate-limits


# ── API helpers ──────────────────────────────────────────────────────────────

def fetch_market_by_condition(condition_id: str) -> Optional[dict[str, Any]]:
    """Fetch a market from the Gamma API by condition_id.

    Queries ``GET /markets?condition_id=<cid>``.

    Returns:
        Market dict or *None* if not found / API error.
    """
    url = f"{GAMMA_API}/markets"
    params = {"condition_id": condition_id}
    try:
        resp = requests.get(url, params=params, timeout=30)
        resp.raise_for_status()
        data = resp.json()
        # The endpoint may return a list; grab the first entry.
        if isinstance(data, list):
            return data[0] if data else None
        return data if data else None
    except Exception:
        return None


def get_winning_info(
    market: dict[str, Any],
) -> tuple[Optional[str], Optional[str], bool]:
    """Determine winning token and outcome from a market.

    Returns:
        ``(winning_token_id, winning_outcome, is_resolved)``
    """
    token_ids = parse_json_list(market.get("clobTokenIds"))
    outcomes = parse_json_list(market.get("outcomes"))
    prices = parse_float_list(market.get("outcomePrices"))

    if len(token_ids) != 2 or len(prices) != 2:
        return None, None, False

    # Resolved when one price ≥ 0.99 (winner) and the other ≤ 0.01
    for i, p in enumerate(prices):
        if p >= 0.99:
            winner_token = str(token_ids[i])
            winner_outcome = outcomes[i] if i < len(outcomes) else "?"
            return winner_token, winner_outcome, True

    return None, None, False


# ── Trade loading / aggregation ──────────────────────────────────────────────

def load_wallet_trades(path: str) -> list[dict]:
    """Load wallet trades, keeping only 5/15-min crypto up-or-down markets."""
    trades: list[dict] = []
    with open(path, newline="") as f:
        for row_num, row in enumerate(csv.DictReader(f), start=2):
            slug = row.get("event_slug", "")
            if "5min-up-or-down" in slug or "15min-up-or-down" in slug:
                row["_row_num"] = row_num
                trades.append(row)
    return trades


def aggregate_positions(trades: list[dict]) -> list[dict]:
    """Group individual fills into positions keyed by (slug, asset, outcome, side)."""
    key_map: dict[tuple, dict] = {}
    for t in trades:
        key = (t["event_slug"], t["asset"], t["outcome"], t["side"])
        if key not in key_map:
            key_map[key] = {
                "event_slug": t["event_slug"],
                "asset": t["asset"],
                "outcome": t["outcome"],
                "side": t["side"],
                "price": float(t["price"]),
                "total_size": 0.0,
                "total_usdc": 0.0,
                "num_fills": 0,
                "first_ts": int(t["timestamp"]),
                "last_ts": int(t["timestamp"]),
                "condition_id": t.get("condition_id", ""),
            }
        pos = key_map[key]
        pos["total_size"] += float(t["size"])
        pos["total_usdc"] += float(t["usdc_value"])
        pos["num_fills"] += 1
        ts = int(t["timestamp"])
        pos["first_ts"] = min(pos["first_ts"], ts)
        pos["last_ts"] = max(pos["last_ts"], ts)
    return sorted(key_map.values(), key=lambda p: p["first_ts"], reverse=True)


# ── Main ─────────────────────────────────────────────────────────────────────

def main() -> int:
    parser = argparse.ArgumentParser(
        description="Resolve win/loss for wallet trades via Polymarket Gamma API",
    )
    parser.add_argument("--wallet", default="wallet_trades.csv", help="Wallet trades CSV (default: wallet_trades.csv)")
    parser.add_argument("--output", default="resolved_trades.csv", help="Output CSV (default: resolved_trades.csv)")
    args = parser.parse_args()

    # ── 1. Load & aggregate ──────────────────────────────────────────────────
    print("Loading wallet trades …")
    trades = load_wallet_trades(args.wallet)
    print(f"  → {len(trades)} fills on 5/15-min markets")
    positions = aggregate_positions(trades)
    print(f"  → {len(positions)} aggregated positions\n")

    if not positions:
        print("No 5/15-min positions found. Nothing to resolve.")
        return 0

    # ── 2. Unique condition_ids ──────────────────────────────────────────────
    cond_ids = list({p["condition_id"] for p in positions if p["condition_id"]})
    print(f"Querying Gamma API for {len(cond_ids)} unique markets …\n")

    # ── 3. Fetch market resolution for each condition_id ─────────────────────
    resolutions: dict[str, dict] = {}
    for i, cid in enumerate(cond_ids, 1):
        short = cid[:10] + "…" if len(cid) > 12 else cid
        print(f"  [{i:>2}/{len(cond_ids)}] {short} ", end="", flush=True)

        market = fetch_market_by_condition(cid)
        if market is None:
            print("❓ not found")
            resolutions[cid] = {
                "winning_token": None,
                "winning_outcome": None,
                "resolved": False,
                "question": "?",
            }
        else:
            w_token, w_outcome, resolved = get_winning_info(market)
            question = market.get("question", market.get("groupItemTitle", "?"))
            resolutions[cid] = {
                "winning_token": w_token,
                "winning_outcome": w_outcome,
                "resolved": resolved,
                "question": question,
            }
            if resolved:
                print(f"→ {w_outcome}")
            else:
                print("⏳ not resolved")

        if i < len(cond_ids):
            time.sleep(RATE_LIMIT_DELAY)

    # ── 4. Classify each position ────────────────────────────────────────────
    wins = losses = unresolved_n = no_cond_n = 0
    csv_rows: list[dict] = []

    for pos in positions:
        cid = pos["condition_id"]
        if not cid:
            result = "NO_CONDITION_ID"
            no_cond_n += 1
        elif cid not in resolutions or not resolutions[cid]["resolved"]:
            result = "UNRESOLVED"
            unresolved_n += 1
        else:
            res = resolutions[cid]
            if str(pos["asset"]) == str(res["winning_token"]):
                result = "WIN"
                wins += 1
            else:
                result = "LOSS"
                losses += 1

        # Detect market duration from slug
        slug = pos["event_slug"]
        duration = "5min" if "5min" in slug else ("15min" if "15min" in slug else "?")

        res_info = resolutions.get(cid, {})
        csv_rows.append({
            "event_slug": pos["event_slug"],
            "duration": duration,
            "outcome": pos["outcome"],
            "side": pos["side"],
            "price": pos["price"],
            "total_size": round(pos["total_size"], 2),
            "total_usdc": round(pos["total_usdc"], 2),
            "num_fills": pos["num_fills"],
            "trade_time_est": ts_to_est(pos["first_ts"]),
            "result": result,
            "winning_outcome": res_info.get("winning_outcome", ""),
            "question": res_info.get("question", ""),
            "condition_id": cid,
            "token_id": pos["asset"],
            "winning_token": res_info.get("winning_token", ""),
        })

    # ── 5. Summary ───────────────────────────────────────────────────────────
    resolved_count = wins + losses
    total = len(positions)

    print(f"\n{'═' * 70}")
    print(f"RESULTS  ({total} positions)")
    print(f"{'─' * 70}")
    if resolved_count:
        print(f"  ✅ Wins:       {wins:>4}  ({wins / resolved_count * 100:.1f}%)")
        print(f"  ❌ Losses:     {losses:>4}  ({losses / resolved_count * 100:.1f}%)")
    else:
        print(f"  ✅ Wins:       {wins:>4}")
        print(f"  ❌ Losses:     {losses:>4}")
    if unresolved_n:
        print(f"  ⏳ Unresolved: {unresolved_n:>4}")
    if no_cond_n:
        print(f"  ❓ No cond_id: {no_cond_n:>4}")

    if resolved_count:
        print(f"\n  Overall win rate: {wins}/{resolved_count} = {wins / resolved_count * 100:.1f}%")

        # Per-duration breakdown
        for dur in ("5min", "15min"):
            dur_rows = [r for r in csv_rows if r["duration"] == dur and r["result"] in ("WIN", "LOSS")]
            if dur_rows:
                d_wins = sum(1 for r in dur_rows if r["result"] == "WIN")
                d_total = len(dur_rows)
                d_usdc_win = sum(r["total_usdc"] for r in dur_rows if r["result"] == "WIN")
                d_usdc_loss = sum(r["total_usdc"] for r in dur_rows if r["result"] == "LOSS")
                print(
                    f"  {dur:>5} win rate: {d_wins}/{d_total} = {d_wins / d_total * 100:.1f}%  "
                    f"(won ${d_usdc_win:.2f}  lost ${d_usdc_loss:.2f})"
                )

    # Recent positions table
    print(f"\n{'─' * 70}")
    print("Recent positions:")
    for r in csv_rows[:20]:
        icon = {"WIN": "✅", "LOSS": "❌"}.get(r["result"], "⏳")
        print(
            f"  {icon} {r['result']:<6}  {r['outcome']:<5}  {r['side']:<5}  "
            f"${r['total_usdc']:>8.2f}  {r['trade_time_est']}  {r['event_slug']}"
        )
    if len(csv_rows) > 20:
        print(f"  … and {len(csv_rows) - 20} more (see {args.output})")

    # ── 6. Write CSV ─────────────────────────────────────────────────────────
    if csv_rows:
        with open(args.output, "w", newline="") as f:
            writer = csv.DictWriter(f, fieldnames=list(csv_rows[0].keys()))
            writer.writeheader()
            writer.writerows(csv_rows)
        print(f"\nResults written to {args.output}")

    print()
    return 0


if __name__ == "__main__":
    sys.exit(main())
