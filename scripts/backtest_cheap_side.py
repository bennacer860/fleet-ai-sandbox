#!/usr/bin/env python3
"""Backtest portfolio-hedged cheap-side buying on collector order-book data.

Replays the @ivy56 / @certova approach from docs/research/cheap_side_strategy.md:
  1. Buy the underdog (cheaper ask) when price is below fair value ($0.50)
  2. One direction per market — never hedge both sides
  3. Optional early exit when bid >= sell threshold
  4. Aggregate Up/Down balance gate across the portfolio

Usage:
    python3 scripts/backtest_cheap_side.py \\
        --data data/collectors/btc_eth_15m_20260525T011348Z_N50.jsonl.gz

    python3 scripts/backtest_cheap_side.py --data-dir data/collectors --sweep
"""

from __future__ import annotations

import argparse
import gzip
import json
import sys
from collections import defaultdict
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


@dataclass
class Config:
    max_entry_price: float = 0.50
    min_entry_price: float = 0.01
    shares_per_buy: float = 50.0
    max_shares_per_market: float = 300.0
    single_buy: bool = True
    min_tte_s: float = 0.0
    max_tte_s: float = 900.0
    sell_threshold: float = 0.0  # 0 = hold to settlement
    max_balance_ratio: float = 999.0  # skip if portfolio Up/Down ratio exceeds this
    fee_bps: float = 0.0


@dataclass
class Position:
    slug: str
    market: str
    outcome: str
    shares: float = 0.0
    cost: float = 0.0
    sold_shares: float = 0.0
    sold_proceeds: float = 0.0
    winner: str | None = None
    fills: int = 0
    entry_prices: list[float] = field(default_factory=list)

    @property
    def net_shares(self) -> float:
        return self.shares - self.sold_shares

    @property
    def net_cost(self) -> float:
        return self.cost - self.sold_proceeds

    @property
    def avg_entry(self) -> float:
        return self.cost / self.shares if self.shares else 0.0

    @property
    def was_traded(self) -> bool:
        return self.shares > 0

    def pnl(self) -> float:
        if not self.was_traded:
            return 0.0
        fee = self.cost * (0.0)  # fees applied in summary
        if self.winner is None:
            return 0.0
        payout = self.net_shares * (1.0 if self.outcome == self.winner else 0.0)
        return payout + self.sold_proceeds - self.cost - fee


@dataclass
class EventData:
    slug: str
    market: str
    window_ts: int
    duration_s: int
    winner: str | None
    ticks: list[dict[str, dict]]


def _parse_ts(ts_utc: str) -> float:
    return datetime.fromisoformat(ts_utc.replace("Z", "+00:00")).timestamp()


def load_events(paths: list[Path]) -> tuple[list[EventData], dict[str, Any]]:
    raw_samples: dict[str, list[dict]] = defaultdict(list)
    meta: dict[str, Any] = {}

    for path in paths:
        try:
            with gzip.open(path, "rt") as fh:
                for line in fh:
                    rec = json.loads(line)
                    if rec["type"] == "meta":
                        meta = rec
                    elif rec["type"] == "sample" and rec.get("data_ready"):
                        raw_samples[rec["slug"]].append(rec)
        except EOFError:
            print(f"  Warning: {path.name} truncated, using partial data")

    duration_s = int(meta.get("duration_minutes", 5)) * 60

    events: list[EventData] = []
    for slug in sorted(raw_samples):
        samples = raw_samples[slug]
        market = samples[0].get("market", slug.split("-")[0].upper())
        window_ts = int(samples[0].get("window_timestamp", 0))

        by_seq: dict[int, dict[str, dict]] = defaultdict(dict)
        for s in samples:
            outcome = s.get("outcome")
            if outcome:
                by_seq[s["sample_seq"]][outcome] = s
        ticks = [by_seq[k] for k in sorted(by_seq)]

        last_by_outcome: dict[str, dict] = {}
        for s in samples:
            if s.get("outcome"):
                last_by_outcome[s["outcome"]] = s
        winner = None
        for out, s in last_by_outcome.items():
            if (s.get("best_bid") or 0) > 0.9:
                winner = out

        events.append(
            EventData(
                slug=slug,
                market=market,
                window_ts=window_ts,
                duration_s=duration_s,
                winner=winner,
                ticks=ticks,
            )
        )

    return events, meta


