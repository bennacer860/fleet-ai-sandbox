#!/usr/bin/env python3
"""Backtest 3-phase legged arbitrage on collector order-book data.

Replays the live dry-run strategy (src/strategy/legged_arb.py) against
historical BTC 15-min book snapshots. Assumes immediate fills at best
ask (buys) and best bid (sells), matching dry-run FillSimulator behaviour.

Usage:
    python3 scripts/backtest_legged_arb.py \\
        --data data/collectors/btc_15m_20260601T213013Z_N300.jsonl.gz

    python3 scripts/backtest_legged_arb.py --data-dir data/collectors --quiet
"""

from __future__ import annotations

import argparse
import gzip
import json
import sys
from collections import defaultdict
from dataclasses import dataclass, replace
from datetime import datetime, timezone
from pathlib import Path

# Allow importing strategy logic from repo root.
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from src.strategy.legged_arb import (  # noqa: E402
    ArbState,
    LeggedArbConfig,
    MarketArbState,
    build_market_book,
    count_active_arbs,
    should_buy_phase3,
    should_enter_phase1,
    should_sell_phase2,
    should_stop_loss,
)


@dataclass
class EventData:
    slug: str
    market: str
    window_ts: int
    duration_s: int
    winner: str | None
    ticks: list[dict[str, dict]]


@dataclass
class MarketResult:
    slug: str
    winner: str | None
    phase: ArbState
    phase1_side: str | None
    phase1_entry: float
    phase1_cost: float
    phase1_shares: float
    phase2_revenue: float
    phase2_sold: float
    phase3_cost: float
    phase3_shares: float
    phase3_side: str | None
    pnl: float = 0.0
    had_phase1: bool = False
    had_phase2: bool = False
    had_phase3: bool = False
    had_stop_loss: bool = False
    skip_reason: str = ""


@dataclass(frozen=True)
class BankrollParams:
    initial: float = 1000.0
    position_fraction: float = 0.08
    max_deploy_fraction: float = 0.70
    ref_entry_price: float = 0.75


@dataclass
class BankrollSummary:
    initial: float
    final: float
    peak: float
    trough: float
    max_drawdown: float
    skipped_insufficient_cash: int
    clip_size: float
    max_concurrent: int


def sizing_for_bankroll(params: BankrollParams) -> tuple[float, int]:
    """Derive clip size and max concurrent arbs from starting bankroll."""
    notional = params.initial * params.position_fraction
    clip = max(
        5.0,
        min(150.0, notional / params.ref_entry_price),
    )
    # Budget ~15% extra per slot for phase-3 cheap legs.
    slot_budget = notional * 1.15
    deployable = params.initial * params.max_deploy_fraction
    max_concurrent = max(1, min(10, int(deployable / slot_budget)))
    return clip, max_concurrent


def _parse_ts(ts_utc: str) -> float:
    return datetime.fromisoformat(ts_utc.replace("Z", "+00:00")).timestamp()


def load_events(paths: list[Path]) -> tuple[list[EventData], dict]:
    raw_samples: dict[str, list[dict]] = defaultdict(list)
    meta: dict = {}

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

    duration_s = int(meta.get("duration_minutes", 15)) * 60
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


def _tick_book(tick: dict[str, dict]) -> tuple[object, float]:
    up = tick.get("Up") or {}
    dn = tick.get("Down") or {}
    book = build_market_book(
        up_bid=float(up.get("best_bid") or 0),
        up_ask=float(up.get("best_ask") or 0),
        up_ask_size=float(up.get("best_ask_size") or 0),
        up_bid_depth=float(up.get("bid_depth") or 0),
        up_ask_depth=float(up.get("ask_depth") or 0),
        down_bid=float(dn.get("best_bid") or 0),
        down_ask=float(dn.get("best_ask") or 0),
        down_ask_size=float(dn.get("best_ask_size") or 0),
        down_bid_depth=float(dn.get("bid_depth") or 0),
        down_ask_depth=float(dn.get("ask_depth") or 0),
    )
    sample_ts = _parse_ts(up.get("ts_utc") or dn.get("ts_utc", "1970-01-01T00:00:00+00:00"))
    return book, sample_ts


