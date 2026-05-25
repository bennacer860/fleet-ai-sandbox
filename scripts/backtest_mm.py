#!/usr/bin/env python3
"""Backtest the dual-sided market-making strategy on collected order book data.

Implements the competitor strategy from docs/research/competitor_market_making_analysis.md:
  1. Post resting BUY limits on both Up and Down at prices summing < $1.00
  2. Get filled passively when takers sell into our bids
  3. Rebalance when one side accumulates too much inventory
  4. At expiration, paired shares pay $1.00 guaranteed; orphans are directional bets

Usage:
    python3 scripts/backtest_mm.py --sweep
    python3 scripts/backtest_mm.py --max-combined 0.96 --fill-rate 0.10
"""

from __future__ import annotations

import argparse
import gzip
import json
import sys
from collections import defaultdict
from dataclasses import dataclass
from pathlib import Path
from typing import Any


@dataclass
class Config:
    max_combined: float = 0.97
    max_position_per_side: float = 300.0
    max_imbalance_ratio: float = 2.0
    rebalance_ratio: float = 3.0
    rebalance_sell_frac: float = 0.3
    min_spread_cents: int = 2
    fill_rate: float = 0.10


@dataclass
class EventResult:
    slug: str
    market: str
    winner: str | None = None
    up_bought: float = 0.0
    up_cost: float = 0.0
    dn_bought: float = 0.0
    dn_cost: float = 0.0
    up_sold: float = 0.0
    up_proceeds: float = 0.0
    dn_sold: float = 0.0
    dn_proceeds: float = 0.0
    fills_up: int = 0
    fills_dn: int = 0

    @property
    def net_up(self) -> float:
        return self.up_bought - self.up_sold

    @property
    def net_dn(self) -> float:
        return self.dn_bought - self.dn_sold

    @property
    def net_up_cost(self) -> float:
        return self.up_cost - self.up_proceeds

    @property
    def net_dn_cost(self) -> float:
        return self.dn_cost - self.dn_proceeds

    @property
    def paired(self) -> float:
        return max(min(self.net_up, self.net_dn), 0)

    @property
    def total_cost(self) -> float:
        return self.net_up_cost + self.net_dn_cost

    def pnl(self) -> float:
        if self.winner is None:
            return 0.0
        nu = max(self.net_up, 0)
        nd = max(self.net_dn, 0)
        cost = self.net_up_cost + self.net_dn_cost
        if self.winner == "Up":
            return nu * 1.0 - cost
        return nd * 1.0 - cost

    def combined_price(self) -> float | None:
        nu = self.net_up
        nd = self.net_dn
        if nu <= 0 or nd <= 0:
            return None
        return (self.net_up_cost / nu) + (self.net_dn_cost / nd)

    @property
    def was_traded(self) -> bool:
        return self.net_up > 0 or self.net_dn > 0


# ── Data loading ──────────────────────────────────────────────────────────

@dataclass
class EventData:
    slug: str
    market: str
    winner: str | None
    ticks: list[dict[str, dict]]
    trades_by_seq: dict[int, list[dict]]


def load_all(paths: list[Path]) -> list[EventData]:
    raw_samples: dict[str, list[dict]] = defaultdict(list)
    raw_trades: dict[str, list[dict]] = defaultdict(list)
    meta: dict[str, dict] = {}

    for path in paths:
        try:
            with gzip.open(path, "rt") as f:
                for line in f:
                    rec = json.loads(line)
                    rtype = rec["type"]
                    if rtype == "meta":
                        for w in rec.get("windows", []):
                            for market, slug in w["slugs"].items():
                                meta[slug] = {"market": market}
                    elif rtype == "sample" and rec.get("data_ready"):
                        raw_samples[rec["slug"]].append(rec)
                    elif rtype == "trade":
                        raw_trades[rec["slug"]].append(rec)
        except EOFError:
            print(f"  Warning: {path.name} truncated, using partial data")

    events: list[EventData] = []
    for slug in sorted(raw_samples):
        m = meta.get(slug, {})
        market = m.get("market", slug.split("-")[0].upper())

        samples = raw_samples[slug]
        by_seq: dict[int, dict[str, dict]] = defaultdict(dict)
        for s in samples:
            by_seq[s["sample_seq"]][s["outcome"]] = s
        ticks = [by_seq[k] for k in sorted(by_seq)]

        last_by_outcome: dict[str, dict] = {}
        for s in samples:
            last_by_outcome[s["outcome"]] = s
        winner = None
        for out, s in last_by_outcome.items():
            if (s.get("best_bid") or 0) > 0.9:
                winner = out

        trades_by_seq: dict[int, list[dict]] = defaultdict(list)
        for t in raw_trades.get(slug, []):
            trades_by_seq[t["sample_seq"]].append(t)

        events.append(EventData(
            slug=slug, market=market, winner=winner,
            ticks=ticks, trades_by_seq=dict(trades_by_seq),
        ))

    return events


# ── Simulation ────────────────────────────────────────────────────────────

