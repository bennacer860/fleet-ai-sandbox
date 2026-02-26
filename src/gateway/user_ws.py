"""User-channel WebSocket gateway for order fill tracking.

Connects to the Polymarket CLOB user WebSocket, authenticates with API
credentials, and publishes typed events (OrderFill, OrderTerminal,
OrderLive) onto the EventBus.

Refactored from the original ``FillTracker``, removing CSV I/O and
in-process order tracking (now handled by OrderManager).
"""

from __future__ import annotations

import asyncio
import time
from typing import Any, Optional

import orjson
import websockets

from ..clob_client import create_clob_client
from ..core.event_bus import EventBus
from ..core.events import OrderFill, OrderLive, OrderStatus, OrderTerminal
from ..logging_config import get_logger

logger = get_logger(__name__)

USER_WS_URL = "wss://ws-subscriptions-clob.polymarket.com/ws/user"

_BASE_BACKOFF = 5
_MAX_BACKOFF = 60


class UserWebSocket:
    """Manages a single WebSocket connection to the Polymarket user feed."""

    def __init__(
        self,
        event_bus: EventBus,
        ws_url: str | None = None,
    ) -> None:
        self.event_bus = event_bus
        self.ws_url = ws_url or USER_WS_URL
        self._websocket: Optional[websockets.WebSocketClientProtocol] = None
        self._running = False
        self._api_creds: Any = None
        self._reconnect_count = 0

    @property
    def connected(self) -> bool:
        return self._websocket is not None

    @property
    def reconnect_count(self) -> int:
        return self._reconnect_count

    # ── Message processing ────────────────────────────────────────────────

    def _process_message(self, raw: str) -> None:
        try:
            data = orjson.loads(raw)
        except Exception:
            return

        items = data if isinstance(data, list) else [data]
        for item in items:
            if isinstance(item, dict):
                self._process_event(item)

    def _process_event(self, event: dict[str, Any]) -> None:
        order_id = (
            event.get("id", "")
            or event.get("order_id", "")
            or event.get("orderId", "")
            or event.get("orderID", "")
        )
        if not order_id:
            return

        evt_type = event.get("type", event.get("event_type", "")).upper()
        status = event.get("status", "").upper()

        fill_size: float | None = None
        fill_price: float | None = None
        try:
            if event.get("size"):
                fill_size = float(event["size"])
            raw_price = event.get("match_price") or event.get("price")
            if raw_price:
                fill_price = float(raw_price)
        except (ValueError, TypeError):
            pass

        if evt_type in ("TRADE", "MATCH") or status == "MATCHED":
            self.event_bus.publish_nowait(OrderFill(
                order_id=order_id,
                fill_price=fill_price or 0.0,
                fill_size=fill_size or 0.0,
                status=OrderStatus.PARTIAL,
            ))
        elif evt_type == "CANCEL" or status == "CANCELLED":
            self.event_bus.publish_nowait(OrderTerminal(
                order_id=order_id,
                status=OrderStatus.CANCELLED,
            ))
        elif status in ("REJECTED",):
            self.event_bus.publish_nowait(OrderTerminal(
                order_id=order_id,
                status=OrderStatus.REJECTED,
                reason=event.get("reason", ""),
            ))
        elif status in ("EXPIRED",):
            self.event_bus.publish_nowait(OrderTerminal(
                order_id=order_id,
                status=OrderStatus.EXPIRED,
            ))
        elif evt_type == "PLACEMENT" or status == "LIVE":
            self.event_bus.publish_nowait(OrderLive(order_id=order_id))

    # ── Auth ──────────────────────────────────────────────────────────────

    async def _authenticate(self, ws: websockets.WebSocketClientProtocol) -> bool:
        auth_msg = orjson.dumps({
            "auth": {
                "apiKey": self._api_creds.api_key,
                "secret": self._api_creds.api_secret,
                "passphrase": self._api_creds.api_passphrase,
            },
            "type": "subscribe",
            "channels": ["user"],
        })
        await ws.send(auth_msg)

        for _ in range(5):
            try:
                raw = await asyncio.wait_for(ws.recv(), timeout=5.0)
                resp = orjson.loads(raw)
                if isinstance(resp, dict) and resp.get("type") == "auth":
                    if resp.get("success"):
                        logger.info("[WS_USER] Authenticated")
                        return True
                    logger.error("[WS_USER] Auth failed: %s", resp)
                    return False
            except asyncio.TimeoutError:
                break
            except Exception:
                pass

        logger.warning("[WS_USER] Auth response timeout — continuing")
        return True

    # ── Main run loop ─────────────────────────────────────────────────────

    async def run(self) -> None:
        client = create_clob_client()
        if client is None:
            logger.error("[WS_USER] Cannot start: CLOB client unavailable")
            return

        self._api_creds = client.creds
        self._running = True
        backoff = _BASE_BACKOFF

        try:
            while self._running:
                try:
                    async with websockets.connect(
                        self.ws_url, ping_interval=None, ping_timeout=60
                    ) as ws:
                        self._websocket = ws
                        backoff = _BASE_BACKOFF

                        if not await self._authenticate(ws):
                            await asyncio.sleep(backoff)
                            continue

                        try:
                            async for message in ws:
                                if not self._running:
                                    break
                                if isinstance(message, str) and message != "INVALID OPERATION":
                                    self._process_message(message)
                        finally:
                            self._websocket = None

                except (
                    websockets.exceptions.ConnectionClosed,
                    websockets.exceptions.WebSocketException,
                ) as e:
                    self._reconnect_count += 1
                    if self._running:
                        logger.warning("[WS_USER] Disconnected: %s — reconnecting in %ds", e, backoff)
                        await asyncio.sleep(backoff)
                        backoff = min(backoff * 2, _MAX_BACKOFF)

                except Exception as e:
                    self._reconnect_count += 1
                    if self._running:
                        logger.error("[WS_USER] Error: %s — reconnecting in %ds", e, backoff)
                        await asyncio.sleep(backoff)
                        backoff = min(backoff * 2, _MAX_BACKOFF)

        except asyncio.CancelledError:
            pass
        finally:
            self._running = False
            logger.info("[WS_USER] Stopped")

    async def stop(self) -> None:
        self._running = False
        if self._websocket:
            await self._websocket.close()