def _settle(
    state: MarketArbState,
    winner: str | None,
    *,
    phase1_cost: float,
    phase1_shares: float,
    phase2_revenue: float,
    phase3_cost: float,
    phase3_shares: float,
    phase3_side: str | None,
) -> float:
    if winner is None:
        return 0.0
    payout = 0.0
    if phase1_shares > 0 and state.phase1_side:
        payout += phase1_shares * (1.0 if state.phase1_side == winner else 0.0)
    if phase3_shares > 0 and phase3_side:
        payout += phase3_shares * (1.0 if phase3_side == winner else 0.0)
    return payout + phase2_revenue - phase1_cost - phase3_cost


def simulate_event(
    ev: EventData,
    cfg: LeggedArbConfig,
    *,
    active_arb_count: int = 0,
) -> MarketResult:
    state = MarketArbState(slug=ev.slug, yes_token_id="up", no_token_id="down")
    phase1_cost = phase1_shares = 0.0
    phase2_revenue = phase2_sold = 0.0
    phase3_cost = phase3_shares = 0.0
    phase3_side: str | None = None
    had_stop_loss = False
    had_profit_sell = False
    last_skip = ""

    expiry_ts = ev.window_ts + ev.duration_s

    for tick in ev.ticks:
        book, sample_ts = _tick_book(tick)
        tte_s = expiry_ts - sample_ts
        if tte_s < 0:
            continue

        if state.phase == ArbState.IDLE:
            decision = should_enter_phase1(
                book, tte_s, cfg, active_arb_count=active_arb_count
            )
            if not decision.enter:
                last_skip = decision.reason
                continue
            fill_size = decision.size
            fill_price = decision.price
            state.phase = ArbState.PHASE1_FILLED
            state.phase1_side = decision.side
            state.phase1_entry_price = fill_price
            state.phase1_target_size = fill_size
            state.phase1_filled_size = fill_size
            state.phase3_target_size = fill_size
            phase1_cost += fill_size * fill_price
            phase1_shares = fill_size
            continue

        if state.phase == ArbState.PHASE1_FILLED:
            profit_decision = should_sell_phase2(state, book, tte_s, cfg)
            if profit_decision.sell:
                decision = profit_decision
                next_phase = ArbState.PHASE2_SOLD
                had_profit_sell = True
            else:
                stop_decision = should_stop_loss(state, book, tte_s, cfg)
                if not stop_decision.sell:
                    continue
                decision = stop_decision
                next_phase = ArbState.DONE
                had_stop_loss = True
            sell_size = decision.size
            sell_price = decision.price
            state.phase = next_phase
            state.phase2_sold_size = sell_size
            phase2_sold = sell_size
            phase2_revenue += sell_size * sell_price
            phase1_shares = max(phase1_shares - sell_size, 0.0)
            continue

        if state.phase == ArbState.PHASE2_SOLD:
            decision = should_buy_phase3(state, book, tte_s, cfg)
            if not decision.buy:
                continue
            fill_size = decision.size
            fill_price = decision.price
            state.phase = ArbState.PHASE3_FILLED
            state.phase3_filled_size = fill_size
            phase3_side = decision.side
            phase3_cost = fill_size * fill_price
            phase3_shares = fill_size
            continue

    pnl = _settle(
        state,
        ev.winner,
        phase1_cost=phase1_cost,
        phase1_shares=phase1_shares,
        phase2_revenue=phase2_revenue,
        phase3_cost=phase3_cost,
        phase3_shares=phase3_shares,
        phase3_side=phase3_side,
    )

    return MarketResult(
        slug=ev.slug,
        winner=ev.winner,
        phase=state.phase,
        phase1_side=state.phase1_side,
        phase1_entry=state.phase1_entry_price,
        phase1_cost=phase1_cost,
        phase1_shares=phase1_shares,
        phase2_revenue=phase2_revenue,
        phase2_sold=phase2_sold,
        phase3_cost=phase3_cost,
        phase3_shares=phase3_shares,
        phase3_side=phase3_side,
        pnl=pnl,
        had_phase1=phase1_cost > 0,
        had_phase2=had_profit_sell,
        had_phase3=phase3_cost > 0,
        had_stop_loss=had_stop_loss,
        skip_reason=last_skip,
    )


def run_backtest(events: list[EventData], cfg: LeggedArbConfig) -> list[MarketResult]:
    """Per-market backtest (no cross-market concurrency gate)."""
    return [simulate_event(ev, cfg) for ev in events]


