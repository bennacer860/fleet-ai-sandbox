"""WebSocket connection pool for Polymarket market data.

Runs N parallel MarketWebSocket connections to the same endpoint, deduplicates
events, and publishes only the first-seen copy of each event to the EventBus.
This dramatically reduces data gaps during reconnects — if one connection is
down, the others continue serving events.

Based on research showing:
- Single connection: ~7-8s gaps every ~30 minutes, ~0.23% event miss rate
- 5 connections: gaps collapse to <100ms, miss rate drops to <0.05%
"""

from __future__ import annotations

import asyncio
import hashlib
import os
import time
from collections import OrderedDict
from dataclasses import dataclass, field
from typing import Any

from ..core.event_bus import EventBus
from ..core.events import BookUpdate, LastTradePrice, MarketMeta, MarketResolved, TickSizeChange
from ..logging_config import get_logger
from .market_ws import MarketWebSocket

logger = get_logger(__name__)

POOL_SIZE = int(os.environ.get("MARKET_WS_POOL_SIZE", "1"))
DEDUP_CACHE_SIZE = int(os.environ.get("MARKET_WS_DEDUP_CACHE_SIZE", "10000"))
DEDUP_TTL_S = float(os.environ.get("MARKET_WS_DEDUP_TTL_S", "5.0"))


@dataclass
class PoolMetrics:
    """Metrics for the WebSocket pool."""
    events_received: dict[int, int] = field(default_factory=dict)
    first_seen_wins: dict[int, int] = field(default_factory=dict)
    duplicates_dropped: int = 0
    total_events: int = 0

    def record_event(self, conn_idx: int, is_first: bool) -> None:
        self.events_received[conn_idx] = self.events_received.get(conn_idx, 0) + 1
        if is_first:
            self.first_seen_wins[conn_idx] = self.first_seen_wins.get(conn_idx, 0) + 1
            self.total_events += 1
        else:
            self.duplicates_dropped += 1


class LRUDedup:
    """LRU cache for event deduplication with TTL."""

    def __init__(self, max_size: int = DEDUP_CACHE_SIZE, ttl_s: float = DEDUP_TTL_S) -> None:
        self._cache: OrderedDict[str, float] = OrderedDict()
        self._max_size = max_size
        self._ttl_s = ttl_s

    def is_duplicate(self, key: str) -> bool:
        """Check if key was seen recently. If not, mark it as seen."""
        now = time.monotonic()

        if key in self._cache:
            ts = self._cache[key]
            if now - ts < self._ttl_s:
                return True
            self._cache.move_to_end(key)
            self._cache[key] = now
            return False

        self._cache[key] = now
        while len(self._cache) > self._max_size:
            self._cache.popitem(last=False)

        return False


def _event_dedup_key(event: Any) -> str | None:
    """Generate a deduplication key for an event.

    Returns None for events that shouldn't be deduplicated (e.g., high-frequency
    book updates where we want the freshest data).
    """
    if isinstance(event, TickSizeChange):
        return f"tick:{event.slug}:{event.new_tick_size}"

    if isinstance(event, MarketResolved):
        return f"resolved:{event.slug}:{event.winning_token_id}"

    if isinstance(event, MarketMeta):
        return f"meta:{event.slug}:{event.condition_id}"

    if isinstance(event, LastTradePrice):
        h = hashlib.md5(
            f"{event.token_id}:{event.price}:{event.size}:{event.side}".encode(),
            usedforsecurity=False,
        ).hexdigest()[:12]
        return f"trade:{h}"

    if isinstance(event, BookUpdate):
        return f"book:{event.token_id}:{event.best_bid}:{event.best_ask}"

    return None