def _portfolio_balance(positions: list[Position]) -> tuple[float, float]:
    up_cost = sum(p.net_cost for p in positions if p.outcome == "Up" and p.was_traded)
    dn_cost = sum(p.net_cost for p in positions if p.outcome == "Down" and p.was_traded)
    return up_cost, dn_cost


def _balance_ok(outcome: str, up_cost: float, dn_cost: float, max_ratio: float) -> bool:
    if max_ratio >= 999:
        return True
    if outcome == "Up":
        new_up = up_cost + 1
        if dn_cost <= 0:
            return True
        return new_up / dn_cost <= max_ratio and dn_cost / new_up >= 1 / max_ratio
    new_dn = dn_cost + 1
    if up_cost <= 0:
        return True
    return new_dn / up_cost <= max_ratio and up_cost / new_dn >= 1 / max_ratio


def simulate_event(
    ev: EventData,
    cfg: Config,
    portfolio_up: float,
    portfolio_dn: float,
) -> Position | None:
    pos: Position | None = None

    for tick in ev.ticks:
        up = tick.get("Up")
        dn = tick.get("Down")
        if not up or not dn:
            continue

        up_ask = up.get("best_ask")
        dn_ask = dn.get("best_ask")
        if not up_ask or not dn_ask or up_ask <= 0 or dn_ask <= 0:
            continue

        sample_ts = _parse_ts(up["ts_utc"])
        expiry_ts = ev.window_ts + ev.duration_s
        tte = expiry_ts - sample_ts
        if tte < cfg.min_tte_s or tte > cfg.max_tte_s:
            continue

        # Early exit
        if pos and pos.was_traded and cfg.sell_threshold > 0:
            held = tick.get(pos.outcome)
            if held:
                bid = held.get("best_bid") or 0
                if bid >= cfg.sell_threshold and pos.net_shares > 0:
                    sell_qty = pos.net_shares
                    pos.sold_shares += sell_qty
                    pos.sold_proceeds += sell_qty * bid
            continue

        if pos and cfg.single_buy and pos.fills >= 1:
            continue

        # Pick the cheap side (underdog)
        if up_ask <= dn_ask:
            cheap_out, cheap_ask, cheap_side = "Up", up_ask, up
        else:
            cheap_out, cheap_ask, cheap_side = "Down", dn_ask, dn

        if cheap_ask > cfg.max_entry_price or cheap_ask < cfg.min_entry_price:
            continue

        if pos and pos.outcome != cheap_out:
            continue  # single direction only

        if not _balance_ok(cheap_out, portfolio_up, portfolio_dn, cfg.max_balance_ratio):
            continue

        room = cfg.max_shares_per_market - (pos.net_shares if pos else 0)
        if room <= 0:
            continue

        ask_depth = cheap_side.get("best_ask_size") or cfg.shares_per_buy
        fill = min(cfg.shares_per_buy, room, ask_depth)
        if fill <= 0:
            continue

        if pos is None:
            pos = Position(
                slug=ev.slug,
                market=ev.market,
                outcome=cheap_out,
                winner=ev.winner,
            )

        pos.shares += fill
        pos.cost += fill * cheap_ask
        pos.fills += 1
        pos.entry_prices.append(cheap_ask)

        if cheap_out == "Up":
            portfolio_up += fill * cheap_ask
        else:
            portfolio_dn += fill * cheap_ask

    if pos:
        pos.winner = ev.winner
    return pos


def run_backtest(events: list[EventData], cfg: Config) -> list[Position]:
    results: list[Position] = []
    portfolio_up = portfolio_dn = 0.0

    for ev in events:
        pos = simulate_event(ev, cfg, portfolio_up, portfolio_dn)
        if pos and pos.was_traded:
            results.append(pos)
            if pos.outcome == "Up":
                portfolio_up += pos.net_cost
            else:
                portfolio_dn += pos.net_cost

    return results


