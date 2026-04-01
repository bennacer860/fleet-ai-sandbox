"""Unit tests for the gabagool_dual strategy adapter."""

import asyncio

from src.core.events import BookUpdate
from src.strategy.base import StrategyContext
from src.strategy.gabagool_dual_adapter import GabagoolDualConfig, GabagoolDualStrategy

TOKEN_YES = "token_yes_dual"
TOKEN_NO = "token_no_dual"
SLUG = "btc-updown-15m-dual"
COND = "cond_dual"


def _ctx(yes_ask: float, no_ask: float) -> StrategyContext:
    return StrategyContext(
        market_meta={
            SLUG: {
                "token_ids": (TOKEN_YES, TOKEN_NO),
                "outcomes": ("Up", "Down"),
                "condition_id": COND,
            }
        },
        best_prices={
            TOKEN_YES: {"ask": yes_ask, "bid": max(0.01, yes_ask - 0.01)},
            TOKEN_NO: {"ask": no_ask, "bid": max(0.01, no_ask - 0.01)},
        },
        tick_sizes={TOKEN_YES: 0.01, TOKEN_NO: 0.01},
        dry_run=True,
    )


def _event(yes_ask: float) -> BookUpdate:
    return BookUpdate(
        token_id=TOKEN_YES,
        condition_id=COND,
        slug=SLUG,
        bids=((max(0.01, yes_ask - 0.01), 100.0),),
        asks=((yes_ask, 100.0),),
        best_bid=max(0.01, yes_ask - 0.01),
        best_ask=yes_ask,
    )


class TestDualAdapter:
    def test_name(self) -> None:
        assert GabagoolDualStrategy().name() == "gabagool_dual"

    def test_emits_two_intents_when_profitable(self) -> None:
        s = GabagoolDualStrategy(
            config=GabagoolDualConfig(
                observation_ticks=0,
                trend_min_reversals=0,
                trend_min_amplitude=0.0,
                max_pair_cost=0.99,
                cooldown_pair_cost=1.01,
                resume_pair_cost=0.99,
                base_order_size=2.0,
                min_order_notional_usd=0.0,
            )
        )
        intents = asyncio.run(s.on_book_update(_event(0.54), _ctx(0.54, 0.43)))
        assert intents is not None
        assert len(intents) == 2
        token_ids = {i.token_id for i in intents}
        assert token_ids == {TOKEN_YES, TOKEN_NO}
        assert all(i.strategy == "gabagool_dual" for i in intents)
        assert all(i.skip_dedup for i in intents)

    def test_cooldown_and_resume(self) -> None:
        s = GabagoolDualStrategy(
            config=GabagoolDualConfig(
                observation_ticks=0,
                trend_min_reversals=0,
                trend_min_amplitude=0.0,
                max_pair_cost=0.999,
                cooldown_pair_cost=0.995,
                resume_pair_cost=0.985,
                base_order_size=2.0,
                min_order_notional_usd=0.0,
            )
        )
        # Tight spread -> enters cooldown.
        first = asyncio.run(s.on_book_update(_event(0.56), _ctx(0.56, 0.44)))
        assert first is None
        # Still tight -> no intents.
        second = asyncio.run(s.on_book_update(_event(0.55), _ctx(0.55, 0.44)))
        assert second is None
        # Wider spread below resume -> trading resumes.
        third = asyncio.run(s.on_book_update(_event(0.54), _ctx(0.54, 0.43)))
        assert third is not None
        assert len(third) == 2

    def test_imbalance_blocks_heavier_side(self) -> None:
        s = GabagoolDualStrategy(
            config=GabagoolDualConfig(
                observation_ticks=0,
                trend_min_reversals=0,
                trend_min_amplitude=0.0,
                max_pair_cost=0.99,
                cooldown_pair_cost=1.01,
                resume_pair_cost=0.99,
                base_order_size=4.0,
                max_imbalance=2.0,
                min_order_notional_usd=0.0,
            )
        )
        asyncio.run(s.on_book_update(_event(0.54), _ctx(0.54, 0.43)))
        s.on_fill_event(TOKEN_YES, fill_size=20.0, fill_price=0.54)
        s.on_fill_event(TOKEN_NO, fill_size=5.0, fill_price=0.43)
        intents = asyncio.run(s.on_book_update(_event(0.54), _ctx(0.54, 0.43)))
        assert intents is not None
        assert len(intents) == 1
        assert intents[0].token_id == TOKEN_NO

    def test_min_notional_resize_applies(self) -> None:
        s = GabagoolDualStrategy(
            config=GabagoolDualConfig(
                observation_ticks=0,
                trend_min_reversals=0,
                trend_min_amplitude=0.0,
                max_pair_cost=0.99,
                cooldown_pair_cost=1.01,
                resume_pair_cost=0.99,
                base_order_size=2.0,
                min_order_notional_usd=1.0,
            )
        )
        intents = asyncio.run(s.on_book_update(_event(0.25), _ctx(0.25, 0.70)))
        assert intents is not None
        yes_intent = next(i for i in intents if i.token_id == TOKEN_YES)
        assert yes_intent.size >= 4.0

    def test_notional_budget_caps_orders(self) -> None:
        s = GabagoolDualStrategy(
            config=GabagoolDualConfig(
                observation_ticks=0,
                trend_min_reversals=0,
                trend_min_amplitude=0.0,
                max_pair_cost=0.99,
                cooldown_pair_cost=1.01,
                resume_pair_cost=0.99,
                base_order_size=10.0,
                max_notional_per_slug=2.0,
                min_order_notional_usd=0.0,
            )
        )
        intents = asyncio.run(s.on_book_update(_event(0.54), _ctx(0.54, 0.43)))
        assert intents is not None
        assert len(intents) <= 2
        total_notional = sum(i.price * i.size for i in intents)
        assert total_notional <= 2.0 + 1e-6