def simulate_event(ev: EventData, cfg: Config) -> EventResult:
    pos = EventResult(slug=ev.slug, market=ev.market, winner=ev.winner)

    for tick in ev.ticks:
        up = tick.get("Up")
        dn = tick.get("Down")
        if not up or not dn:
            continue

        up_bid = up.get("best_bid")
        up_ask = up.get("best_ask")
        dn_bid = dn.get("best_bid")
        dn_ask = dn.get("best_ask")
        if not all(v and v > 0 for v in (up_bid, up_ask, dn_bid, dn_ask)):
            continue

        if (up_ask - up_bid) < cfg.min_spread_cents / 100:
            continue
        if (dn_ask - dn_bid) < cfg.min_spread_cents / 100:
            continue

        # Determine our bid prices: post at best bid, scale down if combined too high
        our_up = up_bid
        our_dn = dn_bid
        combined = our_up + our_dn

        if combined >= cfg.max_combined:
            scale = (cfg.max_combined - 0.005) / combined
            our_up = round(up_bid * scale, 2)
            our_dn = round(dn_bid * scale, 2)
            if our_up + our_dn >= cfg.max_combined:
                continue

        seq = up["sample_seq"]

        # Balance gate: don't accumulate on one side if it's already
        # too far ahead of the other. Forces both legs to fill roughly
        # together, preventing large orphan positions.
        nu = pos.net_up
        nd = pos.net_dn
        up_ok = (nu <= 1.0) or (nd > 0 and nu / nd <= cfg.max_imbalance_ratio)
        dn_ok = (nd <= 1.0) or (nu > 0 and nd / nu <= cfg.max_imbalance_ratio)

        for trade in ev.trades_by_seq.get(seq, []):
            if trade.get("side") != "SELL":
                continue
            outcome = trade.get("outcome")
            price = trade.get("price", 0)
            size = trade.get("size", 0)

            fill_shares = size * cfg.fill_rate

            if outcome == "Up" and price <= our_up and up_ok:
                room = cfg.max_position_per_side - pos.net_up
                f = min(fill_shares, max(room, 0))
                if f > 0:
                    pos.up_bought += f
                    pos.up_cost += f * our_up
                    pos.fills_up += 1
            elif outcome == "Down" and price <= our_dn and dn_ok:
                room = cfg.max_position_per_side - pos.net_dn
                f = min(fill_shares, max(room, 0))
                if f > 0:
                    pos.dn_bought += f
                    pos.dn_cost += f * our_dn
                    pos.fills_dn += 1

        # Rebalance: sell excess on the heavy side, never sell more than we hold
        nu = pos.net_up
        nd = pos.net_dn
        if nu > 0 and nd > 0:
            big, small = max(nu, nd), min(nu, nd)
            if small > 0 and big / small > cfg.rebalance_ratio:
                excess = big - small
                sell_qty = excess * cfg.rebalance_sell_frac
                if nu > nd:
                    sell_qty = min(sell_qty, pos.net_up * 0.5)
                    if sell_qty > 0:
                        pos.up_sold += sell_qty
                        pos.up_proceeds += sell_qty * up_bid
                else:
                    sell_qty = min(sell_qty, pos.net_dn * 0.5)
                    if sell_qty > 0:
                        pos.dn_sold += sell_qty
                        pos.dn_proceeds += sell_qty * dn_bid

    return pos


def run_backtest(events: list[EventData], cfg: Config) -> list[EventResult]:
    return [simulate_event(ev, cfg) for ev in events]


# ── Reporting ─────────────────────────────────────────────────────────────

def summarize(results: list[EventResult], cfg: Config, verbose: bool = True) -> float:
    total_pnl = 0.0
    total_cost = 0.0
    traded = 0
    wins = 0
    losses = 0
    by_market: dict[str, list[float]] = defaultdict(list)
    paired_pnl = 0.0
    orphan_pnl = 0.0

    if verbose:
        print(f"\n{'Slug':<40} {'Mkt':<4} {'NetUp':>7} {'NetDn':>7} {'Pair':>6}"
              f" {'Comb':>7} {'Win':>5} {'PnL':>9}")
        print("-" * 92)

    for r in results:
        if not r.was_traded:
            continue
        traded += 1
        pnl = r.pnl()
        total_pnl += pnl
        total_cost += max(r.total_cost, 0)
        by_market[r.market].append(pnl)
        if pnl > 0:
            wins += 1
        elif pnl < 0:
            losses += 1

        # Decompose paired vs orphan PnL
        p = r.paired
        c = r.combined_price()
        if p > 0 and c is not None:
            paired_pnl += p * (1.0 - c)

        if verbose:
            cs = f"{c:.4f}" if c else "  N/A"
            w = r.winner or "?"
            print(f"{r.slug:<40} {r.market:<4} {r.net_up:>7.1f} {r.net_dn:>7.1f}"
                  f" {r.paired:>6.1f} {cs:>7} {w:>5} {pnl:>+9.2f}")

    if verbose:
        print("-" * 92)
    print(f"\n{'SUMMARY':=^92}")
    print(f"  Events traded:     {traded}")
    print(f"  Profitable:        {wins}")
    print(f"  Losing:            {losses}")
    print(f"  Win rate:          {wins / max(traded, 1) * 100:.1f}%")
    print(f"  Total PnL:         ${total_pnl:+.2f}")
    print(f"  Paired PnL:        ${paired_pnl:+.2f}")
    print(f"  Orphan PnL:        ${total_pnl - paired_pnl:+.2f}")
    print(f"  Total capital:     ${total_cost:.2f}")
    if total_cost > 0:
        print(f"  Return on capital: {total_pnl / total_cost * 100:+.3f}%")

    for mkt in sorted(by_market):
        pnls = by_market[mkt]
        s = sum(pnls)
        w = sum(1 for p in pnls if p > 0)
        print(f"    {mkt}: {len(pnls)} events, {w} wins, PnL=${s:+.2f},"
              f" avg=${s / len(pnls):+.2f}/event")
    print("=" * 92)
    return total_pnl


