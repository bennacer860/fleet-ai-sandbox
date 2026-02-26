"""Health monitoring — heartbeat file and event-loop lag detection."""

from __future__ import annotations

import asyncio
import json
import time
from pathlib import Path
from typing import Any

from ..logging_config import get_logger
from .metrics import Metrics

logger = get_logger(__name__)

DEFAULT_HEARTBEAT_PATH = "/tmp/polymarket_bot_heartbeat.json"
HEARTBEAT_INTERVAL_S = 30
LAG_CHECK_INTERVAL_S = 5


class HealthMonitor:
    """Writes periodic heartbeat file and monitors event-loop lag."""

    def __init__(
        self,
        heartbeat_path: str = DEFAULT_HEARTBEAT_PATH,
        context_fn: Any = None,
    ) -> None:
        self._heartbeat_path = Path(heartbeat_path)
        self._context_fn = context_fn
        self._start_time = time.monotonic()
        self._running = False
        self.event_loop_lag_ms: float = 0.0

    # ── Event loop lag monitor ────────────────────────────────────────────

    async def _lag_monitor(self) -> None:
        while self._running:
            t0 = time.monotonic()
            await asyncio.sleep(LAG_CHECK_INTERVAL_S)
            elapsed = time.monotonic() - t0
            self.event_loop_lag_ms = max(0, (elapsed - LAG_CHECK_INTERVAL_S) * 1000)

            metrics = Metrics.get()
            metrics.set("event_loop_lag_ms", self.event_loop_lag_ms)

            if self.event_loop_lag_ms > 100:
                logger.warning("[HEALTH] Event loop lag: %.0fms", self.event_loop_lag_ms)

    # ── Heartbeat writer ──────────────────────────────────────────────────

    async def _heartbeat_writer(self) -> None:
        while self._running:
            await asyncio.sleep(HEARTBEAT_INTERVAL_S)
            try:
                data: dict[str, Any] = {
                    "timestamp": time.time(),
                    "uptime_s": round(time.monotonic() - self._start_time, 1),
                    "event_loop_lag_ms": round(self.event_loop_lag_ms, 1),
                    "metrics": Metrics.get().snapshot(),
                }
                if self._context_fn:
                    data.update(self._context_fn())

                self._heartbeat_path.write_text(
                    json.dumps(data, indent=2, default=str)
                )
            except Exception:
                logger.exception("[HEALTH] Heartbeat write failed")

    # ── Lifecycle ─────────────────────────────────────────────────────────

    async def run(self) -> None:
        self._running = True
        lag_task = asyncio.create_task(self._lag_monitor())
        hb_task = asyncio.create_task(self._heartbeat_writer())
        try:
            await asyncio.gather(lag_task, hb_task)
        except asyncio.CancelledError:
            pass
        finally:
            self._running = False

    async def stop(self) -> None:
        self._running = False

    @staticmethod
    def read_heartbeat(path: str = DEFAULT_HEARTBEAT_PATH) -> dict[str, Any] | None:
        try:
            return json.loads(Path(path).read_text())
        except Exception:
            return None
