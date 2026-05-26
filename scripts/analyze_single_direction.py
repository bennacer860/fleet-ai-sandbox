#!/usr/bin/env python3
"""
Analyze single-direction markets: markets where only Up OR only Down was bought.
Focus on how often traders buy multiple times at different prices in the same direction.
"""
import csv
import sys
from collections import defaultdict
from typing import Any


def analyze_single_direction_markets(trades_csv: str) -> dict[str, Any]:
    """Analyze markets where only one direction was bought."""
    
    # Group trades by condition_id (market)
    markets: dict[str, list[dict]] = defaultdict(list)
    
    with open(trades_csv, "r") as f:
        reader = csv.DictReader(f)
        for row in reader:
            if row.get("side") == "BUY":
                cid = row["condition_id"]
                markets[cid].append({
                    "outcome": row["outcome"],
                    "price": float(row["price"]),
                    "shares": float(row["size"]),
                    "cost": float(row["price"]) * float(row["size"]),
                    "timestamp": row["timestamp"],
                    "event_slug": row.get("event_slug", ""),
                })
    
    # Categorize markets by direction bought
    single_direction = []  # Markets with only Up OR only Down
    dual_direction = []    # Markets with both Up AND Down
    
    for cid, trades in markets.items():
        outcomes = set(t["outcome"] for t in trades)
        if len(outcomes) == 1:
            single_direction.append((cid, trades))
        else:
            dual_direction.append((cid, trades))
    
    # Analyze single-direction markets
    single_dir_stats = {
        "total_markets": len(single_direction),
        "by_num_buys": defaultdict(int),  # How many markets had 1, 2, 3... buys
        "by_price_spread": defaultdict(int),  # Price spread categories
        "examples": [],  # Examples of multi-buy markets
        "total_cost": 0,
        "avg_buys_per_market": 0,
        "markets_with_multiple_buys": 0,
        "up_only_count": 0,
        "down_only_count": 0,
    }
    
    multi_buy_details = []
    
    for cid, trades in single_direction:
        num_buys = len(trades)
        single_dir_stats["by_num_buys"][num_buys] += 1
        
        outcome = trades[0]["outcome"]
        if outcome.lower() in ["up", "yes"]:
            single_dir_stats["up_only_count"] += 1
        else:
            single_dir_stats["down_only_count"] += 1
        
        total_cost = sum(t["cost"] for t in trades)
        single_dir_stats["total_cost"] += total_cost
        
        if num_buys > 1:
            single_dir_stats["markets_with_multiple_buys"] += 1
            prices = [t["price"] for t in trades]
            min_p, max_p = min(prices), max(prices)
            spread = max_p - min_p
            
            # Categorize by spread
            if spread < 0.05:
                single_dir_stats["by_price_spread"]["< 5c spread"] += 1
            elif spread < 0.10:
                single_dir_stats["by_price_spread"]["5-10c spread"] += 1
            elif spread < 0.20:
                single_dir_stats["by_price_spread"]["10-20c spread"] += 1
            elif spread < 0.30:
                single_dir_stats["by_price_spread"]["20-30c spread"] += 1
            else:
                single_dir_stats["by_price_spread"]["30c+ spread"] += 1
            
            multi_buy_details.append({
                "cid": cid,
                "outcome": outcome,
                "num_buys": num_buys,
                "prices": sorted(prices),
                "spread": spread,
                "total_shares": sum(t["shares"] for t in trades),
                "total_cost": total_cost,
                "event_slug": trades[0]["event_slug"],
            })
    
    # Sort multi-buy by number of buys and spread
    multi_buy_details.sort(key=lambda x: (-x["num_buys"], -x["spread"]))
    single_dir_stats["examples"] = multi_buy_details[:20]  # Top 20 examples
    
    if single_direction:
        total_buys = sum(len(trades) for _, trades in single_direction)
        single_dir_stats["avg_buys_per_market"] = total_buys / len(single_direction)
    
    # Summary of dual-direction markets for comparison
    dual_dir_stats = {
        "total_markets": len(dual_direction),
        "total_cost": sum(sum(t["cost"] for t in trades) for _, trades in dual_direction),
    }
    
    return {
        "single_direction": single_dir_stats,
        "dual_direction": dual_dir_stats,
        "total_markets": len(markets),
    }


def print_report(stats: dict, label: str):
    """Print formatted report."""
    print(f"\n{'='*70}")
    print(f"SINGLE-DIRECTION MARKET ANALYSIS: {label}")
    print(f"{'='*70}")
    
    sd = stats["single_direction"]
    dd = stats["dual_direction"]
    
    print(f"\n## Market Direction Summary")
    print(f"  Total markets traded: {stats['total_markets']:,}")
    print(f"  Single-direction markets: {sd['total_markets']:,} ({100*sd['total_markets']/stats['total_markets']:.1f}%)")
    print(f"  Dual-direction markets: {dd['total_markets']:,} ({100*dd['total_markets']/stats['total_markets']:.1f}%)")
    
    print(f"\n## Single-Direction Breakdown")
    print(f"  Up-only markets: {sd['up_only_count']:,}")
    print(f"  Down-only markets: {sd['down_only_count']:,}")
    print(f"  Total capital in single-dir: ${sd['total_cost']:,.0f}")
    
    print(f"\n## Multi-Buy Frequency (within single-direction markets)")
    print(f"  Markets with 1 buy: {sd['by_num_buys'].get(1, 0):,}")
    print(f"  Markets with 2 buys: {sd['by_num_buys'].get(2, 0):,}")
    print(f"  Markets with 3 buys: {sd['by_num_buys'].get(3, 0):,}")
    print(f"  Markets with 4 buys: {sd['by_num_buys'].get(4, 0):,}")
    print(f"  Markets with 5+ buys: {sum(v for k,v in sd['by_num_buys'].items() if k >= 5):,}")
    print(f"  Avg buys per market: {sd['avg_buys_per_market']:.2f}")
    print(f"  Markets with multiple buys: {sd['markets_with_multiple_buys']:,} ({100*sd['markets_with_multiple_buys']/sd['total_markets']:.1f}%)")
    
    print(f"\n## Price Spread in Multi-Buy Markets")
    for spread_cat, count in sorted(sd["by_price_spread"].items()):
        pct = 100 * count / sd["markets_with_multiple_buys"] if sd["markets_with_multiple_buys"] > 0 else 0
        print(f"  {spread_cat}: {count:,} ({pct:.1f}%)")
    
    print(f"\n## Top Examples of Multi-Buy Single-Direction Markets")
    print(f"  {'Buys':<6} {'Outcome':<6} {'Prices':<40} {'Spread':<8} {'Cost':<10}")
    print(f"  {'-'*6} {'-'*6} {'-'*40} {'-'*8} {'-'*10}")
    for ex in sd["examples"][:15]:
        prices_str = ", ".join(f"${p:.2f}" for p in ex["prices"][:6])
        if len(ex["prices"]) > 6:
            prices_str += f" +{len(ex['prices'])-6} more"
        print(f"  {ex['num_buys']:<6} {ex['outcome']:<6} {prices_str:<40} ${ex['spread']:.2f}    ${ex['total_cost']:.0f}")


if __name__ == "__main__":
    if len(sys.argv) < 2:
        print("Usage: python analyze_single_direction.py <trades.csv> [label]")
        sys.exit(1)
    
    csv_path = sys.argv[1]
    label = sys.argv[2] if len(sys.argv) > 2 else csv_path
    
    stats = analyze_single_direction_markets(csv_path)
    print_report(stats, label)