def run_backtest_concurrent(events: list[EventData], cfg: LeggedArbConfig) -> list[MarketResult]:
    """Chronological replay with max_concurrent gate across overlapping markets."""
    # Build global timeline keyed by slug
    timeline: list[tuple[float, str, dict[str, dict]]] = []
    meta_by_slug: dict[str, EventData] = {ev.slug: ev for ev in events}
    for ev in events:
        expiry_ts = ev.window_ts + ev.duration_s
        for tick in ev.ticks:
            up = tick.get("Up") or {}
            dn = tick.get("Down") or {}
            ts = _parse_ts(up.get("ts_utc") or dn.get("ts_utc", "1970-01-01T00:00:00+00:00"))
            if expiry_ts - ts < 0:
                continue
            timeline.append((ts, ev.slug, tick))
    timeline.sort(key=lambda x: x[0])

    states: dict[str, MarketArbState] = {}
    accounting: dict[str, dict] = {}
    results: dict[str, MarketResult] = {}

    def _acct(slug: str) -> dict:
        if slug not in accounting:
            accounting[slug] = {
                "phase1_cost": 0.0,
                "phase1_shares": 0.0,
                "phase2_revenue": 0.0,
                "phase2_sold": 0.0,
                "phase3_cost": 0.0,
                "phase3_shares": 0.0,
                "phase3_side": None,
                "last_skip": "",
                "had_stop_loss": False,
                "had_profit_sell": False,
            }
        return accounting[slug]

    for ts, slug, tick in timeline:
        ev = meta_by_slug[slug]
        expiry_ts = ev.window_ts + ev.duration_s
        book, sample_ts = _tick_book(tick)
        tte_s = expiry_ts - sample_ts

        if slug not in states:
            states[slug] = MarketArbState(slug=slug, yes_token_id="up", no_token_id="down")
        state = states[slug]
        ac = _acct(slug)

        if tte_s <= 0 and state.phase not in (ArbState.IDLE, ArbState.DONE):
            state.phase = ArbState.DONE
            continue
        if state.phase == ArbState.DONE:
            continue

        active = count_active_arbs(states)

        if state.phase == ArbState.IDLE:
            decision = should_enter_phase1(book, tte_s, cfg, active_arb_count=active)
            if not decision.enter:
                ac["last_skip"] = decision.reason
                continue
            state.phase = ArbState.PHASE1_FILLED
            state.phase1_side = decision.side
            state.phase1_entry_price = decision.price
            state.phase1_filled_size = decision.size
            state.phase3_target_size = decision.size
            ac["phase1_cost"] += decision.size * decision.price
            ac["phase1_shares"] = decision.size
            continue

        if state.phase == ArbState.PHASE1_FILLED:
            profit_decision = should_sell_phase2(state, book, tte_s, cfg)
            if profit_decision.sell:
                decision = profit_decision
                state.phase = ArbState.PHASE2_SOLD
                ac["had_profit_sell"] = True
            else:
                stop_decision = should_stop_loss(state, book, tte_s, cfg)
                if not stop_decision.sell:
                    continue
                decision = stop_decision
                state.phase = ArbState.DONE
                ac["had_stop_loss"] = True
            state.phase2_sold_size = decision.size
            ac["phase2_sold"] = decision.size
            ac["phase2_revenue"] = decision.size * decision.price
            ac["phase1_shares"] = max(ac["phase1_shares"] - decision.size, 0.0)
            continue

        if state.phase == ArbState.PHASE2_SOLD:
            decision = should_buy_phase3(state, book, tte_s, cfg)
            if not decision.buy:
                continue
            state.phase = ArbState.PHASE3_FILLED
            state.phase3_filled_size = decision.size
            ac["phase3_side"] = decision.side
            ac["phase3_cost"] = decision.size * decision.price
            ac["phase3_shares"] = decision.size

    for ev in events:
        state = states.get(ev.slug) or MarketArbState(
            slug=ev.slug, yes_token_id="up", no_token_id="down"
        )
        ac = _acct(ev.slug)
        pnl = _settle(
            state,
            ev.winner,
            phase1_cost=ac["phase1_cost"],
            phase1_shares=ac["phase1_shares"],
            phase2_revenue=ac["phase2_revenue"],
            phase3_cost=ac["phase3_cost"],
            phase3_shares=ac["phase3_shares"],
            phase3_side=ac["phase3_side"],
        )
        results[ev.slug] = MarketResult(
            slug=ev.slug,
            winner=ev.winner,
            phase=state.phase,
            phase1_side=state.phase1_side,
            phase1_entry=state.phase1_entry_price,
            phase1_cost=ac["phase1_cost"],
            phase1_shares=ac["phase1_shares"],
            phase2_revenue=ac["phase2_revenue"],
            phase2_sold=ac["phase2_sold"],
            phase3_cost=ac["phase3_cost"],
            phase3_shares=ac["phase3_shares"],
            phase3_side=ac["phase3_side"],
            pnl=pnl,
            had_phase1=ac["phase1_cost"] > 0,
            had_phase2=ac.get("had_profit_sell", False),
            had_phase3=ac["phase3_cost"] > 0,
            had_stop_loss=ac.get("had_stop_loss", False),
            skip_reason=ac["last_skip"],
        )

    return [results[ev.slug] for ev in events if ev.slug in results]


