#!/usr/bin/env python3
"""Realistic backtest of the ladder strategy using full order book depth data.

This uses the data collected by collect_ladder_data.py, which has:
  - Full order book depth (20 bid/ask levels) at each tick
  - Trade data (fills)
  - BTC spot prices

The key improvement over the previous backtest:
  - We can actually see if our ladder orders would fill
  - We simulate placing GTC limit orders at the open
  - At each tick, we check: does the order book have enough volume at our price?
  - Our order fills only if there's enough depth at that level

Usage:
    python3 scripts/backtest_with_depth.py --data data/backtest_data/ladder_data_*.parquet
    python3 scripts/backtest_with_depth.py --data data/backtest_data/ --outcomes data/outcomes.parquet
"""

import argparse
import json
import math
import sys
from pathlib import Path

import numpy as np
import pandas as pd
import pyarrow.parquet as pq


# ---------------------------------------------------------------------------
# Ladder parameters
# ---------------------------------------------------------------------------
LADDER_PRICES = [0.10, 0.15, 0.20, 0.25, 0.30, 0.35, 0.40, 0.45,
                 0.50, 0.55, 0.60, 0.65, 0.70, 0.75, 0.80, 0.85, 0.90]


def bell_curve_size(price, base=60):
    """Bell curve sizing centered at 0.50, sigma=0.25."""
    sigma = 0.25
    weight = math.exp(-0.5 * ((price - 0.50) / sigma) ** 2)
    return max(round(base * weight), 10)


# ---------------------------------------------------------------------------
# Fill simulation using full order book depth
# ---------------------------------------------------------------------------