# ── Main ──────────────────────────────────────────────────────────────────

def main() -> int:
    parser = argparse.ArgumentParser(description="Backtest dual-sided market-making")
    parser.add_argument("--data-dir", default="data/collectors")
    parser.add_argument("--max-combined", type=float, default=0.96)
    parser.add_argument("--max-position", type=float, default=300.0)
    parser.add_argument("--max-imbalance", type=float, default=2.0,
                        help="Max ratio of heavy/light side before pausing fills on heavy side")
    parser.add_argument("--fill-rate", type=float, default=0.10)
    parser.add_argument("--rebalance-ratio", type=float, default=3.0)
    parser.add_argument("--min-spread", type=int, default=2)
    parser.add_argument("--sweep", action="store_true")
    args = parser.parse_args()

    data_dir = Path(args.data_dir)
    data_files = sorted(data_dir.glob("*.jsonl.gz"))
    if not data_files:
        print(f"No data files in {data_dir}")
        return 1

    print(f"Loading {len(data_files)} files from {data_dir}...")
    events = load_all(data_files)
    print(f"Loaded {len(events)} events ({len(set(e.market for e in events))} markets)")
    known = sum(1 for e in events if e.winner)
    print(f"  {known} events with known outcomes")

    if args.sweep:
        print(f"\n{'PARAMETER SWEEP':=^92}")
        best_pnl = float("-inf")
        best_cfg: dict[str, Any] = {}

        for mc in [0.93, 0.94, 0.95, 0.96, 0.97, 0.98]:
            for fr in [0.03, 0.05, 0.08, 0.10, 0.15]:
                for rb in [2.0, 3.0, 5.0]:
                    for mi in [1.5, 2.0, 3.0, 999.0]:
                        cfg = Config(max_combined=mc, fill_rate=fr,
                                     rebalance_ratio=rb, max_imbalance_ratio=mi,
                                     min_spread_cents=args.min_spread,
                                     max_position_per_side=args.max_position)
                        results = run_backtest(events, cfg)
                        pnl = sum(r.pnl() for r in results if r.was_traded)
                        n = sum(1 for r in results if r.was_traded)
                        w = sum(1 for r in results if r.pnl() > 0)
                        tag = ""
                        if pnl > best_pnl:
                            best_pnl = pnl
                            best_cfg = dict(max_combined=mc, fill_rate=fr,
                                            rebalance_ratio=rb, max_imbalance_ratio=mi,
                                            min_spread_cents=args.min_spread)
                            tag = " <<<"
                        if tag:
                            imb = "off" if mi > 100 else f"{mi:.1f}"
                            print(f"  mc={mc:.2f} fr={fr:.2f} rb={rb:.0f} imb={imb}"
                                  f"  → {n} evts, WR={w / max(n, 1) * 100:.0f}%,"
                                  f" PnL=${pnl:+.2f}{tag}")

        print(f"\nBest: {best_cfg}  →  PnL=${best_pnl:+.2f}")
        print("\nFull report with best params:\n")
        cfg = Config(**best_cfg, max_position_per_side=args.max_position)
    else:
        cfg = Config(
            max_combined=args.max_combined, fill_rate=args.fill_rate,
            rebalance_ratio=args.rebalance_ratio, min_spread_cents=args.min_spread,
            max_imbalance_ratio=args.max_imbalance,
            max_position_per_side=args.max_position,
        )

    print("=" * 92)
    print("MARKET-MAKING BACKTEST")
    imb = "off" if cfg.max_imbalance_ratio > 100 else f"{cfg.max_imbalance_ratio:.1f}"
    print(f"  max_combined={cfg.max_combined}  fill_rate={cfg.fill_rate}"
          f"  rebalance={cfg.rebalance_ratio}  imbalance_cap={imb}"
          f"  min_spread={cfg.min_spread_cents}c  max_pos={cfg.max_position_per_side}")
    print("=" * 92)

    results = run_backtest(events, cfg)
    summarize(results, cfg)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