def _price_bucket(avg: float) -> str:
    if avg < 0.15:
        return "<15c"
    if avg < 0.30:
        return "15-30c"
    if avg < 0.50:
        return "30-50c"
    if avg < 0.70:
        return "50-70c"
    return "70c+"


def summarize(results: list[Position], cfg: Config, meta: dict[str, Any], verbose: bool = True) -> float:
    traded = [r for r in results if r.was_traded and r.winner]
    total_pnl = 0.0
    total_cost = 0.0
    wins = 0
    by_market: dict[str, list[float]] = defaultdict(list)
    by_bucket: dict[str, dict[str, float]] = defaultdict(lambda: {"n": 0, "pnl": 0, "cost": 0, "wins": 0})

    if verbose:
        print(f"\n{'Slug':<42} {'Mkt':<4} {'Side':<5} {'Shrs':>6} {'Avg':>6} {'Win':>5} {'PnL':>9}")
        print("-" * 82)

    for r in traded:
        pnl = r.pnl()
        fee = r.cost * cfg.fee_bps / 10_000
        pnl -= fee
        total_pnl += pnl
        total_cost += r.net_cost
        if pnl > 0:
            wins += 1
        by_market[r.market].append(pnl)

        bucket = _price_bucket(r.avg_entry)
        by_bucket[bucket]["n"] += 1
        by_bucket[bucket]["pnl"] += pnl
        by_bucket[bucket]["cost"] += r.net_cost
        if pnl > 0:
            by_bucket[bucket]["wins"] += 1

        if verbose:
            slug_short = r.slug[-38:] if len(r.slug) > 38 else r.slug
            w = "Y" if r.outcome == r.winner else "N"
            print(
                f"{slug_short:<42} {r.market:<4} {r.outcome:<5} {r.net_shares:>6.0f}"
                f" ${r.avg_entry:>5.2f} {w:>5} ${pnl:>+8.2f}"
            )

    up_cost = sum(r.net_cost for r in traded if r.outcome == "Up")
    dn_cost = sum(r.net_cost for r in traded if r.outcome == "Down")

    if verbose:
        print("-" * 82)

    print(f"\n{'SUMMARY':=^82}")
    if meta:
        print(f"  Data: {meta.get('run_started_utc', '?')}  "
              f"{meta.get('duration_minutes', '?')}m windows  N={meta.get('n_events', '?')}")
    print(f"  max_entry={cfg.max_entry_price:.2f}  shares={cfg.shares_per_buy:.0f}  "
          f"single_buy={cfg.single_buy}  tte=[{cfg.min_tte_s:.0f},{cfg.max_tte_s:.0f}]s  "
          f"fee={cfg.fee_bps:.0f}bps")
    print(f"  Markets traded:    {len(traded)}")
    print(f"  Win rate:          {wins / max(len(traded), 1) * 100:.1f}%")
    print(f"  Total cost:        ${total_cost:.2f}")
    print(f"  Total P&L:         ${total_pnl:+.2f}")
    if total_cost:
        print(f"  ROI:               {total_pnl / total_cost * 100:+.2f}%")
    if up_cost + dn_cost > 0:
        print(f"  Up/Down balance:   ${up_cost:.0f} Up / ${dn_cost:.0f} Down "
              f"({100 * up_cost / (up_cost + dn_cost):.0f}/{100 * dn_cost / (up_cost + dn_cost):.0f})")

    print(f"\n  P&L by price bucket:")
    for bucket in ["<15c", "15-30c", "30-50c", "50-70c", "70c+"]:
        s = by_bucket[bucket]
        if not s["n"]:
            continue
        roi = 100 * s["pnl"] / s["cost"] if s["cost"] else 0
        wr = 100 * s["wins"] / s["n"]
        print(f"    {bucket:<8} {int(s['n']):>4} mkts  cost=${s['cost']:>8.2f}  "
              f"P&L=${s['pnl']:>+8.2f}  ROI={roi:>+6.1f}%  WR={wr:.0f}%")

    for mkt in sorted(by_market):
        pnls = by_market[mkt]
        print(f"    {mkt}: {len(pnls)} events, P&L=${sum(pnls):+.2f}")

    print("=" * 82)
    return total_pnl


