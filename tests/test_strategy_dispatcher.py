"""Unit tests for StrategyDispatcher.

Tests the dispatch logic without any I/O dependencies.
"""

import asyncio
from dataclasses import dataclass, field
from typing import Any
from unittest.mock import MagicMock

import pytest

from src.core.events import (
    BookUpdate,
    MarketResolved,
    OrderFill,
    OrderStatus,
    OrderTerminal,
    TickSizeChange,
)
from src.core.models import OrderIntent, OrderState, Side
from src.strategy.base import Strategy, StrategyContext
from src.strategy_dispatcher import (
    ContextSource,
    DispatchObserver,
    NullObserver,
    StrategyDispatcher,
)


# ── Test fixtures ─────────────────────────────────────────────────────────────


class MockContextSource:
    """Simple context source that returns a fixed context."""

    def __init__(self, ctx: StrategyContext | None = None):
        self._ctx = ctx or StrategyContext()

    def get_context(self) -> StrategyContext:
        return self._ctx


class MockStrategy(Strategy):
    """Controllable mock strategy for testing."""

    def __init__(
        self,
        name: str = "mock",
        tick_intents: list[OrderIntent] | None = None,
        book_intents: list[OrderIntent] | None = None,
        poll_intents: list[OrderIntent] | None = None,
    ):
        self._name = name
        self._tick_intents = tick_intents
        self._book_intents = book_intents
        self._poll_intents = poll_intents
        self.last_skip_reason: str | None = None
        self.tick_calls: list[TickSizeChange] = []
        self.book_calls: list[BookUpdate] = []
        self.resolved_calls: list[MarketResolved] = []
        self.poll_calls: list[StrategyContext] = []
        self.notify_results: list[tuple[str, bool]] = []
        self.fill_events: list[tuple[str, float, float]] = []

    def name(self) -> str:
        return self._name

    async def on_tick_size_change(
        self, event: TickSizeChange, ctx: StrategyContext
    ) -> list[OrderIntent] | None:
        self.tick_calls.append(event)
        return self._tick_intents

    async def on_book_update(
        self, event: BookUpdate, ctx: StrategyContext
    ) -> list[OrderIntent] | None:
        self.book_calls.append(event)
        return self._book_intents

    async def on_market_resolved(
        self, event: MarketResolved, ctx: StrategyContext
    ) -> None:
        self.resolved_calls.append(event)

    async def poll(self, ctx: StrategyContext) -> list[OrderIntent] | None:
        self.poll_calls.append(ctx)
        return self._poll_intents

    def notify_order_result(self, slug: str, filled: bool) -> None:
        self.notify_results.append((slug, filled))

    def on_fill_event(self, token_id: str, fill_size: float, fill_price: float) -> None:
        self.fill_events.append((token_id, fill_size, fill_price))


@dataclass
class MockOrderManager:
    """Mock order manager for testing."""

    submitted_intents: list[OrderIntent] = field(default_factory=list)
    return_state: OrderState | None = None

    async def submit(self, intent: OrderIntent) -> OrderState | None:
        self.submitted_intents.append(intent)
        return self.return_state

    def re_persist(self, state: OrderState) -> None:
        pass


@dataclass
class MockObserver:
    """Capture observer calls for assertions."""

    skip_calls: list[tuple[Any, Strategy, str]] = field(default_factory=list)
    submit_calls: list[tuple[OrderIntent, OrderState | None, Strategy]] = field(default_factory=list)
    resolved_calls: list[MarketResolved] = field(default_factory=list)

    async def on_strategy_skip(
        self,
        event: Any,
        strategy: Strategy,
        reason: str,
        ctx: StrategyContext,
    ) -> None:
        self.skip_calls.append((event, strategy, reason))

    async def on_intent_submitted(
        self,
        intent: OrderIntent,
        state: OrderState | None,
        strategy: Strategy,
        event: Any,
        ctx: StrategyContext,
    ) -> None:
        self.submit_calls.append((intent, state, strategy))

    async def on_market_resolved(self, event: MarketResolved) -> None:
        self.resolved_calls.append(event)


def make_tick_event(slug: str = "test-slug", token_id: str = "tok123") -> TickSizeChange:
    return TickSizeChange(
        condition_id="cond",
        slug=slug,
        token_id=token_id,
        old_tick_size="0.01",
        new_tick_size="0.001",
    )


def make_book_event(slug: str = "test-slug", token_id: str = "tok123") -> BookUpdate:
    return BookUpdate(
        token_id=token_id,
        condition_id="cond",
        slug=slug,
        bids=[],
        asks=[],
        best_bid=0.50,
        best_ask=0.51,
    )


def make_intent(slug: str = "test-slug", token_id: str = "tok123") -> OrderIntent:
    return OrderIntent(
        token_id=token_id,
        price=0.95,
        size=10.0,
        side=Side.BUY,
        strategy="mock",
        slug=slug,
        tick_size=0.001,
    )


# ── Tests ─────────────────────────────────────────────────────────────────────


