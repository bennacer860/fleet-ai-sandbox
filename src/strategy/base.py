"""Strategy abstract base class and context.

Every trading strategy implements this interface.  Strategies are pure:
they receive events and market context, and return ``OrderIntent``
objects (or None).  They never perform I/O or place orders directly.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import Any

from ..core.events import BookUpdate, MarketResolved, TickSizeChange
from ..core.models import OrderIntent, Position


@dataclass(slots=True)
class StrategyContext:
    """Read-only snapshot of system state provided to strategies on each event."""

    positions: dict[str, Position] = field(default_factory=dict)
    best_prices: dict[str, dict[str, float]] = field(default_factory=dict)
    eval_cache: dict[str, dict[str, Any]] = field(default_factory=dict)
    market_meta: dict[str, dict[str, Any]] = field(default_factory=dict)
    tick_sizes: dict[str, float] = field(default_factory=dict)
    dry_run: bool = False
    crypto_prices: dict[str, float] = field(default_factory=dict)
    crypto_price_ts: dict[str, float] = field(default_factory=dict)


class Strategy(ABC):
    """Base class for all trading strategies."""

    @abstractmethod
    def name(self) -> str:
        """Unique human-readable name for this strategy."""
        ...

    @abstractmethod
    async def on_tick_size_change(
        self, event: TickSizeChange, ctx: StrategyContext
    ) -> list[OrderIntent] | None:
        """React to a tick-size change event.

        Return a list of ``OrderIntent`` objects to place, or ``None``
        to do nothing.
        """
        ...

    @abstractmethod
    async def on_book_update(
        self, event: BookUpdate, ctx: StrategyContext
    ) -> list[OrderIntent] | None:
        """React to an order-book update.

        Return a list of ``OrderIntent`` objects to place, or ``None``
        to do nothing.
        """
        ...

    async def on_market_resolved(
        self, event: MarketResolved, ctx: StrategyContext
    ) -> None:
        """Called when a market resolves.  Default is a no-op."""

    async def poll(self, ctx: StrategyContext) -> list[OrderIntent] | None:
        """Called periodically by the bot's poll loop.

        Strategies that need to act on a timer (not just react to events)
        should override this.  Default is a no-op.
        """
        return None

    async def startup(self) -> None:
        """Called once during bot startup.  Override for initialisation."""

    async def shutdown(self) -> None:
        """Called once during bot shutdown.  Override for cleanup."""