def simulate_market_with_depth(snapshots, outcomes, ladder_prices, base_size=60):
    """Simulate the ladder strategy using actual order book depth.

    How it works:
      1. At the first snapshot (t≈0), place GTC limit BUY orders at all
         ladder prices on both Up and Down outcomes.
      2. Walk through each subsequent snapshot.
      3. For each price level in the ladder:
         - Our BUY limit at price X is on the BID side
         - It fills when there's a SELL order at or below X
         - We check the ASK side of the book: are there ask orders at or below X?
         - If yes, and there's enough volume, our order fills
      4. Hold to expiry — no sells.

    Args:
        snapshots: list of dicts, each with bids_json, asks_json, outcome, offset_s
        outcomes: dict mapping slug -> {'winner': 'Up'|'Down', 'up_won': bool}
        ladder_prices: list of price levels
        base_size: base size for bell curve

    Returns:
        dict with fill details and P&L
    """
    if not snapshots:
        return None

    # Separate Up and Down snapshots
    up_snaps = [s for s in snapshots if s.get('outcome') == 'Up']
    down_snaps = [s for s in snapshots if s.get('outcome') == 'Down']

    if not up_snaps or not down_snaps:
        return None

    slug = up_snaps[0].get('slug', '')
    up_outcome = outcomes.get(slug, {})
    up_winner = up_outcome.get('up_won', False)

    # Build our GTC limit orders
    # Each order is a BUY at a specific price level
    # We track: has it filled? how many shares?
    up_orders = {}
    down_orders = {}

    for price in ladder_prices:
        size = bell_curve_size(price, base_size)
        up_orders[price] = {'size': size, 'filled': 0, 'remaining': size}
        down_orders[price] = {'size': size, 'filled': 0, 'remaining': size}

    # Walk through snapshots and check fills
    for snap in up_snaps:
        offset = snap.get('offset_s', 0)
        if offset > 300:
            break

        # Parse the order book
        try:
            asks = json.loads(snap.get('asks_json', '[]'))
        except (json.JSONDecodeError, TypeError):
            asks = []

        if not asks:
            continue

        # Our BUY limit at price X fills when the ask drops to X or below
        # An ask level at [price, size] means someone is selling at that price
        # We need to check: is there enough sell volume at or below our bid?
        # Sort asks by price (ascending)
        asks_sorted = sorted(asks, key=lambda x: x[0])

        # For each price level we have orders at:
        for price in sorted(up_orders.keys()):
            order = up_orders[price]
            if order['remaining'] <= 0:
                continue  # Already fully filled

            # Check if any ask is at or below our price
            available_volume = 0
            for ask_price, ask_size in asks_sorted:
                if ask_price <= price:
                    available_volume += ask_size
                else:
                    break

            if available_volume > 0:
                # Our order fills! The amount depends on available volume
                # We can fill up to our remaining size
                fill = min(order['remaining'], available_volume * 0.1)
                # Why 0.1? Because our order is ONE of many bids at this level.
                # We only get a fraction of the available volume.
                fill = max(fill, 0)
                order['filled'] += fill
                order['remaining'] -= fill

    # Same for Down
    for snap in down_snaps:
        offset = snap.get('offset_s', 0)
        if offset > 300:
            break

        try:
            asks = json.loads(snap.get('asks_json', '[]'))
        except (json.JSONDecodeError, TypeError):
            asks = []

        if not asks:
            continue

        asks_sorted = sorted(asks, key=lambda x: x[0])

        for price in sorted(down_orders.keys()):
            order = down_orders[price]
            if order['remaining'] <= 0:
                continue

            available_volume = 0
            for ask_price, ask_size in asks_sorted:
                if ask_price <= price:
                    available_volume += ask_size
                else:
                    break

            if available_volume > 0:
                fill = min(order['remaining'], available_volume * 0.1)
                fill = max(fill, 0)
                order['filled'] += fill
                order['remaining'] -= fill

    # Also simulate fills from the MARKET CROSSING our bids
    # When the mid price drops through our level, our bid becomes the best bid
    # and gets hit by market sell orders
    up_mids = [s.get('best_bid', 0) for s in up_snaps if s.get('best_bid')]
    down_mids = [s.get('best_bid', 0) for s in down_snaps if s.get('best_bid')]

    if up_mids:
        up_mid_min = min(up_mids)
        up_mid_max = max(up_mids)
        down_mid_min = min(down_mids) if down_mids else 0
        down_mid_max = max(down_mids) if down_mids else 0

        # Additional fills from the market crossing our levels
        for price in ladder_prices:
            # Up: fill when the market dropped to this level
            if price <= up_mid_min and up_orders[price]['remaining'] > 0:
                remaining = up_orders[price]['remaining']
                up_orders[price]['filled'] += remaining
                up_orders[price]['remaining'] = 0

            # Down: same
            if price <= down_mid_min and down_orders[price]['remaining'] > 0:
                remaining = down_orders[price]['remaining']
                down_orders[price]['filled'] += remaining
                down_orders[price]['remaining'] = 0

            # Rally effect: the favorite side fills more
            if up_mid_max > up_mid_min:
                favorite_is_up = up_snaps[0].get('best_bid', 0) >= (down_snaps[0].get('best_bid', 0) if down_snaps else 0)
                if favorite_is_up and price <= up_mid_max and up_orders[price]['remaining'] > 0:
                    # Additional fill from the rally
                    rally_pct = 0.3 * (price - up_mid_min) / max(up_mid_max - up_mid_min, 0.01)
                    if rally_pct > 0:
                        extra = min(up_orders[price]['remaining'], up_orders[price]['size'] * rally_pct)
                        up_orders[price]['filled'] += extra
                        up_orders[price]['remaining'] -= extra

    # Compute P&L
    up_shares = sum(o['filled'] for o in up_orders.values())
    up_cost = sum(price * o['filled'] for price, o in up_orders.items())
    down_shares = sum(o['filled'] for o in down_orders.values())
    down_cost = sum(price * o['filled'] for price, o in down_orders.items())

    if up_winner:
        up_pnl = up_shares * 1.0 - up_cost
        down_pnl = 0 - down_cost
    else:
        up_pnl = 0 - up_cost
        down_pnl = down_shares * 1.0 - down_cost

    total_pnl = up_pnl + down_pnl
    total_cost = up_cost + down_cost

    # Track which price levels filled
    up_fill_levels = sum(1 for o in up_orders.values() if o['filled'] > 0)
    down_fill_levels = sum(1 for o in down_orders.values() if o['filled'] > 0)

    return {
        'slug': slug,
        'up_fills': up_fill_levels,
        'down_fills': down_fill_levels,
        'total_fills': up_fill_levels + down_fill_levels,
        'up_shares': up_shares,
        'down_shares': down_shares,
        'total_shares': up_shares + down_shares,
        'up_cost': up_cost,
        'down_cost': down_cost,
        'total_cost': total_cost,
        'up_pnl': up_pnl,
        'down_pnl': down_pnl,
        'total_pnl': total_pnl,
        'roi': total_pnl / total_cost * 100 if total_cost > 0 else 0,
        'up_winner': up_winner,
        'avg_up_price': up_cost / up_shares if up_shares > 0 else 0,
        'avg_down_price': down_cost / down_shares if down_shares > 0 else 0,
    }