class TestTickSizeDispatch:
    """Tests for on_tick_size_change dispatch."""

    def test_dispatches_to_strategy(self):
        """Tick event should be dispatched to strategy."""
        strategy = MockStrategy()
        dispatcher = StrategyDispatcher(
            strategies=[strategy],
            order_manager=MockOrderManager(),
            context_source=MockContextSource(),
        )

        event = make_tick_event()
        asyncio.run(dispatcher.on_tick_size_change(event))

        assert len(strategy.tick_calls) == 1
        assert strategy.tick_calls[0] == event

    def test_submits_returned_intents(self):
        """Intents returned by strategy should be submitted."""
        intent = make_intent()
        strategy = MockStrategy(tick_intents=[intent])
        order_manager = MockOrderManager()
        dispatcher = StrategyDispatcher(
            strategies=[strategy],
            order_manager=order_manager,
            context_source=MockContextSource(),
        )

        asyncio.run(dispatcher.on_tick_size_change(make_tick_event()))

        assert len(order_manager.submitted_intents) == 1
        assert order_manager.submitted_intents[0] == intent

    def test_notifies_observer_on_skip(self):
        """Observer should be notified when strategy returns no intents."""
        strategy = MockStrategy(tick_intents=None)
        strategy.last_skip_reason = "price too low"
        observer = MockObserver()
        dispatcher = StrategyDispatcher(
            strategies=[strategy],
            order_manager=MockOrderManager(),
            context_source=MockContextSource(),
            observer=observer,
        )

        event = make_tick_event()
        asyncio.run(dispatcher.on_tick_size_change(event))

        assert len(observer.skip_calls) == 1
        assert observer.skip_calls[0][1] == strategy
        assert observer.skip_calls[0][2] == "price too low"

    def test_notifies_observer_on_submit(self):
        """Observer should be notified when intent is submitted."""
        intent = make_intent()
        strategy = MockStrategy(tick_intents=[intent])
        observer = MockObserver()
        dispatcher = StrategyDispatcher(
            strategies=[strategy],
            order_manager=MockOrderManager(),
            context_source=MockContextSource(),
            observer=observer,
        )

        asyncio.run(dispatcher.on_tick_size_change(make_tick_event()))

        assert len(observer.submit_calls) == 1
        assert observer.submit_calls[0][0] == intent

    def test_updates_tick_size_in_context(self):
        """Context should have tick size updated from event."""
        captured_ctx = []

        class CapturingStrategy(MockStrategy):
            async def on_tick_size_change(self, event, ctx):
                captured_ctx.append(dict(ctx.tick_sizes))
                return None

        strategy = CapturingStrategy()
        dispatcher = StrategyDispatcher(
            strategies=[strategy],
            order_manager=MockOrderManager(),
            context_source=MockContextSource(),
        )

        event = make_tick_event(token_id="tok_abc")
        asyncio.run(dispatcher.on_tick_size_change(event))

        assert captured_ctx[0]["tok_abc"] == 0.001


class TestBookUpdateDispatch:
    """Tests for on_book_update dispatch."""

    def test_dispatches_to_strategy(self):
        """Book event should be dispatched to strategy."""
        strategy = MockStrategy()
        dispatcher = StrategyDispatcher(
            strategies=[strategy],
            order_manager=MockOrderManager(),
            context_source=MockContextSource(),
        )

        event = make_book_event()
        asyncio.run(dispatcher.on_book_update(event))

        assert len(strategy.book_calls) == 1
        assert strategy.book_calls[0] == event

    def test_submits_returned_intents(self):
        """Intents from book update should be submitted."""
        intent = make_intent()
        strategy = MockStrategy(book_intents=[intent])
        order_manager = MockOrderManager()
        dispatcher = StrategyDispatcher(
            strategies=[strategy],
            order_manager=order_manager,
            context_source=MockContextSource(),
        )

        asyncio.run(dispatcher.on_book_update(make_book_event()))

        assert len(order_manager.submitted_intents) == 1


class TestMarketResolvedDispatch:
    """Tests for on_market_resolved dispatch."""

    def test_dispatches_to_strategy(self):
        """Resolved event should be dispatched to strategy."""
        strategy = MockStrategy()
        dispatcher = StrategyDispatcher(
            strategies=[strategy],
            order_manager=MockOrderManager(),
            context_source=MockContextSource(),
        )

        event = MarketResolved(slug="test", condition_id="cond", winning_token_id="tok")
        asyncio.run(dispatcher.on_market_resolved(event))

        assert len(strategy.resolved_calls) == 1
        assert strategy.resolved_calls[0] == event

    def test_notifies_observer(self):
        """Observer should be notified of market resolution."""
        observer = MockObserver()
        dispatcher = StrategyDispatcher(
            strategies=[MockStrategy()],
            order_manager=MockOrderManager(),
            context_source=MockContextSource(),
            observer=observer,
        )

        event = MarketResolved(slug="test", condition_id="cond", winning_token_id="tok")
        asyncio.run(dispatcher.on_market_resolved(event))

        assert len(observer.resolved_calls) == 1


