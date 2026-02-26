"""Async write-behind persistence layer.

Hot-path components call ``enqueue()`` (sub-microsecond) instead of
performing direct I/O.  A background drain loop batches writes into
single SQLite transactions for efficiency.
"""

from __future__ import annotations

import asyncio
import sqlite3
import time
from dataclasses import dataclass
from typing import Any

from ..logging_config import get_logger

logger = get_logger(__name__)


@dataclass(slots=True)
class WriteOp:
    """A single pending database write."""

    sql: str
    params: tuple[Any, ...] | dict[str, Any]


class AsyncPersistence:
    """Queue-backed async persistence with batch drain."""

    def __init__(self, conn: sqlite3.Connection, flush_interval: float = 0.1) -> None:
        self._conn = conn
        self._queue: asyncio.Queue[WriteOp] = asyncio.Queue()
        self._flush_interval = flush_interval
        self._running = False
        self._total_writes = 0

    def enqueue(self, sql: str, params: tuple[Any, ...] | dict[str, Any] = ()) -> None:
        """Non-blocking enqueue — safe to call from the hot path."""
        try:
            self._queue.put_nowait(WriteOp(sql=sql, params=params))
        except asyncio.QueueFull:
            logger.warning("[PERSISTENCE] Queue full — dropping write")

    async def drain_loop(self) -> None:
        """Background task: batch-drain the queue into SQLite transactions."""
        self._running = True
        logger.info("[PERSISTENCE] Drain loop started (interval=%.2fs)", self._flush_interval)
        try:
            while self._running:
                await asyncio.sleep(self._flush_interval)
                await self._flush()
        except asyncio.CancelledError:
            await self._flush()
            logger.info("[PERSISTENCE] Final flush complete")
        finally:
            self._running = False
            logger.info(
                "[PERSISTENCE] Drain loop stopped (total writes: %d)",
                self._total_writes,
            )

    async def _flush(self) -> None:
        if self._queue.empty():
            return

        batch: list[WriteOp] = []
        while not self._queue.empty():
            try:
                batch.append(self._queue.get_nowait())
            except asyncio.QueueEmpty:
                break

        if not batch:
            return

        try:
            cursor = self._conn.cursor()
            cursor.execute("BEGIN")
            for op in batch:
                cursor.execute(op.sql, op.params)
            self._conn.commit()
            self._total_writes += len(batch)
        except sqlite3.Error:
            self._conn.rollback()
            logger.exception("[PERSISTENCE] Batch write failed (%d ops)", len(batch))

    async def stop(self) -> None:
        self._running = False
        await self._flush()

    @property
    def pending(self) -> int:
        return self._queue.qsize()

    @property
    def total_writes(self) -> int:
        return self._total_writes
