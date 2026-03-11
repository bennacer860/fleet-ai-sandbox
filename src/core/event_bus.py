"""Async event bus with Queue-backed dispatch.

Subscribers register handlers for specific event types.  Publishers
push events onto a shared queue.  A single drain loop dispatches each
event to all matching handlers via ``asyncio.create_task``.
"""

from __future__ import annotations

import asyncio
import functools
from collections import defaultdict
from typing import Any, Callable, Coroutine

from ..logging_config import get_logger

logger = get_logger(__name__)

Handler = Callable[..., Coroutine[Any, Any, None]]


class EventBus:
    """Central async pub-sub event bus."""

    def __init__(self, max_queue: int = 10_000) -> None:
        self._queue: asyncio.Queue[Any] = asyncio.Queue(maxsize=max_queue)
        self._subscribers: dict[type, list[Handler]] = defaultdict(list)
        self._running = False

    def subscribe(self, event_type: type, handler: Handler) -> None:
        """Register *handler* to be called on every event of *event_type*.

        If *handler* is a plain (non-async) callable it is automatically
        wrapped so callers can register synchronous callbacks without
        converting them.
        """
        if not asyncio.iscoroutinefunction(handler):
            sync_fn = handler

            @functools.wraps(sync_fn)
            async def _wrapper(event: Any) -> None:
                sync_fn(event)

            handler = _wrapper

        self._subscribers[event_type].append(handler)

    async def publish(self, event: Any) -> None:
        """Enqueue *event* for dispatch.  Non-blocking in practice because
        the queue is generously sized; blocks only under extreme back-pressure."""
        await self._queue.put(event)

    def publish_nowait(self, event: Any) -> None:
        """Fire-and-forget enqueue.  Drops the event if the queue is full."""
        try:
            self._queue.put_nowait(event)
        except asyncio.QueueFull:
            logger.warning(
                "Event bus queue full — dropping %s", type(event).__name__
            )

    async def run(self) -> None:
        """Drain loop: pull events and dispatch to subscribers."""
        self._running = True
        logger.info("[EVENT_BUS] Started")
        try:
            while self._running:
                event = await self._queue.get()
                handlers = self._subscribers.get(type(event), [])
                for handler in handlers:
                    asyncio.create_task(self._safe_dispatch(handler, event))
                self._queue.task_done()
        except asyncio.CancelledError:
            pass
        finally:
            logger.info("[EVENT_BUS] Stopped")

    async def _safe_dispatch(self, handler: Handler, event: Any) -> None:
        try:
            await handler(event)
        except Exception:
            logger.exception(
                "Handler %s raised for %s",
                getattr(handler, "__qualname__", handler),
                type(event).__name__,
            )

    async def stop(self) -> None:
        self._running = False

    @property
    def pending(self) -> int:
        return self._queue.qsize()