class TestPollDispatch:
    """Tests for poll() dispatch."""

    def test_polls_all_strategies(self):
        """Poll should call poll() on all strategies."""
        strategy = MockStrategy()
        dispatcher = StrategyDispatcher(
            strategies=[strategy],
            order_manager=MockOrderManager(),
            context_source=MockContextSource(),
        )

        asyncio.run(dispatcher.poll())

        assert len(strategy.poll_calls) == 1

    def test_submits_poll_intents(self):
        """Intents from poll should be submitted."""
        intent = make_intent()
        strategy = MockStrategy(poll_intents=[intent])
        order_manager = MockOrderManager()
        dispatcher = StrategyDispatcher(
            strategies=[strategy],
            order_manager=order_manager,
            context_source=MockContextSource(),
        )

        asyncio.run(dispatcher.poll())

        assert len(order_manager.submitted_intents) == 1


class TestFillNotification:
    """Tests for order fill notification to strategies."""

    def test_notifies_strategy_on_fill(self):
        """Strategy should be notified of fills."""
        strategy = MockStrategy()
        dispatcher = StrategyDispatcher(
            strategies=[strategy],
            order_manager=MockOrderManager(),
            context_source=MockContextSource(),
        )

        intent = make_intent(slug="test-slug", token_id="tok123")
        state = OrderState(
            order_id="ord1",
            intent=intent,
            status=OrderStatus.FILLED,
        )

        event = OrderFill(order_id="ord1", fill_price=0.95, fill_size=10.0, status=OrderStatus.FILLED)
        asyncio.run(dispatcher.on_fill(event, {"ord1": state}))

        assert len(strategy.notify_results) == 1
        assert strategy.notify_results[0] == ("test-slug", True)

    def test_calls_on_fill_event(self):
        """Strategy's on_fill_event should be called."""
        strategy = MockStrategy()
        dispatcher = StrategyDispatcher(
            strategies=[strategy],
            order_manager=MockOrderManager(),
            context_source=MockContextSource(),
        )

        intent = make_intent(token_id="tok_abc")
        state = OrderState(order_id="ord1", intent=intent, status=OrderStatus.PARTIAL)

        event = OrderFill(order_id="ord1", fill_price=0.95, fill_size=5.0, status=OrderStatus.PARTIAL)
        asyncio.run(dispatcher.on_fill(event, {"ord1": state}))

        assert len(strategy.fill_events) == 1
        assert strategy.fill_events[0] == ("tok_abc", 5.0, 0.95)


class TestTerminalNotification:
    """Tests for order terminal notification to strategies."""

    def test_notifies_strategy_on_terminal(self):
        """Strategy should be notified of terminal orders."""
        strategy = MockStrategy()
        dispatcher = StrategyDispatcher(
            strategies=[strategy],
            order_manager=MockOrderManager(),
            context_source=MockContextSource(),
        )

        intent = make_intent(slug="test-slug")
        state = OrderState(order_id="ord1", intent=intent, status=OrderStatus.CANCELLED)

        event = OrderTerminal(order_id="ord1", status=OrderStatus.CANCELLED, reason="user cancelled")
        asyncio.run(dispatcher.on_terminal(event, {"ord1": state}))

        assert len(strategy.notify_results) == 1
        assert strategy.notify_results[0] == ("test-slug", False)


class TestMultipleStrategies:
    """Tests for dispatching to multiple strategies."""

    def test_dispatches_to_all_strategies(self):
        """Events should be dispatched to all strategies."""
        s1 = MockStrategy(name="s1")
        s2 = MockStrategy(name="s2")
        dispatcher = StrategyDispatcher(
            strategies=[s1, s2],
            order_manager=MockOrderManager(),
            context_source=MockContextSource(),
        )

        asyncio.run(dispatcher.on_tick_size_change(make_tick_event()))

        assert len(s1.tick_calls) == 1
        assert len(s2.tick_calls) == 1

    def test_submits_intents_from_all_strategies(self):
        """Intents from all strategies should be submitted."""
        i1 = make_intent(token_id="tok1")
        i2 = make_intent(token_id="tok2")
        s1 = MockStrategy(name="s1", tick_intents=[i1])
        s2 = MockStrategy(name="s2", tick_intents=[i2])
        order_manager = MockOrderManager()
        dispatcher = StrategyDispatcher(
            strategies=[s1, s2],
            order_manager=order_manager,
            context_source=MockContextSource(),
        )

        asyncio.run(dispatcher.on_tick_size_change(make_tick_event()))

        assert len(order_manager.submitted_intents) == 2


class TestNullObserver:
    """Tests for NullObserver default."""

    def test_null_observer_is_noop(self):
        """NullObserver methods should not raise."""
        async def run_test():
            observer = NullObserver()
            await observer.on_strategy_skip(None, MagicMock(), "reason", StrategyContext())
            await observer.on_intent_submitted(MagicMock(), None, MagicMock(), None, StrategyContext())
            await observer.on_market_resolved(MagicMock())

        asyncio.run(run_test())