def run_backtest_bankroll(
    events: list[EventData],
    cfg: LeggedArbConfig,
    bankroll: BankrollParams,
) -> tuple[list[MarketResult], BankrollSummary]:
    """Chronological replay with cash balance, sizing, and settlement credits."""
    clip, max_concurrent = sizing_for_bankroll(bankroll)
    cfg = replace(cfg, clip_size=clip, max_concurrent=max_concurrent)

    timeline: list[tuple[float, str, dict[str, dict]]] = []
    meta_by_slug: dict[str, EventData] = {ev.slug: ev for ev in events}
    for ev in events:
        expiry_ts = ev.window_ts + ev.duration_s
        for tick in ev.ticks:
            up = tick.get("Up") or {}
            dn = tick.get("Down") or {}
            ts = _parse_ts(up.get("ts_utc") or dn.get("ts_utc", "1970-01-01T00:00:00+00:00"))
            if expiry_ts - ts < 0:
                continue
            timeline.append((ts, ev.slug, tick))
    timeline.sort(key=lambda x: x[0])

    cash = bankroll.initial
    peak = trough = cash
    skipped_cash = 0
    settled_slugs: set[str] = set()

    states: dict[str, MarketArbState] = {}
    accounting: dict[str, dict] = {}
    results: dict[str, MarketResult] = {}

    def _acct(slug: str) -> dict:
        if slug not in accounting:
            accounting[slug] = {
                "phase1_cost": 0.0,
                "phase1_shares": 0.0,
                "phase2_revenue": 0.0,
                "phase2_sold": 0.0,
                "phase3_cost": 0.0,
                "phase3_shares": 0.0,
                "phase3_side": None,
                "last_skip": "",
                "had_stop_loss": False,
                "had_profit_sell": False,
            }
        return accounting[slug]

    def _track_cash() -> None:
        nonlocal peak, trough
        peak = max(peak, cash)
        trough = min(trough, cash)

    def _settle_slug(slug: str) -> None:
        nonlocal cash
        if slug in settled_slugs:
            return
        ev = meta_by_slug[slug]
        if ev.winner is None:
            settled_slugs.add(slug)
            return
        state = states.get(slug)
        ac = _acct(slug)
        if state is None or ac["phase1_cost"] <= 0:
            settled_slugs.add(slug)
            return
        payout = 0.0
        if ac["phase1_shares"] > 0 and state.phase1_side:
            payout += ac["phase1_shares"] * (
                1.0 if state.phase1_side == ev.winner else 0.0
            )
        if ac["phase3_shares"] > 0 and ac["phase3_side"]:
            payout += ac["phase3_shares"] * (
                1.0 if ac["phase3_side"] == ev.winner else 0.0
            )
        cash += payout
        _track_cash()
        if state.phase not in (ArbState.IDLE, ArbState.DONE):
            state.phase = ArbState.DONE
        settled_slugs.add(slug)

    for ts, slug, tick in timeline:
        ev = meta_by_slug[slug]
        expiry_ts = ev.window_ts + ev.duration_s
        book, sample_ts = _tick_book(tick)
        tte_s = expiry_ts - sample_ts

        if slug not in states:
            states[slug] = MarketArbState(slug=slug, yes_token_id="up", no_token_id="down")
        state = states[slug]
        ac = _acct(slug)

        if tte_s <= 0:
            _settle_slug(slug)
            continue
        if state.phase == ArbState.DONE or slug in settled_slugs:
            continue

        active = count_active_arbs(states)
        reserve = bankroll.initial * (1.0 - bankroll.max_deploy_fraction)

        if state.phase == ArbState.IDLE:
            decision = should_enter_phase1(book, tte_s, cfg, active_arb_count=active)
            if not decision.enter:
                ac["last_skip"] = decision.reason
                continue
            cost = decision.size * decision.price
            if cash - cost < reserve:
                ac["last_skip"] = "insufficient cash"
                skipped_cash += 1
                continue
            cash -= cost
            _track_cash()
            state.phase = ArbState.PHASE1_FILLED
            state.phase1_side = decision.side
            state.phase1_entry_price = decision.price
            state.phase1_filled_size = decision.size
            state.phase1_target_size = decision.size
            state.phase3_target_size = decision.size
            ac["phase1_cost"] += cost
            ac["phase1_shares"] = decision.size
            continue

        if state.phase == ArbState.PHASE1_FILLED:
            profit_decision = should_sell_phase2(state, book, tte_s, cfg)
            if profit_decision.sell:
                decision = profit_decision
                state.phase = ArbState.PHASE2_SOLD
                ac["had_profit_sell"] = True
            else:
                stop_decision = should_stop_loss(state, book, tte_s, cfg)
                if not stop_decision.sell:
                    continue
                decision = stop_decision
                state.phase = ArbState.DONE
                ac["had_stop_loss"] = True
            revenue = decision.size * decision.price
            cash += revenue
            _track_cash()
            state.phase2_sold_size = decision.size
            ac["phase2_sold"] = decision.size
            ac["phase2_revenue"] += revenue
            ac["phase1_shares"] = max(ac["phase1_shares"] - decision.size, 0.0)
            if state.phase == ArbState.DONE:
                _settle_slug(slug)
            continue

        if state.phase == ArbState.PHASE2_SOLD:
            decision = should_buy_phase3(state, book, tte_s, cfg)
            if not decision.buy:
                continue
            cost = decision.size * decision.price
            if cash < cost:
                ac["last_skip"] = "insufficient cash for phase3"
                skipped_cash += 1
                continue
            cash -= cost
            _track_cash()
            state.phase = ArbState.PHASE3_FILLED
            state.phase3_filled_size = decision.size
            ac["phase3_side"] = decision.side
            ac["phase3_cost"] += cost
            ac["phase3_shares"] = decision.size

    for ev in events:
        _settle_slug(ev.slug)
        state = states.get(ev.slug) or MarketArbState(
            slug=ev.slug, yes_token_id="up", no_token_id="down"
        )
        ac = _acct(ev.slug)
        pnl = _settle(
            state,
            ev.winner,
            phase1_cost=ac["phase1_cost"],
            phase1_shares=ac["phase1_shares"],
            phase2_revenue=ac["phase2_revenue"],
            phase3_cost=ac["phase3_cost"],
            phase3_shares=ac["phase3_shares"],
            phase3_side=ac["phase3_side"],
        )
        results[ev.slug] = MarketResult(
            slug=ev.slug,
            winner=ev.winner,
            phase=state.phase,
            phase1_side=state.phase1_side,
            phase1_entry=state.phase1_entry_price,
            phase1_cost=ac["phase1_cost"],
            phase1_shares=ac["phase1_shares"],
            phase2_revenue=ac["phase2_revenue"],
            phase2_sold=ac["phase2_sold"],
            phase3_cost=ac["phase3_cost"],
            phase3_shares=ac["phase3_shares"],
            phase3_side=ac["phase3_side"],
            pnl=pnl,
            had_phase1=ac["phase1_cost"] > 0,
            had_phase2=ac.get("had_profit_sell", False),
            had_phase3=ac["phase3_cost"] > 0,
            had_stop_loss=ac.get("had_stop_loss", False),
            skip_reason=ac["last_skip"],
        )

    br = BankrollSummary(
        initial=bankroll.initial,
        final=cash,
        peak=peak,
        trough=trough,
        max_drawdown=peak - trough,
        skipped_insufficient_cash=skipped_cash,
        clip_size=clip,
        max_concurrent=max_concurrent,
    )
    return [results[ev.slug] for ev in events if ev.slug in results], br