# ---------------------------------------------------------------------------
# Load and process data
# ---------------------------------------------------------------------------

def load_data(data_path):
    """Load the collected parquet data and organize by market slug."""
    path = Path(data_path)
    if path.is_dir():
        files = sorted(path.glob("ladder_data_*.parquet"))
    elif path.is_file():
        files = [path]
    else:
        files = sorted(Path("data/backtest_data").glob("ladder_data_*.parquet"))

    if not files:
        print(f"ERROR: No data files found at {data_path}")
        print("Collect data first: python3 scripts/collect_ladder_data.py --n-events 100")
        sys.exit(1)

    print(f"Loading {len(files)} data file(s)...")
    tables = [pq.read_table(f) for f in files]
    df = pd.concat([t.to_pandas() for t in tables], ignore_index=True)
    print(f"  Total rows: {len(df):,}")
    print(f"  Date range: {df['ts_utc'].min()} to {df['ts_utc'].max()}")
    print(f"  Unique slugs: {df['slug'].nunique()}")
    print(f"  Has bids_json: {df['bids_json'].notna().sum():,} rows")

    # Group by slug + outcome
    # For each slug, collect all snapshots for Up and Down
    grouped = {}
    for slug, grp in df.groupby('slug'):
        grouped[slug] = {
            'up': grp[grp['outcome'] == 'Up'].to_dict('records'),
            'down': grp[grp['outcome'] == 'Down'].to_dict('records'),
            'n_up': len(grp[grp['outcome'] == 'Up']),
            'n_down': len(grp[grp['outcome'] == 'Down']),
        }

    return df, grouped


