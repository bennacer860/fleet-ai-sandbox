"""Unit tests for the GabagoolStrategy adapter.

Tests the bridge between gabagool pure logic and the bot's event-driven
Strategy interface: BookUpdate -> OrderIntent conversion, fill sync
back into PairState, and multi-slug state isolation.
"""

import pytest

from src.core.events import BookUpdate, MarketResolved, TickSizeChange
from src.core.models import OrderIntent, Side
from src.strategy.base import StrategyContext
from src.strategy.gabagool_adapter import GabagoolConfig, GabagoolStrategy, SlugState


# ═══════════════════════════════════════════════════════════════════════════════
# Helpers
# ═══════════════════════════════════════════════════════════════════════════════

TOKEN_YES_A = "token_yes_aaa"
TOKEN_NO_A = "token_no_aaa"
SLUG_A = "btc-up-5m-test-a"
CONDITION_A = "cond_aaa"

TOKEN_YES_B = "token_yes_bbb"
TOKEN_NO_B = "token_no_bbb"
SLUG_B = "eth-up-5m-test-b"
CONDITION_B = "cond_bbb"


def _make_ctx(
    slugs: list[tuple[str, str, str, str]] | None = None,
    prices: dict[str, dict[str, float]] | None = None,
    tick_sizes: dict[str, float] | None = None,
) -> StrategyContext:
    """Build a StrategyContext with market_meta for the given slugs.

    Each slug entry is (slug, yes_token_id, no_token_id, condition_id).
    """
    if slugs is None:
        slugs = [(SLUG_A, TOKEN_YES_A, TOKEN_NO_A, CONDITION_A)]

    meta: dict[str, dict] = {}
    for slug, yes_tid, no_tid, cond_id in slugs:
        meta[slug] = {
            "token_ids": (yes_tid, no_tid),
            "outcomes": ("Up", "Down"),
            "condition_id": cond_id,
        }

    return StrategyContext(
        market_meta=meta,
        best_prices=prices or {},
        tick_sizes=tick_sizes or {},
        dry_run=True,
    )


def _book_update(
    token_id: str,
    slug: str,
    best_ask: float = 0.50,
    best_bid: float = 0.48,
    condition_id: str = CONDITION_A,
) -> BookUpdate:
    return BookUpdate(
        token_id=token_id,
        condition_id=condition_id,
        slug=slug,
        bids=((best_bid, 100.0),),
        asks=((best_ask, 100.0),),
        best_bid=best_bid,
        best_ask=best_ask,
    )


def _oscillating_prices(n: int, center: float = 0.50, amplitude: float = 0.20) -> list[float]:
    """Generate oscillating YES ask prices to trigger TrendDetector activation."""
    import math
    return [center + amplitude * math.sin(2 * math.pi * i / 10) for i in range(n)]


# ═══════════════════════════════════════════════════════════════════════════════
# Strategy identity
# ═══════════════════════════════════════════════════════════════════════════════


class TestStrategyInterface:
    def test_name(self):
        s = GabagoolStrategy()
        assert s.name() == "gabagool"

    def test_implements_strategy_methods(self):
        s = GabagoolStrategy()
        assert hasattr(s, "on_tick_size_change")
        assert hasattr(s, "on_book_update")
        assert hasattr(s, "on_market_resolved")
        assert hasattr(s, "on_fill_event")


# ═══════════════════════════════════════════════════════════════════════════════
# Slug state initialization
# ═══════════════════════════════════════════════════════════════════════════════


class TestSlugStateInit:
    def test_creates_state_on_first_book_update(self):
        s = GabagoolStrategy()
        ctx = _make_ctx(prices={
            TOKEN_YES_A: {"ask": 0.50},
            TOKEN_NO_A: {"ask": 0.50},
        })
        import asyncio
        asyncio.run(s.on_book_update(
            _book_update(TOKEN_YES_A, SLUG_A, best_ask=0.50), ctx
        ))
        state = s.get_slug_state(SLUG_A)
        assert state is not None
        assert state.yes_token_id == TOKEN_YES_A
        assert state.no_token_id == TOKEN_NO_A

    def test_no_state_without_market_meta(self):
        s = GabagoolStrategy()
        ctx = StrategyContext(dry_run=True)
        import asyncio
        result = asyncio.run(s.on_book_update(
            _book_update(TOKEN_YES_A, "unknown-slug"), ctx
        ))
        assert result is None

    def test_token_to_slug_mapping(self):
        s = GabagoolStrategy()
        ctx = _make_ctx(prices={
            TOKEN_YES_A: {"ask": 0.50},
            TOKEN_NO_A: {"ask": 0.50},
        })
        import asyncio
        asyncio.run(s.on_book_update(
            _book_update(TOKEN_YES_A, SLUG_A), ctx
        ))
        assert s.token_to_slug(TOKEN_YES_A) == SLUG_A
        assert s.token_to_slug(TOKEN_NO_A) == SLUG_A
        assert s.token_to_slug("unknown") is None


