"""Binance miniTicker WebSocket — streams real-time crypto spot prices.

Maintains a ``latest_prices`` dict that callers can read at any time
(no EventBus needed).  Tracks per-asset update timestamps so consumers
can detect stale data.
"""

from __future__ import annotations

import asyncio
import time
from typing import Optional

import orjson
import websockets

from ..logging_config import get_logger

logger = get_logger(__name__)

_BASE_BACKOFF = 5
_MAX_BACKOFF = 60
# miniTicker fires ~every 1s per symbol; with 4 assets, 30s of silence is
# clearly abnormal and means the connection is stale or half-open.
_HEARTBEAT_TIMEOUT_S = 30

_BINANCE_WS = "wss://stream.binance.com:9443/stream"

_SYMBOL_TO_ASSET: dict[str, str] = {
    "BTCUSDT": "BTC",
    "ETHUSDT": "ETH",
    "SOLUSDT": "SOL",
    "XRPUSDT": "XRP",
}

_ASSET_TO_SYMBOL: dict[str, str] = {v: k for k, v in _SYMBOL_TO_ASSET.items()}


class CryptoWebSocket:
    """Streams Binance miniTicker prices for a set of crypto assets."""

    def __init__(self, assets: list[str] | None = None) -> None:
        self._assets = [a.upper() for a in (assets or list(_ASSET_TO_SYMBOL))]
        self._streams = [
            f"{_ASSET_TO_SYMBOL[a].lower()}@miniTicker"
            for a in self._assets
            if a in _ASSET_TO_SYMBOL
        ]
        self._ws_url = f"{_BINANCE_WS}?streams={'/'.join(self._streams)}"

        self.latest_prices: dict[str, float] = {}
        self.last_update_ts: dict[str, float] = {}

        self._websocket: Optional[websockets.WebSocketClientProtocol] = None
        self._running = False
        self._last_message_time: float = 0.0
        self._msg_count = 0
        self._reconnect_count = 0

    # ── Public read-only state ────────────────────────────────────────────

    @property
    def connected(self) -> bool:
        return self._websocket is not None

    @property
    def message_count(self) -> int:
        return self._msg_count

    @property
    def reconnect_count(self) -> int:
        return self._reconnect_count

    @property
    def last_message_age_s(self) -> float:
        if self._last_message_time == 0:
            return -1
        return time.monotonic() - self._last_message_time

    def get_price_age_ms(self, asset: str) -> float | None:
        """Milliseconds since the last price update for *asset*, or None."""
        ts = self.last_update_ts.get(asset.upper())
        if ts is None:
            return None
        return (time.monotonic() - ts) * 1000

    # ── Heartbeat watchdog ─────────────────────────────────────────────────
    # Binance miniTicker is high-frequency; prolonged silence suggests a
    # half-open or stale connection. Forcing close triggers reconnect.

    async def _heartbeat_watchdog(self) -> None:
        while self._running:
            await asyncio.sleep(_HEARTBEAT_TIMEOUT_S / 2)
            if self._last_message_time == 0:
                continue
            silence = time.monotonic() - self._last_message_time
            if silence > _HEARTBEAT_TIMEOUT_S and self._websocket:
                logger.warning(
                    "[WS_CRYPTO] No message for %.0fs — forcing reconnect", silence
                )
                try:
                    await self._websocket.close()
                except Exception:
                    pass

    # ── Main run loop ─────────────────────────────────────────────────────
    # ping_interval=20 sends a ping every 20s.  ping_timeout=20 closes the
    # socket if pong isn't received within 20s (was 60 — far too generous).

    async def run(self) -> None:
        if not self._streams:
            logger.warning("[WS_CRYPTO] No streams configured — not starting")
            return

        self._running = True
        backoff = _BASE_BACKOFF
        watchdog_task: asyncio.Task | None = None

        try:
            while self._running:
                try:
                    async with websockets.connect(
                        self._ws_url, ping_interval=20, ping_timeout=20
                    ) as ws:
                        self._websocket = ws
                        backoff = _BASE_BACKOFF
                        if watchdog_task is None:
                            watchdog_task = asyncio.create_task(self._heartbeat_watchdog())
                        logger.info(
                            "[WS_CRYPTO] Connected to Binance (%d streams: %s)",
                            len(self._streams),
                            ", ".join(self._assets),
                        )

                        try:
                            async for raw in ws:
                                if not self._running:
                                    break
                                self._last_message_time = time.monotonic()
                                self._msg_count += 1
                                if raw == "INVALID OPERATION":
                                    continue
                                self._process_message(raw)
                        finally:
                            self._websocket = None

                except (
                    websockets.exceptions.ConnectionClosed,
                    websockets.exceptions.WebSocketException,
                ) as e:
                    self._reconnect_count += 1
                    logger.warning(
                        "[WS_CRYPTO] Disconnected: %s — reconnect #%d in %ds",
                        e, self._reconnect_count, backoff,
                    )
                    if self._running:
                        await asyncio.sleep(backoff)
                        backoff = min(backoff * 2, _MAX_BACKOFF)

                except Exception as e:
                    self._reconnect_count += 1
                    logger.error(
                        "[WS_CRYPTO] Unexpected error: %s — reconnect #%d in %ds",
                        e, self._reconnect_count, backoff,
                    )
                    if self._running:
                        await asyncio.sleep(backoff)
                        backoff = min(backoff * 2, _MAX_BACKOFF)

        except asyncio.CancelledError:
            pass
        finally:
            self._running = False
            if watchdog_task is not None:
                watchdog_task.cancel()
                try:
                    await watchdog_task
                except asyncio.CancelledError:
                    pass
            logger.info("[WS_CRYPTO] Stopped")

    async def stop(self) -> None:
        self._running = False
        if self._websocket:
            try:
                await self._websocket.close()
            except Exception:
                pass

    # ── Message processing ────────────────────────────────────────────────

    def _process_message(self, raw: bytes | str) -> None:
        try:
            data = orjson.loads(raw)
        except Exception:
            return

        # Binance combined stream wraps payload in {"stream": ..., "data": {...}}
        payload = data.get("data", data)
        if not isinstance(payload, dict):
            return

        symbol = (payload.get("s") or "").upper()
        asset = _SYMBOL_TO_ASSET.get(symbol)
        if not asset:
            return

        try:
            price = float(payload["c"])
        except (KeyError, ValueError, TypeError):
            return

        now = time.monotonic()
        self.latest_prices[asset] = price
        self.last_update_ts[asset] = now