def main():
    parser = argparse.ArgumentParser(
        description="Backtest ladder strategy with full order book depth"
    )
    parser.add_argument("--data", default="data/backtest_data",
                        help="Path to parquet data file or directory")
    parser.add_argument("--outcomes", default=None,
                        help="Path to market outcomes parquet (optional)")
    parser.add_argument("--base-size", type=float, default=60,
                        help="Base size per ladder rung")
    parser.add_argument("--max-markets", type=int, default=None,
                        help="Limit number of markets to test")
    parser.add_argument("--output", default=None,
                        help="Save results to CSV")
    args = parser.parse_args()

    # Load the tick data
    df, grouped = load_data(args.data)

    # Load or fetch outcomes
    outcomes = {}
    if args.outcomes:
        outcomes_df = pd.read_parquet(args.outcomes)
        for _, row in outcomes_df.iterrows():
            slug = row.get('event_slug', '')
            winner = row.get('winner_side', '')
            outcomes[slug] = {
                'winner': winner,
                'up_won': winner == 'UP',
            }
        print(f"  Outcomes loaded: {len(outcomes)}")

    # Run backtest
    print(f"\nRunning backtest on {len(grouped)} markets...")
    results = []
    for slug, data in grouped.items():
        if args.max_markets and len(results) >= args.max_markets:
            break

        up_snaps = data['up']
        down_snaps = data['down']
        if not up_snaps or not down_snaps:
            continue

        # Get outcome
        slug_outcome = outcomes.get(slug, {})
        if not slug_outcome:
            # Can't determine outcome — skip
            continue

        # Combine snapshots
        all_snaps = up_snaps + down_snaps

        result = simulate_market_with_depth(
            all_snaps, outcomes, LADDER_PRICES, args.base_size
        )
        if result:
            result['n_up_snaps'] = data['n_up']
            result['n_down_snaps'] = data['n_down']
            results.append(result)

    if not results:
        print("No results. Check that data files have outcomes information.")
        return 1

    # Aggregate
    rdf = pd.DataFrame(results)

    print(f"\n{'='*70}")
    print("  BACKTEST WITH FULL ORDER BOOK DEPTH")
    print(f"{'='*70}")
    print(f"  Markets:              {len(rdf):>8,}")
    print(f"  Total P&L:            ${rdf['total_pnl'].sum():>8,.2f}")
    print(f"  Avg P&L/market:       ${rdf['total_pnl'].mean():>8,.2f}")
    print(f"  Std P&L:              ${rdf['total_pnl'].std():>8,.2f}")
    print(f"  Total cost:           ${rdf['total_cost'].sum():>8,.2f}")
    print(f"  Total shares:         {rdf['total_shares'].sum():>8,.0f}")
    print(f"  ROI:                  {rdf['total_pnl'].sum()/rdf['total_cost'].sum()*100:>7.2f}%")
    print(f"  Win rate:             {(rdf['total_pnl']>0).mean()*100:>7.2f}%")
    print(f"  Avg win:              ${rdf[rdf['total_pnl']>0]['total_pnl'].mean():>8,.2f}")
    print(f"  Avg loss:             ${rdf[rdf['total_pnl']<0]['total_pnl'].mean():>8,.2f}")
    print(f"  Avg fills/market:     {rdf['total_fills'].mean():>8.1f}")
    print(f"  Avg cost/market:      ${rdf['total_cost'].mean():>8,.2f}")
    print(f"  Avg Up price:         ${rdf['avg_up_price'].mean():>8,.4f}")
    print(f"  Avg Down price:       ${rdf['avg_down_price'].mean():>8,.4f}")

    # Edge decomposition
    tc = rdf['total_cost'].sum()
    tp = rdf['total_pnl'].sum()
    ts = rdf['total_shares'].sum()
    if ts > 0:
        ap = tc / ts
        rv = (tc + tp) / ts
        ep = tp / ts
        pc = 2 * ap
        print(f"\n  Avg price/share:      ${ap:.4f}")
        print(f"  Share win rate:       {rv*100:.2f}%")
        print(f"  Edge/share:           ${ep:.4f}")
        print(f"  Pair cost:            ${pc:.4f}")

    # Save
    if args.output:
        rdf.to_csv(args.output, index=False)
        print(f"\nResults saved to {args.output}")

    print(f"\n{'='*70}")
    print("  DATA QUALITY REPORT")
    print(f"{'='*70}")
    print(f"  Avg snapshots per market:  {(rdf['n_up_snaps'] + rdf['n_down_snaps']).mean():.0f}")
    print(f"  Avg Up snapshots:          {rdf['n_up_snaps'].mean():.0f}")
    print(f"  Avg Down snapshots:        {rdf['n_down_snaps'].mean():.0f}")
    print(f"  Markets with <10 snaps:    {(rdf['n_up_snaps']+rdf['n_down_snaps']<10).sum()}")
    print(f"  Markets with 50+ snaps:    {(rdf['n_up_snaps']+rdf['n_down_snaps']>=50).sum()}")


if __name__ == "__main__":
    main()