def summarize(
    results: list[MarketResult],
    cfg: LeggedArbConfig,
    meta: dict,
    *,
    verbose: bool = True,
    label: str = "LEGGED ARB BACKTEST",
) -> float:
    traded = [r for r in results if r.had_phase1 and r.winner]
    total_pnl = sum(r.pnl for r in traded)
    total_cost = sum(r.phase1_cost + r.phase3_cost for r in traded)
    wins = sum(1 for r in traded if r.pnl > 0)

    p1 = sum(1 for r in traded if r.had_phase1)
    p2 = sum(1 for r in traded if r.had_phase2)
    p3 = sum(1 for r in traded if r.had_phase3)
    sl = sum(1 for r in traded if r.had_stop_loss)
    full_arb = sum(1 for r in traded if r.had_phase1 and r.had_phase2 and r.had_phase3)

    if verbose:
        print(f"\n{'Slug':<42} {'P1':>5} {'P2':>5} {'P3':>5} {'Entry':>6} {'PnL':>9}")
        print("-" * 78)
        for r in sorted(traded, key=lambda x: x.pnl):
            slug_short = r.slug[-38:] if len(r.slug) > 38 else r.slug
            print(
                f"{slug_short:<42} "
                f"{'Y' if r.had_phase1 else 'n':>5} "
                f"{'Y' if r.had_phase2 else 'n':>5} "
                f"{'Y' if r.had_phase3 else 'n':>5} "
                f"${r.phase1_entry:>5.2f} "
                f"${r.pnl:>+8.2f}"
            )
        print("-" * 78)

    print(f"\n{'SUMMARY':=^78}")
    if meta:
        print(
            f"  Data: {meta.get('run_started_utc', '?')}  "
            f"{meta.get('duration_minutes', '?')}m  N={meta.get('n_events', '?')}"
        )
    print(
        f"  Config: p1=[{cfg.phase1_price_min:.2f},{cfg.phase1_price_max:.2f}] "
        f"tte=[{cfg.phase1_tte_min_s:.0f},{cfg.phase1_tte_max_s:.0f}]s "
        f"p2_uplift={cfg.phase2_uplift:.2f} p3_max={cfg.phase3_max_price:.2f} "
        f"clip={cfg.clip_size:.0f} stop_loss={cfg.stop_loss_drop:.2f}"
    )
    print(f"  Markets in data:     {len(results)}")
    print(f"  Phase-1 entries:     {p1}")
    print(f"  Phase-2 sells:       {p2}  ({100*p2/max(p1,1):.1f}% of P1)")
    print(f"  Phase-3 cheap legs:  {p3}  ({100*p3/max(p1,1):.1f}% of P1)")
    print(f"  Stop-loss exits:     {sl}  ({100*sl/max(p1,1):.1f}% of P1)")
    print(f"  Full 3-phase arbs:   {full_arb}")
    print(f"  Win rate:            {wins / max(len(traded), 1) * 100:.1f}%")
    print(f"  Total capital used:  ${total_cost:.2f}")
    print(f"  Total P&L:           ${total_pnl:+.2f}")
    if total_cost:
        print(f"  ROI:                 {total_pnl / total_cost * 100:+.2f}%")
    if traded:
        print(f"  Avg P&L / entry:     ${total_pnl / p1:+.2f}")
    print("=" * 78)
    return total_pnl