# ═══════════════════════════════════════════════════════════════════════════════
# BookUpdate -> OrderIntent conversion
# ═══════════════════════════════════════════════════════════════════════════════


class TestBookToIntent:
    """Test that BookUpdates eventually produce correct OrderIntents."""

    def _run_through_activation(
        self,
        strategy: GabagoolStrategy | None = None,
        config: GabagoolConfig | None = None,
    ) -> tuple[GabagoolStrategy, list[OrderIntent]]:
        """Feed enough oscillating prices to activate and produce an intent."""
        import asyncio

        cfg = config or GabagoolConfig(
            observation_ticks=5,
            trend_min_reversals=1,
            trend_min_amplitude=0.10,
            base_order_size=10.0,
            probe_size_factor=0.25,
        )
        s = strategy or GabagoolStrategy(config=cfg)

        prices = _oscillating_prices(40, center=0.50, amplitude=0.25)
        all_intents: list[OrderIntent] = []

        for yes_ask in prices:
            no_ask = max(0.01, min(0.99, 1.0 - yes_ask))
            ctx = _make_ctx(prices={
                TOKEN_YES_A: {"ask": yes_ask},
                TOKEN_NO_A: {"ask": no_ask},
            })
            result = asyncio.run(s.on_book_update(
                _book_update(TOKEN_YES_A, SLUG_A, best_ask=yes_ask), ctx
            ))
            if result:
                all_intents.extend(result)

        return s, all_intents

    def test_produces_intents_after_activation(self):
        s, intents = self._run_through_activation()
        assert len(intents) > 0, "Expected at least one OrderIntent after activation"

    def test_intent_has_correct_fields(self):
        s, intents = self._run_through_activation()
        assert len(intents) > 0
        intent = intents[0]
        assert intent.strategy == "gabagool"
        assert intent.side == Side.BUY
        assert intent.slug == SLUG_A
        assert intent.token_id in (TOKEN_YES_A, TOKEN_NO_A)
        assert intent.price > 0
        assert intent.size > 0

    def test_probe_phase_uses_reduced_size(self):
        cfg = GabagoolConfig(
            observation_ticks=5,
            trend_min_reversals=1,
            trend_min_amplitude=0.10,
            base_order_size=10.0,
            probe_size_factor=0.25,
        )
        s, intents = self._run_through_activation(config=cfg)
        assert len(intents) > 0
        assert intents[0].size == 10.0 * 0.25

    def test_no_intent_during_observation(self):
        import asyncio
        cfg = GabagoolConfig(observation_ticks=100)
        s = GabagoolStrategy(config=cfg)

        ctx = _make_ctx(prices={
            TOKEN_YES_A: {"ask": 0.50},
            TOKEN_NO_A: {"ask": 0.50},
        })
        for _ in range(10):
            result = asyncio.run(s.on_book_update(
                _book_update(TOKEN_YES_A, SLUG_A, best_ask=0.50), ctx
            ))
            assert result is None

    def test_no_intent_without_reversal(self):
        """Monotonic prices should not trigger activation."""
        import asyncio
        cfg = GabagoolConfig(
            observation_ticks=3,
            trend_min_reversals=1,
            trend_min_amplitude=0.10,
        )
        s = GabagoolStrategy(config=cfg)

        for i in range(30):
            yes_ask = 0.50 + i * 0.01
            no_ask = max(0.01, 1.0 - yes_ask)
            ctx = _make_ctx(prices={
                TOKEN_YES_A: {"ask": yes_ask},
                TOKEN_NO_A: {"ask": no_ask},
            })
            result = asyncio.run(s.on_book_update(
                _book_update(TOKEN_YES_A, SLUG_A, best_ask=yes_ask), ctx
            ))
            assert result is None

        state = s.get_slug_state(SLUG_A)
        assert state is not None
        assert state.activated is False


# ═══════════════════════════════════════════════════════════════════════════════
# Fill sync
# ═══════════════════════════════════════════════════════════════════════════════


