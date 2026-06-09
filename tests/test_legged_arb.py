"""Unit tests for legged arbitrage decision logic and adapter."""

from __future__ import annotations

from dataclasses import replace

import pytest

from src.core.events import BookUpdate, MarketResolved
from src.core.models import Side
from src.strategy.base import StrategyContext
from src.strategy.legged_arb import (
    ArbState,
    LeggedArbConfig,
    MarketArbState,
    build_market_book,
    count_active_arbs,
    is_eligible_slug,
    should_buy_phase3,
    should_enter_phase1,
    should_sell_phase2,
)
from src.strategy.legged_arb_adapter import LeggedArbStrategy


TOKEN_UP = "token_up"
TOKEN_DOWN = "token_down"
SLUG = "btc-updown-15m-1700000000"


def _cfg(**overrides) -> LeggedArbConfig:
    return replace(LeggedArbConfig(), **overrides)


def _favorite_book(
    *,
    fav_ask: float = 0.82,
    fav_bid: float = 0.81,
    opp_ask: float = 0.18,
    opp_bid: float = 0.17,
    fav_is_up: bool = True,
    ask_depth: float = 5000.0,
):
    if fav_is_up:
        return build_market_book(
            up_bid=fav_bid,
            up_ask=fav_ask,
            up_ask_size=500.0,
            up_bid_depth=3000.0,
            up_ask_depth=ask_depth,
            down_bid=opp_bid,
            down_ask=opp_ask,
            down_ask_size=500.0,
            down_bid_depth=3000.0,
            down_ask_depth=ask_depth,
        )
    return build_market_book(
        up_bid=opp_bid,
        up_ask=opp_ask,
        up_ask_size=500.0,
        up_bid_depth=3000.0,
        up_ask_depth=ask_depth,
        down_bid=fav_bid,
        down_ask=fav_ask,
        down_ask_size=500.0,
        down_bid_depth=3000.0,
        down_ask_depth=ask_depth,
    )


class TestSlugEligibility:
    def test_btc_15m_slug_matches(self):
        assert is_eligible_slug(SLUG, _cfg())

    def test_eth_15m_slug_rejected_when_btc_only(self):
        assert not is_eligible_slug("eth-updown-15m-1700000000", _cfg())

    def test_btc_5m_slug_rejected_when_15_only(self):
        assert not is_eligible_slug("btc-updown-5m-1700000000", _cfg())


class TestPhase1Entry:
    def test_enters_when_favorite_in_band_and_tte_ok(self):
        book = _favorite_book(fav_ask=0.82)
        decision = should_enter_phase1(book, tte_s=600, cfg=_cfg(), active_arb_count=0)
        assert decision.enter is True
        assert decision.side == "Up"
        assert decision.size > 0

    def test_skips_when_favorite_too_cheap(self):
        book = _favorite_book(fav_ask=0.55)
        decision = should_enter_phase1(book, tte_s=600, cfg=_cfg(), active_arb_count=0)
        assert decision.enter is False
        assert "too cheap" in decision.reason

    def test_skips_when_favorite_too_expensive(self):
        book = _favorite_book(fav_ask=0.97)
        decision = should_enter_phase1(book, tte_s=600, cfg=_cfg(), active_arb_count=0)
        assert decision.enter is False

    def test_skips_when_tte_too_low(self):
        book = _favorite_book()
        decision = should_enter_phase1(book, tte_s=120, cfg=_cfg(), active_arb_count=0)
        assert decision.enter is False

    def test_skips_when_tte_too_high(self):
        book = _favorite_book()
        decision = should_enter_phase1(book, tte_s=900, cfg=_cfg(), active_arb_count=0)
        assert decision.enter is False

    def test_skips_when_spread_too_wide(self):
        book = _favorite_book(fav_bid=0.70, fav_ask=0.82)
        decision = should_enter_phase1(book, tte_s=600, cfg=_cfg(max_spread=0.01), active_arb_count=0)
        assert decision.enter is False

    def test_skips_when_depth_too_thin(self):
        book = _favorite_book(ask_depth=100.0)
        decision = should_enter_phase1(book, tte_s=600, cfg=_cfg(), active_arb_count=0)
        assert decision.enter is False

    def test_skips_at_max_concurrent(self):
        book = _favorite_book()
        decision = should_enter_phase1(book, tte_s=600, cfg=_cfg(max_concurrent=2), active_arb_count=2)
        assert decision.enter is False


class TestPhase2Sell:
    def _filled_state(self) -> MarketArbState:
        return MarketArbState(
            slug=SLUG,
            yes_token_id=TOKEN_UP,
            no_token_id=TOKEN_DOWN,
            phase=ArbState.PHASE1_FILLED,
            phase1_side="Up",
            phase1_entry_price=0.70,
            phase1_filled_size=150.0,
        )

    def test_sell_when_bid_uplift_threshold_met(self):
        book = _favorite_book(fav_bid=0.86, fav_ask=0.87)
        decision = should_sell_phase2(self._filled_state(), book, tte_s=300, cfg=_cfg())
        assert decision.sell is True
        assert decision.side == "Up"
        assert decision.size == 149.0

    def test_sell_when_absolute_bid_high(self):
        book = _favorite_book(fav_bid=0.91, fav_ask=0.92)
        decision = should_sell_phase2(self._filled_state(), book, tte_s=300, cfg=_cfg())
        assert decision.sell is True

    def test_no_sell_when_bid_below_threshold(self):
        book = _favorite_book(fav_bid=0.75, fav_ask=0.76)
        decision = should_sell_phase2(self._filled_state(), book, tte_s=300, cfg=_cfg())
        assert decision.sell is False

    def test_no_sell_after_tte_cutoff(self):
        book = _favorite_book(fav_bid=0.95, fav_ask=0.96)
        decision = should_sell_phase2(self._filled_state(), book, tte_s=30, cfg=_cfg())
        assert decision.sell is False