def summarize_bankroll(
    results: list[MarketResult],
    cfg: LeggedArbConfig,
    meta: dict,
    br: BankrollSummary,
    *,
    verbose: bool = True,
) -> float:
    total_pnl = summarize(results, cfg, meta, verbose=verbose)
    roi = (br.final - br.initial) / br.initial * 100 if br.initial else 0.0
    print(f"\n{'BANKROLL':=^78}")
    print(f"  Initial balance:     ${br.initial:.2f}")
    print(f"  Final balance:       ${br.final:.2f}")
    print(f"  Net P&L:             ${br.final - br.initial:+.2f}")
    print(f"  ROI on bankroll:     {roi:+.2f}%")
    print(f"  Peak balance:        ${br.peak:.2f}")
    print(f"  Trough balance:      ${br.trough:.2f}")
    print(f"  Max drawdown:        ${br.max_drawdown:.2f}")
    print(f"  Clip size:           {br.clip_size:.0f} shares (~${br.clip_size * 0.75:.0f}/trade @ $0.75)")
    print(f"  Max concurrent:      {br.max_concurrent}")
    print(f"  Skipped (no cash):   {br.skipped_insufficient_cash}")
    print("=" * 78)
    return br.final - br.initial


def main() -> int:
    parser = argparse.ArgumentParser(description="Backtest legged arb on collector data")
    parser.add_argument("--data", type=Path, help="Single collector .jsonl.gz file")
    parser.add_argument("--data-dir", type=Path, default=Path("data/collectors"))
    parser.add_argument(
        "--bankroll",
        type=float,
        default=1000.0,
        help="Starting USDC balance (0 = unlimited per-market mode)",
    )
    parser.add_argument(
        "--position-pct",
        type=float,
        default=0.08,
        help="Fraction of bankroll per Phase-1 entry (default 8%%)",
    )
    parser.add_argument("--concurrent", action="store_true", help="Also run max_concurrent-only mode")
    parser.add_argument("--unlimited", action="store_true", help="Unlimited capital per-market mode")
    parser.add_argument("--quiet", action="store_true")
    parser.add_argument("--clip", type=float, default=None, help="Override auto clip size")
    parser.add_argument("--p1-min", type=float, default=0.70)
    parser.add_argument("--p1-max", type=float, default=0.95)
    parser.add_argument("--p2-uplift", type=float, default=0.14)
    parser.add_argument("--p3-max", type=float, default=0.05)
    parser.add_argument("--stop-loss", type=float, default=0.10, help="Bid drop to trigger stop (0=off)")
    parser.add_argument("--no-stop-loss", action="store_true")
    args = parser.parse_args()

    if args.data:
        data_files = [args.data]
    else:
        data_files = sorted(args.data_dir.glob("btc_15m*.jsonl.gz"))
        if not data_files:
            data_files = sorted(args.data_dir.glob("*.jsonl.gz"))
    if not data_files:
        print("No collector data found.")
        return 1

    bankroll_params = BankrollParams(
        initial=args.bankroll,
        position_fraction=args.position_pct,
    )
    clip_auto, max_conc_auto = sizing_for_bankroll(bankroll_params)
    clip_size = args.clip if args.clip is not None else clip_auto

    cfg = LeggedArbConfig(
        clip_size=clip_size,
        max_concurrent=max_conc_auto,
        phase1_price_min=args.p1_min,
        phase1_price_max=args.p1_max,
        phase2_uplift=args.p2_uplift,
        phase3_max_price=args.p3_max,
        stop_loss_drop=0.0 if args.no_stop_loss else args.stop_loss,
    )

    print(f"Loading {len(data_files)} file(s)...")
    events, meta = load_events(data_files)
    known = sum(1 for e in events if e.winner)
    print(f"Loaded {len(events)} events, {known} with known outcomes")

    use_bankroll = args.bankroll > 0 and not args.unlimited
    if use_bankroll:
        print("=" * 78)
        print(f"LEGGED ARB BACKTEST (${args.bankroll:.0f} bankroll)")
        print("=" * 78)
        results, br = run_backtest_bankroll(events, cfg, bankroll_params)
        if args.clip is not None:
            br = BankrollSummary(
                initial=br.initial,
                final=br.final,
                peak=br.peak,
                trough=br.trough,
                max_drawdown=br.max_drawdown,
                skipped_insufficient_cash=br.skipped_insufficient_cash,
                clip_size=clip_size,
                max_concurrent=br.max_concurrent,
            )
        summarize_bankroll(results, cfg, meta, br, verbose=not args.quiet)
    else:
        print("=" * 78)
        print("LEGGED ARB BACKTEST (unlimited per-market)")
        print("=" * 78)
        results = run_backtest(events, cfg)
        summarize(results, cfg, meta, verbose=not args.quiet)

    if args.concurrent:
        print("\n" + "=" * 78)
        print("LEGGED ARB BACKTEST (max_concurrent gate, no bankroll)")
        print("=" * 78)
        results_c = run_backtest_concurrent(events, cfg)
        summarize(results_c, cfg, meta, verbose=not args.quiet, label="CONCURRENT")

    return 0


if __name__ == "__main__":
    sys.exit(main())
