"""Gabagool pair-arbitrage performance report.

Usage: .venv/bin/python /tmp/q.py [--hours N] [--profile N]
Reads from data/bot_p{profile}.db (default profile=1, hours=24).
Filters to strategy='gabagool' and dry_run=0.
"""

import argparse
import sqlite3
import time
from collections import defaultdict

parser = argparse.ArgumentParser()
parser.add_argument("--hours", type=float, default=24)
parser.add_argument("--profile", type=int, default=1)
args = parser.parse_args()

db_path = f"data/bot_p{args.profile}.db"
cutoff = time.time() - args.hours * 3600

conn = sqlite3.connect(db_path)
cur = conn.cursor()

# ── Filled gabagool orders (real only) ────────────────────────────────
cur.execute("""
    SELECT slug, token_id, price, size, final_status, placed_at
    FROM orders
    WHERE strategy = 'gabagool'
      AND dry_run = 0
      AND final_status IN ('FILLED', 'PARTIAL')
      AND placed_at >= ?
    ORDER BY slug, placed_at
""", (cutoff,))
rows = cur.fetchall()

slug_data = defaultdict(lambda: defaultdict(list))
for slug, token_id, price, size, status, placed_at in rows:
    slug_data[slug][token_id].append({"price": price, "size": size})

orphans, complete = [], []
for slug, tokens in slug_data.items():
    (orphans if len(tokens) == 1 else complete).append((slug, tokens))

# ── Per-pair metrics ──────────────────────────────────────────────────
all_pair_costs = []
leg_a_prices, leg_b_prices = [], []
total_capital, total_profit = 0.0, 0.0

for slug, tokens in complete:
    legs = []
    for tid, fills in tokens.items():
        qty = sum(f["size"] for f in fills)
        cost = sum(f["price"] * f["size"] for f in fills)
        legs.append({"qty": qty, "avg": cost / qty if qty else 0, "cost": cost, "fills": len(fills)})
    legs.sort(key=lambda l: l["avg"])
    pc = legs[0]["avg"] + legs[1]["avg"]
    mq = min(legs[0]["qty"], legs[1]["qty"])
    lp = mq * (1.0 - pc)
    all_pair_costs.append(pc)
    leg_a_prices.append(legs[0]["avg"])
    leg_b_prices.append(legs[1]["avg"])
    total_capital += legs[0]["cost"] + legs[1]["cost"]
    total_profit += lp

orphan_capital = 0.0
orphan_prices = []
for slug, tokens in orphans:
    fills = list(tokens.values())[0]
    qty = sum(f["size"] for f in fills)
    cost = sum(f["price"] * f["size"] for f in fills)
    orphan_capital += cost
    orphan_prices.append(cost / qty if qty else 0)

# ── Print report ──────────────────────────────────────────────────────
W = 60
print("=" * W)
print(f"GABAGOOL PERFORMANCE — last {args.hours:.0f}h (profile {args.profile}, real trades)")
print("=" * W)

print(f"\nMarkets traded:   {len(slug_data)}")
print(f"Complete pairs:   {len(complete)}")
print(f"Orphan pairs:     {len(orphans)}")
print(f"Filled orders:    {len(rows)}")

if all_pair_costs:
    spc = sorted(all_pair_costs)
    profitable = sum(1 for pc in spc if pc < 1.0)
    print(f"\n{'='*W}")
    print("PAIR ECONOMICS")
    print(f"{'='*W}")
    print(f"  Win rate:          {profitable}/{len(spc)} ({profitable/len(spc)*100:.1f}%)")
    print(f"  Avg pair cost:     ${sum(spc)/len(spc):.4f}")
    print(f"  Median pair cost:  ${spc[len(spc)//2]:.4f}")
    print(f"  Best / Worst:      ${spc[0]:.4f} / ${spc[-1]:.4f}")
    print(f"  Avg profit/share:  ${1.0 - sum(spc)/len(spc):.4f}")
    print(f"  Avg cheap leg:     ${sum(leg_a_prices)/len(leg_a_prices):.4f}")
    print(f"  Avg dear leg:      ${sum(leg_b_prices)/len(leg_b_prices):.4f}")
    print(f"  Capital (pairs):   ${total_capital:.2f}")
    print(f"  Locked profit:     ${total_profit:.4f}")
    if total_capital > 0:
        print(f"  ROI (pairs):       {total_profit/total_capital*100:.2f}%")

    print(f"\n  PAIR COST DISTRIBUTION")
    for lo, hi in [(0.90,0.93),(0.93,0.95),(0.95,0.97),(0.97,0.98),(0.98,0.99),(0.99,1.00),(1.00,1.01),(1.01,1.05)]:
        n = sum(1 for pc in spc if lo <= pc < hi)
        if n:
            print(f"    ${lo:.2f}-${hi:.2f}: {n:3d} {'#'*n}")

print(f"\n{'='*W}")
print("ORPHAN ANALYSIS")
print(f"{'='*W}")
print(f"  Orphan pairs:   {len(orphans)}")
print(f"  Capital at risk: ${orphan_capital:.4f}")
if orphan_prices:
    print(f"  Avg orphan price: ${sum(orphan_prices)/len(orphan_prices):.4f}")
for slug, tokens in sorted(orphans):
    fills = list(tokens.values())[0]
    qty = sum(f["size"] for f in fills)
    cost = sum(f["price"] * f["size"] for f in fills)
    print(f"    {slug}: {qty:.2f} @ ${cost/qty:.4f} (${cost:.4f})")

# ── Order funnel ──────────────────────────────────────────────────────
print(f"\n{'='*W}")
print("ORDER FUNNEL")
print(f"{'='*W}")
cur.execute("""
    SELECT final_status, COUNT(*), COALESCE(SUM(size),0)
    FROM orders
    WHERE strategy = 'gabagool' AND dry_run = 0 AND placed_at >= ?
    GROUP BY final_status ORDER BY COUNT(*) DESC
""", (cutoff,))
for s, c, sz in cur.fetchall():
    print(f"  {s or 'NULL'}: {c} orders ({sz:.1f} shares)")

# ── Overall ───────────────────────────────────────────────────────────
print(f"\n{'='*W}")
print("OVERALL")
print(f"{'='*W}")
all_cap = total_capital + orphan_capital
print(f"  Total capital:   ${all_cap:.2f}")
print(f"  Locked profit:   ${total_profit:.4f}")
print(f"  Orphan risk:     ${orphan_capital:.4f}")
if all_cap > 0:
    print(f"  Overall ROI:     {total_profit/all_cap*100:.2f}%")
if args.hours > 0 and total_profit:
    print(f"  Profit/hour:     ${total_profit/args.hours:.4f}")

conn.close()
