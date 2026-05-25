#!/usr/bin/env python3
"""Quick summary of a _positions.csv file."""
import sys
import glob
import pandas as pd

# Find the latest positions file
files = sorted(glob.glob("data/*_positions.csv"))
path = sys.argv[1] if len(sys.argv) > 1 else (files[-1] if files else None)
if not path:
    print("No positions CSV found")
    sys.exit(1)

print(f"File: {path}")
pos = pd.read_csv(path)
pos["pnl"] = pd.to_numeric(pos["pnl"], errors="coerce")
pos["buy_cost"] = pd.to_numeric(pos["buy_cost"], errors="coerce")
pos["sell_revenue"] = pd.to_numeric(pos["sell_revenue"], errors="coerce")

resolved = pos[pos["pnl"].notna()]
unresolved = pos[pos["pnl"].isna()]

print(f"\nTotal positions:    {len(pos)}")
print(f"Resolved w/ P&L:    {len(resolved)}")
print(f"  Winners:          {(resolved['winner']==True).sum()}")
print(f"  Losers:           {(resolved['winner']==False).sum()}")
print(f"  Win rate:         {(resolved['pnl']>0).mean()*100:.1f}%")
print(f"  Total P&L:       ${resolved['pnl'].sum():+,.2f}")
print(f"  Total buy cost:  ${resolved['buy_cost'].sum():,.2f}")
print(f"Unresolved (open):  {len(unresolved)}")

if len(resolved) > 0:
    print(f"\nP&L by price bucket:")
    resolved = resolved.copy()
    resolved["bucket"] = pd.cut(resolved["buy_cost"] / resolved["buy_cost"].replace(0, float("nan")),
        bins=[0, 0.1, 0.3, 0.5, 0.7, 1.0], include_lowest=True)
    # Instead, bucket by avg buy price
    resolved["avg_buy"] = resolved["buy_cost"] / (resolved["buy_cost"] + resolved["net_cost"])
    resolved["price_bucket"] = pd.cut(
        resolved["buy_cost"] / resolved[["buy_cost","sell_revenue"]].sum(axis=1).replace(0, float("nan")),
        bins=[0, 0.15, 0.30, 0.50, 0.70, 1.0],
        labels=["<15c", "15-30c", "30-50c", "50-70c", "70c+"]
    )
    print(resolved.groupby("price_bucket", observed=True).agg(
        count=("pnl", "count"),
        total_pnl=("pnl", "sum"),
        win_rate=("pnl", lambda x: f"{(x>0).mean()*100:.0f}%")
    ).to_string())
