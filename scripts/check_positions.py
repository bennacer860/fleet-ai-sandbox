#!/usr/bin/env python3
"""Quick summary of a _positions.csv file. Stdlib only — no pandas required."""
import csv
import glob
import sys
from collections import defaultdict

files = sorted(glob.glob("data/*_positions.csv"))
path = sys.argv[1] if len(sys.argv) > 1 else (files[-1] if files else None)
if not path:
    print("No positions CSV found")
    sys.exit(1)

print(f"File: {path}")

rows = []
with open(path) as f:
    for row in csv.DictReader(f):
        rows.append(row)

total = len(rows)
resolved = [r for r in rows if r.get("pnl") not in ("", None)]
unresolved = [r for r in rows if r.get("pnl") in ("", None)]

pnl_values = [float(r["pnl"]) for r in resolved]
total_pnl = sum(pnl_values)
winners = [r for r in resolved if r.get("winner") == "True"]
losers  = [r for r in resolved if r.get("winner") == "False"]
win_rate = len(winners) / len(resolved) * 100 if resolved else 0

buy_costs = [float(r["buy_cost"]) for r in resolved if r.get("buy_cost")]
total_buy_cost = sum(buy_costs)

print(f"\nTotal positions:    {total:>6,}")
print(f"Resolved w/ P&L:    {len(resolved):>6,}")
print(f"  Winners:          {len(winners):>6,}")
print(f"  Losers:           {len(losers):>6,}")
print(f"  Win rate:             {win_rate:>5.1f}%")
print(f"  Total P&L:       ${total_pnl:>+12,.2f}")
print(f"  Total buy cost:  ${total_buy_cost:>12,.2f}")
print(f"Unresolved (open):  {len(unresolved):>6,}")

# P&L by price bucket
if resolved:
    buckets: dict[str, list[float]] = defaultdict(list)
    for r in resolved:
        cost = float(r.get("buy_cost") or 0)
        shares = float(r.get("buy_shares") or 1)
        avg_price = cost / shares if shares > 0 else 0
        if avg_price < 0.15:   bucket = "<15c"
        elif avg_price < 0.30: bucket = "15-30c"
        elif avg_price < 0.50: bucket = "30-50c"
        elif avg_price < 0.70: bucket = "50-70c"
        else:                  bucket = "70c+"
        buckets[bucket].append(float(r["pnl"]))

    print(f"\n{'Bucket':<10} {'Count':>6} {'Total P&L':>12} {'Win rate':>10}")
    print("-" * 42)
    for b in ["<15c", "15-30c", "30-50c", "50-70c", "70c+"]:
        vals = buckets.get(b, [])
        if not vals:
            continue
        wr = sum(1 for v in vals if v > 0) / len(vals) * 100
        print(f"{b:<10} {len(vals):>6} ${sum(vals):>+11,.2f} {wr:>9.0f}%")