class DedupEventBus:
    """Wrapper around EventBus that deduplicates events from multiple sources."""

    def __init__(self, inner: EventBus, conn_idx: int, dedup: LRUDedup, metrics: PoolMetrics) -> None:
        self._inner = inner
        self._conn_idx = conn_idx
        self._dedup = dedup
        self._metrics = metrics

    async def publish(self, event: Any) -> None:
        key = _event_dedup_key(event)
        if key is not None:
            is_dup = self._dedup.is_duplicate(key)
            self._metrics.record_event(self._conn_idx, is_first=not is_dup)
            if is_dup:
                return
        await self._inner.publish(event)

    def publish_nowait(self, event: Any) -> None:
        key = _event_dedup_key(event)
        if key is not None:
            is_dup = self._dedup.is_duplicate(key)
            self._metrics.record_event(self._conn_idx, is_first=not is_dup)
            if is_dup:
                return
        self._inner.publish_nowait(event)

    def subscribe(self, event_type: type, handler: Any) -> None:
        self._inner.subscribe(event_type, handler)


class MarketWebSocketPool:
    """Pool of parallel MarketWebSocket connections with deduplication.

    Presents the same interface as MarketWebSocket so it can be a drop-in
    replacement in Bot. Internally spawns N connections and merges their
    state.
    """

    def __init__(
        self,
        event_bus: EventBus,
        initial_slugs: list[str] | None = None,
        ws_url: str | None = None,
        check_interval: int = 60,
        book_event_filter: set[str] | None = None,
        pool_size: int | None = None,
    ) -> None:
        self._event_bus = event_bus
        self._initial_slugs = initial_slugs or []
        self._ws_url = ws_url
        self._check_interval = check_interval
        self._book_event_filter = book_event_filter
        self._pool_size = pool_size if pool_size is not None else POOL_SIZE

        self._dedup = LRUDedup()
        self._metrics = PoolMetrics()
        self._connections: list[MarketWebSocket] = []
        self._tasks: list[asyncio.Task] = []
        self._running = False

        self._primary_idx = 0

        for i in range(self._pool_size):
            dedup_bus = DedupEventBus(event_bus, i, self._dedup, self._metrics)
            conn = MarketWebSocket(
                event_bus=dedup_bus,  # type: ignore[arg-type]
                initial_slugs=list(self._initial_slugs),
                ws_url=self._ws_url,
                check_interval=self._check_interval,
                book_event_filter=self._book_event_filter,
            )
            self._connections.append(conn)

        logger.info(
            "[WS_POOL] Initialized with %d connections (dedup_cache=%d, ttl=%.1fs)",
            self._pool_size,
            DEDUP_CACHE_SIZE,
            DEDUP_TTL_S,
        )

    @property
    def pool_size(self) -> int:
        return self._pool_size

    @property
    def metrics(self) -> PoolMetrics:
        return self._metrics

    @property
    def connected(self) -> bool:
        return any(c.connected for c in self._connections)

    @property
    def connections_up(self) -> int:
        return sum(1 for c in self._connections if c.connected)

    @property
    def message_count(self) -> int:
        return sum(c.message_count for c in self._connections)

    @property
    def reconnect_count(self) -> int:
        return sum(c.reconnect_count for c in self._connections)

    @property
    def resubscribe_count(self) -> int:
        return sum(c.resubscribe_count for c in self._connections)

    @property
    def keepalive_count(self) -> int:
        return sum(c.keepalive_count for c in self._connections)

    @property
    def last_message_age_s(self) -> float:
        ages = [c.last_message_age_s for c in self._connections if c.last_message_age_s >= 0]
        return min(ages) if ages else -1

    @property
    def last_data_message_age_s(self) -> float:
        ages = [c.last_data_message_age_s for c in self._connections if c.last_data_message_age_s >= 0]
        return min(ages) if ages else -1

    def channel_message_ages_s(self) -> dict[str, float]:
        """Per-channel message age — minimum across all connections."""
        merged: dict[str, float] = {}
        for conn in self._connections:
            for channel, age in conn.channel_message_ages_s().items():
                if age < 0:
                    continue
                if channel not in merged or age < merged[channel]:
                    merged[channel] = age
        return merged

    @property
    def token_ids(self) -> dict[str, list[str]]:
        return self._primary.token_ids

    @property
    def slug_by_token(self) -> dict[str, str]:
        return self._primary.slug_by_token

    @property
    def market_active(self) -> dict[str, bool]:
        return self._primary.market_active

    @property
    def condition_ids(self) -> dict[str, str]:
        return self._primary.condition_ids

    @property
    def condition_by_token(self) -> dict[str, str]:
        return self._primary.condition_by_token

    @property
    def token_outcomes(self) -> dict[str, str]:
        return self._primary.token_outcomes

    @property
    def best_prices(self) -> dict[str, dict[str, float]]:
        merged: dict[str, dict[str, float]] = {}
        for conn in self._connections:
            for token_id, prices in conn.best_prices.items():
                if token_id not in merged:
                    merged[token_id] = prices
        return merged

    @property
    def order_books(self) -> dict[str, dict[str, tuple[tuple[float, float], ...]]]:
        return self._primary.order_books

    @property
    def last_trade_prices(self) -> list[dict[str, Any]]:
        return self._primary.last_trade_prices

    @property
    def _books_filtered(self) -> int:
        return sum(c._books_filtered for c in self._connections)

    @property
    def _books_processed(self) -> int:
        return sum(c._books_processed for c in self._connections)

    @property
    def _primary(self) -> MarketWebSocket:
        """The primary connection used for shared state lookups."""
        return self._connections[self._primary_idx]

    def get_realtime_price(self, asset_id: str) -> float | None:
        for conn in self._connections:
            price = conn.get_realtime_price(asset_id)
            if price is not None:
                return price
        return None

    async def add_markets(self, slugs: list[str]) -> None:
        """Add markets to all connections in the pool."""
        await asyncio.gather(*(conn.add_markets(slugs) for conn in self._connections))

    async def remove_markets(self, slugs: list[str]) -> None:
        """Remove markets from all connections in the pool."""
        await asyncio.gather(*(conn.remove_markets(slugs) for conn in self._connections))

    async def run(self) -> None:
        """Start all connections in the pool."""
        self._running = True

        for i, conn in enumerate(self._connections):
            task = asyncio.create_task(conn.run(), name=f"market_ws_pool_{i}")
            self._tasks.append(task)

        logger.info("[WS_POOL] Started %d connections", len(self._tasks))

        stats_task = asyncio.create_task(self._log_stats_loop())

        try:
            await asyncio.gather(*self._tasks, return_exceptions=True)
        finally:
            stats_task.cancel()
            try:
                await stats_task
            except asyncio.CancelledError:
                pass

    async def _log_stats_loop(self) -> None:
        """Periodically log pool statistics."""
        while self._running:
            await asyncio.sleep(300)
            m = self._metrics
            total_recv = sum(m.events_received.values())
            if total_recv == 0:
                continue

            win_pcts = {
                i: (wins / total_recv * 100) if total_recv > 0 else 0
                for i, wins in m.first_seen_wins.items()
            }
            win_str = " ".join(f"#{i}:{pct:.0f}%" for i, pct in sorted(win_pcts.items()))

            logger.info(
                "[WS_POOL] conns=%d/%d | events=%d | dupes=%d (%.1f%%) | wins: %s",
                self.connections_up,
                self._pool_size,
                m.total_events,
                m.duplicates_dropped,
                (m.duplicates_dropped / total_recv * 100) if total_recv > 0 else 0,
                win_str,
            )

    async def stop(self) -> None:
        """Stop all connections in the pool."""
        self._running = False
        await asyncio.gather(*(conn.stop() for conn in self._connections))
        for task in self._tasks:
            task.cancel()
        await asyncio.gather(*self._tasks, return_exceptions=True)
        self._tasks.clear()
        logger.info("[WS_POOL] Stopped")