class TestFillSync:
    """Test that fills are correctly synced back into PairState."""

    def test_fill_updates_pair_state(self):
        import asyncio
        cfg = GabagoolConfig(observation_ticks=3, trend_min_amplitude=0.05)
        s = GabagoolStrategy(config=cfg)
        ctx = _make_ctx(prices={
            TOKEN_YES_A: {"ask": 0.40},
            TOKEN_NO_A: {"ask": 0.55},
        })
        asyncio.run(s.on_book_update(
            _book_update(TOKEN_YES_A, SLUG_A, best_ask=0.40), ctx
        ))

        s.on_fill_event(token_id=TOKEN_YES_A, fill_size=10.0, fill_price=0.40)

        state = s.get_slug_state(SLUG_A)
        assert state is not None
        assert state.pair.qty_yes == 10.0
        assert abs(state.pair.cost_yes - 10.0 * 0.40) < 1e-9

    def test_fill_records_phase_transition(self):
        import asyncio
        cfg = GabagoolConfig(observation_ticks=3, trend_min_amplitude=0.05)
        s = GabagoolStrategy(config=cfg)
        ctx = _make_ctx(prices={
            TOKEN_YES_A: {"ask": 0.55},
            TOKEN_NO_A: {"ask": 0.55},
        })
        asyncio.run(s.on_book_update(
            _book_update(TOKEN_YES_A, SLUG_A), ctx
        ))

        assert s.get_slug_state(SLUG_A).phase.phase == "probe"

        # Prices that sum > 1.0 so profit is NOT locked
        s.on_fill_event(token_id=TOKEN_YES_A, fill_size=2.5, fill_price=0.55)
        assert s.get_slug_state(SLUG_A).phase.phase == "probe"

        s.on_fill_event(token_id=TOKEN_NO_A, fill_size=2.5, fill_price=0.55)
        assert s.get_slug_state(SLUG_A).phase.phase == "build"

    def test_fill_on_unknown_token_is_noop(self):
        s = GabagoolStrategy()
        s.on_fill_event(token_id="unknown_token", fill_size=10.0, fill_price=0.50)

    def test_profit_lock_detected(self):
        import asyncio
        cfg = GabagoolConfig(observation_ticks=3, trend_min_amplitude=0.05)
        s = GabagoolStrategy(config=cfg)
        ctx = _make_ctx(prices={
            TOKEN_YES_A: {"ask": 0.40},
            TOKEN_NO_A: {"ask": 0.55},
        })
        asyncio.run(s.on_book_update(
            _book_update(TOKEN_YES_A, SLUG_A), ctx
        ))

        s.on_fill_event(token_id=TOKEN_YES_A, fill_size=100.0, fill_price=0.40)
        s.on_fill_event(token_id=TOKEN_NO_A, fill_size=100.0, fill_price=0.55)

        state = s.get_slug_state(SLUG_A)
        assert state.pair.pair_cost == pytest.approx(0.95, abs=1e-9)
        assert state.pair.is_profit_locked is True
        assert state.phase.phase == "locked"


# ═══════════════════════════════════════════════════════════════════════════════
# Multi-slug state isolation
# ═══════════════════════════════════════════════════════════════════════════════


