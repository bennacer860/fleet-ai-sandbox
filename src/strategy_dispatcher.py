"""Strategy dispatcher — bridges market events to strategies and intents to orders.

Receives market events, calls strategies with context, and submits returned
intents to the OrderManager. Notifies strategies of fill/terminal events.

This module handles the core dispatch logic. Display/notification side effects
are delegated to an optional DispatchObserver.
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any, Protocol, runtime_checkable

from .core.events import (
    BookUpdate,
    MarketResolved,
    OrderFill,
    OrderStatus,
    OrderTerminal,
    TickSizeChange,
)
from .core.models import OrderIntent, OrderState
from .logging_config import get_logger
from .markets.fifteen_min import extract_market_end_ts, extract_market_from_slug
from .strategy.base import Strategy, StrategyContext

if TYPE_CHECKING:
    from .execution.order_manager import OrderManager

logger = get_logger(__name__)


# ── Protocols ─────────────────────────────────────────────────────────────────


@runtime_checkable
class ContextSource(Protocol):
    """Provides data for building StrategyContext."""

    def get_context(self) -> StrategyContext:
        """Return a fresh StrategyContext snapshot."""
        ...


@runtime_checkable
class DispatchObserver(Protocol):
    """Observes dispatch outcomes for display/logging.

    All methods are optional no-ops by default. Implement only the ones you need.
    """

    async def on_strategy_skip(
        self,
        event: Any,
        strategy: Strategy,
        reason: str,
        ctx: StrategyContext,
    ) -> None:
        """Called when a strategy returns no intents."""
        ...

    async def on_intent_submitted(
        self,
        intent: OrderIntent,
        state: OrderState | None,
        strategy: Strategy,
        event: Any,
        ctx: StrategyContext,
    ) -> None:
        """Called after an intent is submitted to OrderManager."""
        ...

    async def on_market_resolved(
        self,
        event: MarketResolved,
    ) -> None:
        """Called when a market resolves."""
        ...


class NullObserver:
    """Default no-op observer."""

    async def on_strategy_skip(self, *args: Any, **kwargs: Any) -> None:
        pass

    async def on_intent_submitted(self, *args: Any, **kwargs: Any) -> None:
        pass

    async def on_market_resolved(self, *args: Any, **kwargs: Any) -> None:
        pass


# ── StrategyDispatcher ────────────────────────────────────────────────────────


@dataclass
class StrategyDispatcher:
    """Dispatches market events to strategies and submits intents.

    Core responsibilities:
    - Call strategies on market events (tick_size_change, book_update, market_resolved)
    - Submit returned intents to OrderManager
    - Notify strategies of order lifecycle events (fills, terminals)
    - Run the periodic poll loop

    Side effects (dashboard, telegram, persistence) are delegated to the observer.
    """

    strategies: list[Strategy]
    order_manager: "OrderManager"
    context_source: ContextSource
    observer: DispatchObserver = field(default_factory=NullObserver)

    # ── Market event handlers ─────────────────────────────────────────────────

    async def on_tick_size_change(self, event: TickSizeChange) -> None:
        """Dispatch a tick-size change to all strategies."""
        handler_start_ns = time.time_ns()
        ctx = self.context_source.get_context()
        ctx.tick_sizes[event.token_id] = float(event.new_tick_size)

        for strategy in self.strategies:
            try:
                intents = await strategy.on_tick_size_change(event, ctx)
                if intents:
                    await self._submit_intents(intents, event, handler_start_ns, strategy, ctx)
                else:
                    reason = getattr(strategy, "last_skip_reason", None) or "no signal"
                    await self.observer.on_strategy_skip(event, strategy, reason, ctx)
            except Exception:
                logger.exception("Strategy %s error on tick_size_change", strategy.name())

    async def on_book_update(self, event: BookUpdate) -> None:
        """Dispatch a book update to all strategies."""
        handler_start_ns = time.time_ns()
        ctx = self.context_source.get_context()

        for strategy in self.strategies:
            try:
                intents = await strategy.on_book_update(event, ctx)
                if intents:
                    await self._submit_intents(intents, event, handler_start_ns, strategy, ctx)
            except Exception:
                logger.exception("Strategy %s error on book_update", strategy.name())

    async def on_market_resolved(self, event: MarketResolved) -> None:
        """Notify all strategies that a market has resolved."""
        ctx = self.context_source.get_context()

        for strategy in self.strategies:
            try:
                await strategy.on_market_resolved(event, ctx)
            except Exception:
                logger.exception("Strategy %s error on market_resolved", strategy.name())

        await self.observer.on_market_resolved(event)

    async def poll(self) -> None:
        """Call poll() on all strategies (for timer-driven logic)."""
        ctx = self.context_source.get_context()

        for strategy in self.strategies:
            try:
                intents = await strategy.poll(ctx)
                if intents:
                    await self._submit_intents(intents, None, None, strategy, ctx)
            except Exception:
                logger.exception("Strategy %s error on poll", strategy.name())

    # ── Order lifecycle notifications ─────────────────────────────────────────

    async def on_fill(self, event: OrderFill, active_orders: dict[str, OrderState]) -> None:
        """Notify strategies when an order fills."""
        state = active_orders.get(event.order_id)
        if not state:
            return

        is_filled = state.status == OrderStatus.FILLED

        for strategy in self.strategies:
            if hasattr(strategy, "notify_order_result"):
                strategy.notify_order_result(state.intent.slug, filled=is_filled)
            if hasattr(strategy, "on_fill_event"):
                strategy.on_fill_event(
                    token_id=state.intent.token_id,
                    fill_size=event.fill_size,
                    fill_price=event.fill_price,
                )

    async def on_terminal(self, event: OrderTerminal, active_orders: dict[str, OrderState]) -> None:
        """Notify strategies when an order terminates."""
        state = active_orders.get(event.order_id)
        if not state:
            return

        is_filled = state.status == OrderStatus.FILLED

        for strategy in self.strategies:
            if hasattr(strategy, "notify_order_result"):
                strategy.notify_order_result(state.intent.slug, filled=is_filled)

    # ── Intent submission ─────────────────────────────────────────────────────

    async def _submit_intents(
        self,
        intents: list[OrderIntent],
        event: Any,
        handler_start_ns: int | None,
        strategy: Strategy,
        ctx: StrategyContext,
    ) -> None:
        """Submit intents to OrderManager and notify observer."""
        tick_event_ns = getattr(event, "timestamp_ns", None) if event else None

        for intent in intents:
            state = await self.order_manager.submit(intent)

            if state is not None:
                # Attach timing and metadata
                state.tick_event_ns = tick_event_ns
                state.handler_start_ns = handler_start_ns
                state.market_end_ts = extract_market_end_ts(intent.slug)
                state.market = extract_market_from_slug(intent.slug)
                state.submission_source = strategy.classify_submission(event) if event else "poll"

                bp = ctx.best_prices.get(intent.token_id, {})
                state.best_bid = bp.get("bid")
                state.best_ask = bp.get("ask")

                # Strategy-specific metadata (sweep proximity)
                state.spot_price = getattr(strategy, "last_spot_price", None)
                state.strike_price = getattr(strategy, "last_price_to_beat", None)
                state.proximity = getattr(strategy, "last_proximity", None)
                state.spot_price_age_ms = getattr(strategy, "last_price_age_ms", None)

                self.order_manager.re_persist(state)

            await self.observer.on_intent_submitted(intent, state, strategy, event, ctx)

            # Notify strategy of immediate result (for retry logic)
            if hasattr(strategy, "notify_order_result"):
                if state is None or state.is_terminal:
                    strategy.notify_order_result(intent.slug, filled=False)
                elif state.status == OrderStatus.FILLED:
                    strategy.notify_order_result(intent.slug, filled=True)