def main() -> int:
    parser = argparse.ArgumentParser(description="Backtest portfolio-hedged cheap-side buying")
    parser.add_argument("--data", type=Path, help="Single collector .jsonl.gz file")
    parser.add_argument("--data-dir", type=Path, default=Path("data/collectors"))
    parser.add_argument("--max-price", type=float, default=0.50)
    parser.add_argument("--min-price", type=float, default=0.01)
    parser.add_argument("--shares", type=float, default=50.0)
    parser.add_argument("--max-shares", type=float, default=300.0)
    parser.add_argument("--multi-buy", action="store_true", help="Allow multiple buys per market")
    parser.add_argument("--min-tte", type=float, default=0.0, help="Min seconds to expiry")
    parser.add_argument("--max-tte", type=float, default=900.0, help="Max seconds to expiry")
    parser.add_argument("--sell-at", type=float, default=0.0, help="Sell when bid >= this (0=hold)")
    parser.add_argument("--max-balance", type=float, default=999.0)
    parser.add_argument("--fee-bps", type=float, default=0.0)
    parser.add_argument("--sweep", action="store_true")
    parser.add_argument("--quiet", action="store_true")
    args = parser.parse_args()

    if args.data:
        data_files = [args.data]
    else:
        data_files = sorted(args.data_dir.glob("*.jsonl.gz"))
    if not data_files:
        print("No collector data found.")
        return 1

    print(f"Loading {len(data_files)} file(s)...")
    events, meta = load_events(data_files)
    known = sum(1 for e in events if e.winner)
    print(f"Loaded {len(events)} events, {known} with known outcomes")

    if args.sweep:
        print(f"\n{'PARAMETER SWEEP':=^82}")
        best_pnl = float("-inf")
        best: dict[str, Any] = {}
        for max_p in [0.30, 0.40, 0.50]:
            for min_tte in [0, 120, 300]:
                for max_tte in [120, 300, 600, 900]:
                    if min_tte >= max_tte:
                        continue
                    cfg = Config(
                        max_entry_price=max_p,
                        shares_per_buy=args.shares,
                        max_shares_per_market=args.max_shares,
                        single_buy=not args.multi_buy,
                        min_tte_s=min_tte,
                        max_tte_s=max_tte,
                        fee_bps=args.fee_bps,
                    )
                    results = run_backtest(events, cfg)
                    pnl = sum(r.pnl() for r in results if r.winner)
                    n = sum(1 for r in results if r.was_traded and r.winner)
                    if pnl > best_pnl and n >= 5:
                        best_pnl = pnl
                        best = dict(max_price=max_p, min_tte=min_tte, max_tte=max_tte, n=n)
                        print(f"  max_p={max_p:.2f} tte=[{min_tte},{max_tte}]  "
                              f"→ {n} mkts P&L=${pnl:+.2f} <<<")
        print(f"\nBest sweep: {best}  P&L=${best_pnl:+.2f}")
        cfg = Config(
            max_entry_price=best.get("max_price", 0.50),
            min_tte_s=best.get("min_tte", 0),
            max_tte_s=best.get("max_tte", 900),
            shares_per_buy=args.shares,
            max_shares_per_market=args.max_shares,
            single_buy=not args.multi_buy,
            fee_bps=args.fee_bps,
        )
    else:
        cfg = Config(
            max_entry_price=args.max_price,
            min_entry_price=args.min_price,
            shares_per_buy=args.shares,
            max_shares_per_market=args.max_shares,
            single_buy=not args.multi_buy,
            min_tte_s=args.min_tte,
            max_tte_s=args.max_tte,
            sell_threshold=args.sell_at,
            max_balance_ratio=args.max_balance,
            fee_bps=args.fee_bps,
        )

    print("=" * 82)
    print("PORTFOLIO-HEDGED CHEAP-SIDE BUYING BACKTEST")
    print("=" * 82)
    results = run_backtest(events, cfg)
    summarize(results, cfg, meta, verbose=not args.quiet)
    return 0


if __name__ == "__main__":
    sys.exit(main())
