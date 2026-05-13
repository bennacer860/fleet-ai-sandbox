"""Crypto spot-price WebSocket — streams real-time prices from Binance & Bybit.

Maintains a ``latest_prices`` dict that callers can read at any time
(no EventBus needed).  Tracks per-asset update timestamps so consumers
can detect stale data.

Assets in ``_BYBIT_ASSETS`` are routed to Bybit's spot ticker WS
(e.g. HYPE has no Binance spot pair).  All others use Binance miniTicker.
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
_HEARTBEAT_TIMEOUT_S = 30

_BINANCE_WS = "wss://stream.binance.com:9443/stream"
_BYBIT_WS = "wss://stream.bybit.com/v5/public/spot"

_SYMBOL_TO_ASSET: dict[str, str] = {
    "BTCUSDT": "BTC",
    "ETHUSDT": "ETH",
    "SOLUSDT": "SOL",
    "XRPUSDT": "XRP",
    "DOGEUSDT": "DOGE",
    "HYPEUSDT": "HYPE",
    "BNBUSDT": "BNB",
}

_ASSET_TO_SYMBOL: dict[str, str] = {v: k for k, v in _SYMBOL_TO_ASSET.items()}

_BYBIT_ASSETS: set[str] = {"HYPE"}


class CryptoWebSocket:
    """Streams spot prices from Binance (most assets) and Bybit (HYPE etc.)."""

    def __init__(self, assets: list[str] | None = None) -> None:
        self._assets = [a.upper() for a in (assets or list(_ASSET_TO_SYMBOL))]

        self._binance_assets = [a for a in self._assets if a not in _BYBIT_ASSETS and a in _ASSET_TO_SYMBOL]
        self._bybit_assets = [a for a in self._assets if a in _BYBIT_ASSETS and a in _ASSET_TO_SYMBOL]

        self._binance_streams = [
            f"{_ASSET_TO_SYMBOL[a].lower()}@miniTicker"
            for a in self._binance_assets
        ]
        self._binance_url = f"{_BINANCE_WS}?streams={'/'.join(self._binance_streams)}" if self._binance_streams else ""

        self.latest_prices: dict[str, float] = {}
        self.last_update_ts: dict[str, float] = {}

        self._binance_ws: Optional[websockets.WebSocketClientProtocol] = None
        self._bybit_ws: Optional[websockets.WebSocketClientProtocol] = None
        self._running = False
        self._last_message_time: float = 0.0
        self._msg_count = 0
        self._reconnect_count = 0

    # ── Public read-only state ────────────────────────────────────────────

    @property
    def connected(self) -> bool:
        binance_ok = self._binance_ws is not None if self._binance_streams else True
        bybit_ok = self._bybit_ws is not None if self._bybit_assets else True
        return binance_ok and bybit_ok

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

    async def _heartbeat_watchdog(self) -> None:
        while self._running:
            await asyncio.sleep(_HEARTBEAT_TIMEOUT_S / 2)
            if self._last_message_time == 0:
                continue
            silence = time.monotonic() - self._last_message_time
            if silence > _HEARTBEAT_TIMEOUT_S:
                if self._binance_ws:
                    logger.warning("[WS_CRYPTO] No message for %.0fs — forcing Binance reconnect", silence)
                    try:
                        await self._binance_ws.close()
                    except Exception:
                        pass
                if self._bybit_ws:
                    logger.warning("[WS_CRYPTO] No message for %.0fs — forcing Bybit reconnect", silence)
                    try:
                        await self._bybit_ws.close()
                    except Exception:
                        pass

    # ── Binance connection loop ───────────────────────────────────────────

    async def _run_binance(self) -> None:
        if not self._binance_streams:
            return
        backoff = _BASE_BACKOFF
        while self._running:
            try:
                async with websockets.connect(
                    self._binance_url, ping_interval=20, ping_timeout=20
                ) as ws:
                    self._binance_ws = ws
                    backoff = _BASE_BACKOFF
                    logger.info(
                        "[WS_CRYPTO] Binance connected (%d streams: %s)",
                        len(self._binance_streams), ", ".join(self._binance_assets),
                    )
                    try:
                        async for raw in ws:
                            if not self._running:
                                break
                            self._last_message_time = time.monotonic()
                            self._msg_count += 1
                            if raw == "INVALID OPERATION":
                                continue
                            self._process_binance(raw)
                    finally:
                        self._binance_ws = None
            except (websockets.exceptions.ConnectionClosed, websockets.exceptions.WebSocketException) as e:
                self._reconnect_count += 1
                logger.warning("[WS_CRYPTO] Binance disconnected: %s — reconnect in %ds", e, backoff)
            except Exception as e:
                self._reconnect_count += 1
                logger.error("[WS_CRYPTO] Binance error: %s — reconnect in %ds", e, backoff)
            if self._running:
                await asyncio.sleep(backoff)
                backoff = min(backoff * 2, _MAX_BACKOFF)

    def _process_binance(self, raw: bytes | str) -> None:
        try:
            data = orjson.loads(raw)
        except Exception:
            return
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
        self.latest_prices[asset] = price
        self.last_update_ts[asset] = time.monotonic()

    # ── Bybit connection loop ────────────────────────────────────────────

    async def _run_bybit(self) -> None:
        if not self._bybit_assets:
            return
        backoff = _BASE_BACKOFF
        topics = [f"tickers.{_ASSET_TO_SYMBOL[a]}" for a in self._bybit_assets]
        sub_msg = orjson.dumps({"op": "subscribe", "args": topics})

        while self._running:
            try:
                async with websockets.connect(
                    _BYBIT_WS, ping_interval=20, ping_timeout=20
                ) as ws:
                    self._bybit_ws = ws
                    backoff = _BASE_BACKOFF
                    await ws.send(sub_msg)
                    logger.info(
                        "[WS_CRYPTO] Bybit connected (%d topics: %s)",
                        len(topics), ", ".join(self._bybit_assets),
                    )
                    try:
                        async for raw in ws:
                            if not self._running:
                                break
                            self._last_message_time = time.monotonic()
                            self._msg_count += 1
                            self._process_bybit(raw)
                    finally:
                        self._bybit_ws = None
            except (websockets.exceptions.ConnectionClosed, websockets.exceptions.WebSocketException) as e:
                self._reconnect_count += 1
                logger.warning("[WS_CRYPTO] Bybit disconnected: %s — reconnect in %ds", e, backoff)
            except Exception as e:
                self._reconnect_count += 1
                logger.error("[WS_CRYPTO] Bybit error: %s — reconnect in %ds", e, backoff)
            if self._running:
                await asyncio.sleep(backoff)
                backoff = min(backoff * 2, _MAX_BACKOFF)

    def _process_bybit(self, raw: bytes | str) -> None:
        try:
            data = orjson.loads(raw)
        except Exception:
            return
        if data.get("op") == "subscribe":
            return
        topic = data.get("topic", "")
        if not topic.startswith("tickers."):
            return
        payload = data.get("data")
        if not isinstance(payload, dict):
            return
        symbol = (payload.get("symbol") or "").upper()
        asset = _SYMBOL_TO_ASSET.get(symbol)
        if not asset:
            return
        try:
            price = float(payload["lastPrice"])
        except (KeyError, ValueError, TypeError):
            return
        self.latest_prices[asset] = price
        self.last_update_ts[asset] = time.monotonic()

    # ── Main run / stop ──────────────────────────────────────────────────

    async def run(self) -> None:
        if not self._binance_streams and not self._bybit_assets:
            logger.warning("[WS_CRYPTO] No streams configured — not starting")
            return

        self._running = True
        tasks: list[asyncio.Task] = [
            asyncio.create_task(self._heartbeat_watchdog()),
        ]
        if self._binance_streams:
            tasks.append(asyncio.create_task(self._run_binance()))
        if self._bybit_assets:
            tasks.append(asyncio.create_task(self._run_bybit()))

        try:
            await asyncio.gather(*tasks)
        except asyncio.CancelledError:
            pass
        finally:
            self._running = False
            for t in tasks:
                t.cancel()
            await asyncio.gather(*tasks, return_exceptions=True)
            logger.info("[WS_CRYPTO] Stopped")

    async def stop(self) -> None:
        self._running = False
        for ws in (self._binance_ws, self._bybit_ws):
            if ws:
                try:
                    await ws.close()
                except Exception:
                    pass
