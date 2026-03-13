"""Binance crypto WebSocket — streams real-time spot prices via @trade
and @miniTicker combined streams.

@trade delivers every individual fill (sub-second freshness).
@miniTicker acts as a periodic heartbeat (~1-3s) so prices stay fresh
even during quiet periods with no trades.

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

_BINANCE_WS = "wss://stream.binance.com:9443/stream"

_SYMBOL_TO_ASSET: dict[str, str] = {
    "BTCUSDT": "BTC",
    "ETHUSDT": "ETH",
    "SOLUSDT": "SOL",
    "XRPUSDT": "XRP",
}

_ASSET_TO_SYMBOL: dict[str, str] = {v: k for k, v in _SYMBOL_TO_ASSET.items()}


class CryptoWebSocket:
    """Streams Binance trade + miniTicker prices for a set of crypto assets."""

    def __init__(self, assets: list[str] | None = None) -> None:
        self._assets = [a.upper() for a in (assets or list(_ASSET_TO_SYMBOL))]

        self._streams: list[str] = []
        for a in self._assets:
            sym = _ASSET_TO_SYMBOL.get(a)
            if not sym:
                continue
            lower = sym.lower()
            self._streams.append(f"{lower}@trade")
            self._streams.append(f"{lower}@miniTicker")

        self._ws_url = f"{_BINANCE_WS}?streams={'/'.join(self._streams)}"

        self.latest_prices: dict[str, float] = {}
        self.last_update_ts: dict[str, float] = {}

        self._websocket: Optional[websockets.WebSocketClientProtocol] = None
        self._running = False
        self._last_message_time: float = 0.0
        self._msg_count = 0
        self._trade_count = 0
        self._ticker_count = 0

    # ── Public read-only state ────────────────────────────────────────────

    @property
    def connected(self) -> bool:
        return self._websocket is not None

    @property
    def message_count(self) -> int:
        return self._msg_count

    @property
    def trade_count(self) -> int:
        return self._trade_count

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

    # ── Main run loop ─────────────────────────────────────────────────────

    async def run(self) -> None:
        if not self._streams:
            logger.warning("[WS_CRYPTO] No streams configured — not starting")
            return

        self._running = True
        backoff = _BASE_BACKOFF

        try:
            while self._running:
                try:
                    async with websockets.connect(
                        self._ws_url, ping_interval=20, ping_timeout=60
                    ) as ws:
                        self._websocket = ws
                        backoff = _BASE_BACKOFF
                        logger.info(
                            "[WS_CRYPTO] Connected to Binance (%d streams: %s) [trade+miniTicker]",
                            len(self._streams),
                            ", ".join(self._assets),
                        )

                        try:
                            async for raw in ws:
                                if not self._running:
                                    break
                                self._last_message_time = time.monotonic()
                                self._msg_count += 1
                                self._process_message(raw)
                        finally:
                            self._websocket = None

                except (
                    websockets.exceptions.ConnectionClosed,
                    websockets.exceptions.WebSocketException,
                ) as e:
                    logger.warning(
                        "[WS_CRYPTO] Disconnected: %s — reconnecting in %ds",
                        e, backoff,
                    )
                    if self._running:
                        await asyncio.sleep(backoff)
                        backoff = min(backoff * 2, _MAX_BACKOFF)

                except Exception as e:
                    logger.error(
                        "[WS_CRYPTO] Unexpected error: %s — reconnecting in %ds",
                        e, backoff,
                    )
                    if self._running:
                        await asyncio.sleep(backoff)
                        backoff = min(backoff * 2, _MAX_BACKOFF)

        except asyncio.CancelledError:
            pass
        finally:
            self._running = False
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

        stream = data.get("stream", "")
        payload = data.get("data", data)
        if not isinstance(payload, dict):
            return

        symbol = (payload.get("s") or "").upper()
        asset = _SYMBOL_TO_ASSET.get(symbol)
        if not asset:
            return

        # @trade: price is in "p" field
        # @miniTicker: price is in "c" (close) field
        if "@trade" in stream:
            price_key = "p"
            self._trade_count += 1
        else:
            price_key = "c"
            self._ticker_count += 1

        try:
            price = float(payload[price_key])
        except (KeyError, ValueError, TypeError):
            return

        now = time.monotonic()
        self.latest_prices[asset] = price
        self.last_update_ts[asset] = now