class TestMultiSlugIsolation:
    """Each slug gets independent PairState / TrendDetector / PhaseManager."""

    def test_separate_pair_states(self):
        import asyncio
        cfg = GabagoolConfig(observation_ticks=3, trend_min_amplitude=0.05)
        s = GabagoolStrategy(config=cfg)

        ctx = _make_ctx(
            slugs=[
                (SLUG_A, TOKEN_YES_A, TOKEN_NO_A, CONDITION_A),
                (SLUG_B, TOKEN_YES_B, TOKEN_NO_B, CONDITION_B),
            ],
            prices={
                TOKEN_YES_A: {"ask": 0.40},
                TOKEN_NO_A: {"ask": 0.55},
                TOKEN_YES_B: {"ask": 0.60},
                TOKEN_NO_B: {"ask": 0.35},
            },
        )

        asyncio.run(s.on_book_update(
            _book_update(TOKEN_YES_A, SLUG_A, best_ask=0.40, condition_id=CONDITION_A), ctx
        ))
        asyncio.run(s.on_book_update(
            _book_update(TOKEN_YES_B, SLUG_B, best_ask=0.60, condition_id=CONDITION_B), ctx
        ))

        s.on_fill_event(token_id=TOKEN_YES_A, fill_size=10.0, fill_price=0.40)

        state_a = s.get_slug_state(SLUG_A)
        state_b = s.get_slug_state(SLUG_B)

        assert state_a.pair.qty_yes == 10.0
        assert state_b.pair.qty_yes == 0.0

    def test_market_resolved_cleans_up_slug(self):
        import asyncio
        cfg = GabagoolConfig(observation_ticks=3, trend_min_amplitude=0.05)
        s = GabagoolStrategy(config=cfg)

        ctx = _make_ctx(
            slugs=[
                (SLUG_A, TOKEN_YES_A, TOKEN_NO_A, CONDITION_A),
                (SLUG_B, TOKEN_YES_B, TOKEN_NO_B, CONDITION_B),
            ],
            prices={
                TOKEN_YES_A: {"ask": 0.40},
                TOKEN_NO_A: {"ask": 0.55},
                TOKEN_YES_B: {"ask": 0.60},
                TOKEN_NO_B: {"ask": 0.35},
            },
        )

        asyncio.run(s.on_book_update(
            _book_update(TOKEN_YES_A, SLUG_A, best_ask=0.40), ctx
        ))
        asyncio.run(s.on_book_update(
            _book_update(TOKEN_YES_B, SLUG_B, best_ask=0.60, condition_id=CONDITION_B), ctx
        ))

        assert s.get_slug_state(SLUG_A) is not None
        assert s.get_slug_state(SLUG_B) is not None

        asyncio.run(s.on_market_resolved(
            MarketResolved(slug=SLUG_A, condition_id=CONDITION_A, winning_token_id=TOKEN_YES_A),
            ctx,
        ))

        assert s.get_slug_state(SLUG_A) is None
        assert s.get_slug_state(SLUG_B) is not None
        assert s.token_to_slug(TOKEN_YES_A) is None
        assert s.token_to_slug(TOKEN_YES_B) == SLUG_B


# ═══════════════════════════════════════════════════════════════════════════════
# Edge cases
# ═══════════════════════════════════════════════════════════════════════════════


class TestEdgeCases:
    def test_no_intent_when_only_one_price_available(self):
        """Missing NO price should not crash or produce intents."""
        import asyncio
        cfg = GabagoolConfig(observation_ticks=3, trend_min_amplitude=0.05)
        s = GabagoolStrategy(config=cfg)
        ctx = _make_ctx(prices={
            TOKEN_YES_A: {"ask": 0.50},
        })
        result = asyncio.run(s.on_book_update(
            _book_update(TOKEN_YES_A, SLUG_A, best_ask=0.50), ctx
        ))
        assert result is None

    def test_no_intent_when_profit_locked(self):
        """Once profit is locked, no more intents should be produced."""
        import asyncio
        cfg = GabagoolConfig(
            observation_ticks=3,
            trend_min_reversals=1,
            trend_min_amplitude=0.05,
        )
        s = GabagoolStrategy(config=cfg)

        prices = _oscillating_prices(20, center=0.50, amplitude=0.25)
        for yes_ask in prices:
            no_ask = max(0.01, min(0.99, 1.0 - yes_ask))
            ctx = _make_ctx(prices={
                TOKEN_YES_A: {"ask": yes_ask},
                TOKEN_NO_A: {"ask": no_ask},
            })
            asyncio.run(s.on_book_update(
                _book_update(TOKEN_YES_A, SLUG_A, best_ask=yes_ask), ctx
            ))

        # Lock profit manually
        s.on_fill_event(token_id=TOKEN_YES_A, fill_size=100.0, fill_price=0.40)
        s.on_fill_event(token_id=TOKEN_NO_A, fill_size=100.0, fill_price=0.55)

        state = s.get_slug_state(SLUG_A)
        assert state.pair.is_profit_locked

        ctx = _make_ctx(prices={
            TOKEN_YES_A: {"ask": 0.30},
            TOKEN_NO_A: {"ask": 0.65},
        })
        result = asyncio.run(s.on_book_update(
            _book_update(TOKEN_YES_A, SLUG_A, best_ask=0.30), ctx
        ))
        assert result is None

    def test_tick_size_change_initializes_state(self):
        import asyncio
        s = GabagoolStrategy()
        ctx = _make_ctx()
        asyncio.run(s.on_tick_size_change(
            TickSizeChange(
                condition_id=CONDITION_A,
                slug=SLUG_A,
                token_id=TOKEN_YES_A,
                old_tick_size="0.01",
                new_tick_size="0.001",
            ),
            ctx,
        ))
        assert s.get_slug_state(SLUG_A) is not None
