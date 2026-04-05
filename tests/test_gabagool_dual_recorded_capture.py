"""Replay a real BTC 5m collector capture through gabagool_dual (smoke).

Fixture comes from EC2 collector output (N=1 window), stored under
``tests/fixtures/collector_captures/`` so CI does not depend on S3.
"""

from __future__ import annotations

import asyncio
import gzip
import json
from pathlib import Path

from src.core.events import BookUpdate
from src.strategy.base import StrategyContext
from src.strategy.gabagool_dual_adapter import GabagoolDualConfig, GabagoolDualStrategy

FIXTURE = (
    Path(__file__).resolve().parent
    / "fixtures"
    / "collector_captures"
    / "btc_eth_5m_N1.jsonl.gz"
)


def _load_btc_batches(path: Path) -> tuple[str, dict[int, list[dict]]]:
    """Parse gz JSONL; return (btc_slug, sample_seq -> rows for that slug)."""
    slug_batches: dict[str, dict[int, list[dict]]] = {}
    with gzip.open(path, "rt", encoding="utf-8") as fh:
        for line in fh:
            row = json.loads(line)
            if row.get("type") != "sample":
                continue
            slug = row.get("slug") or ""
            if not slug.startswith("btc-updown-5m-"):
                continue
            if not row.get("data_ready"):
                continue
            bid = row.get("best_bid")
            ask = row.get("best_ask")
            if bid is None or ask is None:
                continue
            seq = int(row["sample_seq"])
            slug_batches.setdefault(slug, {}).setdefault(seq, []).append(row)

    if not slug_batches:
        raise ValueError("no btc-updown-5m samples in capture")
    btc_slug = sorted(slug_batches.keys())[0]
    return btc_slug, slug_batches[btc_slug]


def _ctx_for_tick(slug: str, up_row: dict, down_row: dict) -> StrategyContext:
    yes_tid = str(up_row["token_id"])
    no_tid = str(down_row["token_id"])
    return StrategyContext(
        market_meta={
            slug: {
                "token_ids": (yes_tid, no_tid),
                "outcomes": ("Up", "Down"),
                "condition_id": "",
            }
        },
        best_prices={
            yes_tid: {
                "bid": float(up_row["best_bid"]),
                "ask": float(up_row["best_ask"]),
            },
            no_tid: {
                "bid": float(down_row["best_bid"]),
                "ask": float(down_row["best_ask"]),
            },
        },
        tick_sizes={yes_tid: 0.01, no_tid: 0.01},
        dry_run=True,
    )


def _book_event_yes(slug: str, ctx: StrategyContext, yes_tid: str) -> BookUpdate:
    bp = ctx.best_prices[yes_tid]
    bid, ask = float(bp["bid"]), float(bp["ask"])
    return BookUpdate(
        token_id=yes_tid,
        condition_id="",
        slug=slug,
        bids=((bid, 100.0),),
        asks=((ask, 100.0),),
        best_bid=bid,
        best_ask=ask,
    )


async def _replay_btc_ticks(strategy: GabagoolDualStrategy, slug: str, batches: dict[int, list[dict]]) -> tuple[int, int]:
    ticks = 0
    intent_count = 0
    for seq in sorted(batches.keys()):
        rows = batches[seq]
        by_outcome = {r["outcome"]: r for r in rows}
        if "Up" not in by_outcome or "Down" not in by_outcome:
            continue
        up_row, down_row = by_outcome["Up"], by_outcome["Down"]
        ctx = _ctx_for_tick(slug, up_row, down_row)
        ev = _book_event_yes(slug, ctx, str(up_row["token_id"]))
        out = await strategy.on_book_update(ev, ctx)
        ticks += 1
        if out:
            intent_count += len(out)
    return ticks, intent_count


class TestGabagoolDualRecordedCapture:
    def test_fixture_present(self) -> None:
        assert FIXTURE.is_file(), f"missing fixture: {FIXTURE}"

    def test_replay_btc_5m_capture_smoke(self) -> None:
        slug, batches = _load_btc_batches(FIXTURE)
        strategy = GabagoolDualStrategy(
            config=GabagoolDualConfig(
                observation_ticks=0,
                trend_min_reversals=0,
                trend_min_amplitude=0.0,
                max_pair_cost=0.999,
                cooldown_pair_cost=1.01,
                resume_pair_cost=0.99,
                base_order_size=2.0,
                min_order_notional_usd=0.0,
            )
        )
        ticks, intent_count = asyncio.run(_replay_btc_ticks(strategy, slug, batches))
        # N1 run sampled ~1 Hz for ~40s with 4 rows per tick across BTC+ETH -> ~40 BTC ticks.
        assert ticks >= 30
        assert intent_count >= 0