class TestPhase3CheapLeg:
    def _sold_state(self) -> MarketArbState:
        return MarketArbState(
            slug=SLUG,
            yes_token_id=TOKEN_UP,
            no_token_id=TOKEN_DOWN,
            phase=ArbState.PHASE2_SOLD,
            phase1_side="Up",
            phase1_filled_size=1.0,
            phase3_target_size=150.0,
        )

    def test_attempts_phase3_after_sell(self):
        book = _favorite_book(fav_ask=0.95, fav_bid=0.94, opp_ask=0.03, opp_bid=0.02)
        decision = should_buy_phase3(self._sold_state(), book, tte_s=120, cfg=_cfg())
        assert decision.buy is True
        assert decision.side == "Down"

    def test_skips_when_phase3_too_expensive(self):
        book = _favorite_book(fav_ask=0.95, fav_bid=0.94, opp_ask=0.20, opp_bid=0.18)
        decision = should_buy_phase3(self._sold_state(), book, tte_s=120, cfg=_cfg())
        assert decision.buy is False

    def test_skips_when_tte_too_high(self):
        book = _favorite_book(fav_ask=0.95, fav_bid=0.94, opp_ask=0.03, opp_bid=0.02)
        decision = should_buy_phase3(self._sold_state(), book, tte_s=600, cfg=_cfg())
        assert decision.buy is False


class TestActiveArbCount:
    def test_counts_non_idle_states(self):
        states = {
            "a": MarketArbState(slug="a", yes_token_id="1", no_token_id="2", phase=ArbState.IDLE),
            "b": MarketArbState(slug="b", yes_token_id="3", no_token_id="4", phase=ArbState.PHASE1_FILLED),
            "c": MarketArbState(slug="c", yes_token_id="5", no_token_id="6", phase=ArbState.DONE),
        }
        assert count_active_arbs(states) == 1


def _ctx() -> StrategyContext:
    return StrategyContext(
        market_meta={
            SLUG: {
                "token_ids": (TOKEN_UP, TOKEN_DOWN),
                "outcomes": ("Up", "Down"),
                "condition_id": "cond",
            }
        },
        best_prices={
            TOKEN_UP: {"bid": 0.81, "ask": 0.82, "ask_size": 500, "ask_depth": 5000, "bid_depth": 3000},
            TOKEN_DOWN: {"bid": 0.17, "ask": 0.18, "ask_size": 500, "ask_depth": 5000, "bid_depth": 3000},
        },
        tick_sizes={TOKEN_UP: 0.01, TOKEN_DOWN: 0.01},
        dry_run=True,
    )


@pytest.mark.anyio
class TestLeggedArbAdapter:
    async def test_emits_phase1_intent(self, monkeypatch):
        monkeypatch.setattr(
            "src.strategy.legged_arb_adapter.extract_market_end_ts",
            lambda slug: 9999999999,
        )
        monkeypatch.setattr(
            "src.strategy.legged_arb_adapter.time.time",
            lambda: 9999999999 - 600,
        )
        strategy = LeggedArbStrategy(config=_cfg())
        event = BookUpdate(
            token_id=TOKEN_UP,
            condition_id="cond",
            slug=SLUG,
            bids=((0.81, 500.0),),
            asks=((0.82, 500.0),),
            best_bid=0.81,
            best_ask=0.82,
        )
        intents = await strategy.on_book_update(event, _ctx())
        assert intents is not None
        assert len(intents) == 1
        assert intents[0].side == Side.BUY
        assert intents[0].strategy == "legged_arb"

    async def test_fill_transitions_to_phase1_filled(self, monkeypatch):
        monkeypatch.setattr(
            "src.strategy.legged_arb_adapter.extract_market_end_ts",
            lambda slug: 9999999999,
        )
        monkeypatch.setattr(
            "src.strategy.legged_arb_adapter.time.time",
            lambda: 9999999999 - 600,
        )
        strategy = LeggedArbStrategy(config=_cfg())
        await strategy.on_book_update(
            BookUpdate(
                token_id=TOKEN_UP,
                condition_id="cond",
                slug=SLUG,
                bids=((0.81, 500.0),),
                asks=((0.82, 500.0),),
                best_bid=0.81,
                best_ask=0.82,
            ),
            _ctx(),
        )
        strategy.on_fill_event(TOKEN_UP, 150.0, 0.82)
        state = strategy.get_slug_state(SLUG)
        assert state is not None
        assert state.phase == ArbState.PHASE1_FILLED

    async def test_market_resolved_cleans_up(self):
        strategy = LeggedArbStrategy(config=_cfg())
        ctx = _ctx()
        await strategy.on_book_update(
            BookUpdate(
                token_id=TOKEN_UP,
                condition_id="cond",
                slug=SLUG,
                bids=((0.81, 500.0),),
                asks=((0.82, 500.0),),
                best_bid=0.81,
                best_ask=0.82,
            ),
            ctx,
        )
        await strategy.on_market_resolved(
            MarketResolved(
                slug=SLUG,
                condition_id="cond",
                winning_token_id=TOKEN_UP,
            ),
            ctx,
        )
        assert strategy.get_slug_state(SLUG) is None
