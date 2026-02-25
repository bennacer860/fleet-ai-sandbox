"""Order fill tracker via the Polymarket user WebSocket channel.

Connects to the CLOB user WebSocket to receive real-time order status
updates (placements, fills, cancellations).  Integrates with SweeperBot
to close the feedback loop on placed orders — answering "did my order
actually get filled?"
"""

import asyncio
import csv
import json
import os
import time
from datetime import datetime
from typing import Any, Callable, Optional

import websockets
from pytz import timezone as pytz_timezone

from ..clob_client import create_clob_client
from ..logging_config import get_logger
from ..utils.timestamps import format_slug_with_est_time

logger = get_logger(__name__)

USER_WS_URL = "wss://ws-subscriptions-clob.polymarket.com/ws/user"

C_GREEN = "\033[92m"
C_RED = "\033[91m"
C_YELLOW = "\033[93m"
C_RESET = "\033[0m"

FillCallback = Callable[[str, dict[str, Any]], None]

CSV_HEADERS = [
    "timestamp_est",
    "timestamp_ms",
    "order_id",
    "event_slug",
    "formatted_slug",
    "outcome",
    "status",
    "placed_price",
    "placed_size",
    "filled_size",
    "fill_price",
    "latency_ms",
    "signal_to_rest_ms",
    "signal_to_book_ms",
    "signal_to_fill_ms",
    "token_id",
]


class FillTracker:
    """Track order fills via the Polymarket user WebSocket channel.

    Lifecycle:
        1. ``track_order()`` registers an order placed by the sweeper.
        2. The background ``run()`` coroutine listens for user-channel
           events and matches them to tracked orders.
        3. Fill, cancel, and rejection events are logged to CSV and
           surfaced through registered callbacks.
    """

    def __init__(
        self,
        fill_log_file: str = "fill_log.csv",
        ws_url: Optional[str] = None,
    ):
        self.ws_url = ws_url or USER_WS_URL
        self.fill_log_file = fill_log_file
        self.running = False
        self.websocket: Optional[websockets.WebSocketClientProtocol] = None
        self._api_creds: Any = None

        self.pending_orders: dict[str, dict[str, Any]] = {}
        self.fills: list[dict[str, Any]] = []

        self.stats = {
            "orders_tracked": 0,
            "fills_confirmed": 0,
            "fills_partial": 0,
            "cancellations": 0,
            "rejections": 0,
            "ws_reconnects": 0,
        }

        self._fill_callbacks: list[FillCallback] = []
        self._csv_file: Any = None
        self._csv_writer: Any = None

    # ── Public API ────────────────────────────────────────────────────────

    def register_fill_callback(self, callback: FillCallback) -> None:
        """Register a callback invoked on every fill/cancel/reject event."""
        self._fill_callbacks.append(callback)

    def track_order(
        self,
        order_id: str,
        slug: str,
        token_id: str,
        outcome: str,
        price: float,
        size: float,
        signal_ns: Optional[int] = None,
        rest_response_ns: Optional[int] = None,
    ) -> None:
        """Register an order for fill tracking.

        Args:
            signal_ns: ``time.perf_counter_ns()`` when the tick_size_change
                callback started.  Used to measure signal-to-book latency.
            rest_response_ns: ``time.perf_counter_ns()`` right after the
                REST API returned.  Used to measure signal-to-REST latency.
        """
        signal_to_rest_ms: Optional[float] = None
        if signal_ns is not None and rest_response_ns is not None:
            signal_to_rest_ms = round((rest_response_ns - signal_ns) / 1_000_000, 1)

        self.pending_orders[order_id] = {
            "order_id": order_id,
            "slug": slug,
            "token_id": token_id,
            "outcome": outcome,
            "price": price,
            "size": size,
            "placed_at_ms": int(time.time() * 1000),
            "status": "PENDING",
            "filled_size": 0.0,
            "fill_price": None,
            "signal_ns": signal_ns,
            "signal_to_rest_ms": signal_to_rest_ms,
        }
        self.stats["orders_tracked"] += 1

        rest_str = f", signal→REST={signal_to_rest_ms:.0f}ms" if signal_to_rest_ms else ""
        logger.info(
            "[FILL_TRACKER] Tracking order %s for %s/%s @ %.4f x %.2f%s",
            order_id[:16],
            slug,
            outcome,
            price,
            size,
            rest_str,
        )

    def get_stats_summary(self) -> str:
        """One-line stats string for periodic logging."""
        s = self.stats
        pending = len(self.pending_orders)
        return (
            f"fills={s['fills_confirmed']} partial={s['fills_partial']} "
            f"cancelled={s['cancellations']} rejected={s['rejections']} "
            f"pending={pending} tracked={s['orders_tracked']} "
            f"reconnects={s['ws_reconnects']}"
        )

    # ── CSV ────────────────────────────────────────────────────────────────

    def _setup_csv(self) -> None:
        file_exists = (
            os.path.isfile(self.fill_log_file)
            and os.path.getsize(self.fill_log_file) > 0
        )
        self._csv_file = open(self.fill_log_file, "a", newline="")
        self._csv_writer = csv.writer(self._csv_file)
        if not file_exists:
            self._csv_writer.writerow(CSV_HEADERS)
            self._csv_file.flush()

    def _close_csv(self) -> None:
        if self._csv_file:
            self._csv_file.close()
            self._csv_file = None
            self._csv_writer = None

    def _log_fill_csv(self, order_id: str, meta: dict[str, Any], status: str) -> None:
        est_tz = pytz_timezone("US/Eastern")
        now_ns = time.perf_counter_ns()
        now_ms = int(time.time() * 1000)
        est_str = datetime.fromtimestamp(now_ms / 1000.0, tz=est_tz).strftime(
            "%Y-%m-%d %H:%M:%S.%f"
        )[:-3]
        latency_ms = now_ms - meta.get("placed_at_ms", now_ms)

        raw_slug = meta.get("slug", "")
        formatted = format_slug_with_est_time(raw_slug) if raw_slug else ""

        signal_to_rest_ms = meta.get("signal_to_rest_ms", "")
        signal_ns = meta.get("signal_ns")

        # signal_to_book_ms: signal → LIVE on book
        signal_to_book_ms = ""
        if signal_ns is not None and status == "LIVE":
            signal_to_book_ms = round((now_ns - signal_ns) / 1_000_000, 1)
            meta["signal_to_book_ms"] = signal_to_book_ms

        # signal_to_fill_ms: signal → first fill
        signal_to_fill_ms = ""
        if signal_ns is not None and status in ("FILLED", "PARTIAL"):
            signal_to_fill_ms = round((now_ns - signal_ns) / 1_000_000, 1)

        if self._csv_writer:
            self._csv_writer.writerow([
                est_str,
                now_ms,
                order_id,
                raw_slug,
                formatted,
                meta.get("outcome", ""),
                status,
                meta.get("price", ""),
                meta.get("size", ""),
                meta.get("filled_size", ""),
                meta.get("fill_price", ""),
                latency_ms,
                signal_to_rest_ms,
                signal_to_book_ms,
                signal_to_fill_ms,
                meta.get("token_id", ""),
            ])
            self._csv_file.flush()

    # ── Message processing ────────────────────────────────────────────────

    def _process_message(self, raw: str) -> None:
        try:
            data = json.loads(raw)
        except json.JSONDecodeError:
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

        if not order_id or order_id not in self.pending_orders:
            return

        meta = self.pending_orders[order_id]
        evt_type = event.get("type", event.get("event_type", "")).upper()
        status = event.get("status", "").upper()

        fill_size: Optional[float] = None
        fill_price: Optional[float] = None
        try:
            if event.get("size"):
                fill_size = float(event["size"])
            fill_price_raw = event.get("match_price") or event.get("price")
            if fill_price_raw:
                fill_price = float(fill_price_raw)
        except (ValueError, TypeError):
            pass

        if evt_type in ("TRADE", "MATCH") or status == "MATCHED":
            self._handle_fill(order_id, meta, fill_size, fill_price)
        elif evt_type == "CANCEL" or status == "CANCELLED":
            self._handle_terminal(order_id, meta, "CANCELLED")
        elif status in ("REJECTED", "EXPIRED"):
            self._handle_terminal(order_id, meta, status)
        elif evt_type == "PLACEMENT" or status == "LIVE":
            meta["status"] = "LIVE"
            self._log_fill_csv(order_id, meta, "LIVE")
            book_ms = meta.get("signal_to_book_ms", "")
            book_str = f" (signal→book={book_ms:.0f}ms)" if book_ms else ""
            logger.info(
                "[FILL_TRACKER] Order %s is LIVE on the book: %s/%s%s",
                order_id[:16],
                meta["slug"],
                meta["outcome"],
                book_str,
            )

    def _handle_fill(
        self,
        order_id: str,
        meta: dict[str, Any],
        fill_size: Optional[float],
        fill_price: Optional[float],
    ) -> None:
        if fill_size:
            meta["filled_size"] += fill_size
        if fill_price:
            meta["fill_price"] = fill_price

        latency_ms = int(time.time() * 1000) - meta["placed_at_ms"]

        signal_ns = meta.get("signal_ns")
        signal_fill_str = ""
        if signal_ns is not None:
            signal_to_fill_ms = round((time.perf_counter_ns() - signal_ns) / 1_000_000, 1)
            signal_fill_str = f", signal→fill={signal_to_fill_ms:.0f}ms"

        if meta["filled_size"] >= meta["size"] * 0.99:
            meta["status"] = "FILLED"
            self.stats["fills_confirmed"] += 1
            self._log_fill_csv(order_id, meta, "FILLED")
            logger.info(
                "%s[FILL] Order %s FILLED: %s/%s @ %.4f x %.2f "
                "(place→fill=%dms%s)%s",
                C_GREEN,
                order_id[:16],
                meta["slug"],
                meta["outcome"],
                meta.get("fill_price", meta["price"]),
                meta["filled_size"],
                latency_ms,
                signal_fill_str,
                C_RESET,
            )
            self.pending_orders.pop(order_id, None)
            self.fills.append(meta)
        else:
            meta["status"] = "PARTIAL"
            self.stats["fills_partial"] += 1
            self._log_fill_csv(order_id, meta, "PARTIAL")
            logger.info(
                "%s[FILL] Order %s PARTIAL: filled %.2f/%.2f "
                "(place→fill=%dms%s)%s",
                C_YELLOW,
                order_id[:16],
                meta["filled_size"],
                meta["size"],
                latency_ms,
                signal_fill_str,
                C_RESET,
            )

        for cb in self._fill_callbacks:
            try:
                cb(order_id, meta)
            except Exception:
                logger.exception("Fill callback error")

    def _handle_terminal(
        self, order_id: str, meta: dict[str, Any], status: str
    ) -> None:
        meta["status"] = status
        if status == "CANCELLED":
            self.stats["cancellations"] += 1
        else:
            self.stats["rejections"] += 1

        self._log_fill_csv(order_id, meta, status)
        logger.warning(
            "%s[FILL] Order %s %s: %s/%s%s",
            C_RED,
            order_id[:16],
            status,
            meta["slug"],
            meta["outcome"],
            C_RESET,
        )
        self.pending_orders.pop(order_id, None)

    # ── WebSocket loop ────────────────────────────────────────────────────

    async def run(self) -> None:
        """Connect to the user WebSocket and listen for order events."""
        client = create_clob_client()
        if client is None:
            logger.error("[FILL_TRACKER] Cannot start: CLOB client unavailable")
            return

        self._api_creds = client.creds
        self._setup_csv()
        self.running = True

        _BASE_BACKOFF = 5
        _MAX_BACKOFF = 60
        backoff = _BASE_BACKOFF

        try:
            while self.running:
                try:
                    async with websockets.connect(
                        self.ws_url,
                        ping_interval=None,
                        ping_timeout=60,
                    ) as ws:
                        self.websocket = ws
                        backoff = _BASE_BACKOFF

                        auth_msg = json.dumps({
                            "auth": {
                                "apiKey": self._api_creds.api_key,
                                "secret": self._api_creds.api_secret,
                                "passphrase": self._api_creds.api_passphrase,
                            },
                            "type": "subscribe",
                            "channels": ["user"],
                        })
                        await ws.send(auth_msg)
                        logger.info("[FILL_TRACKER] Connected to user WebSocket")

                        await self._wait_for_auth(ws)

                        try:
                            async for message in ws:
                                if not self.running:
                                    break
                                if isinstance(message, str) and message != "INVALID OPERATION":
                                    self._process_message(message)
                        finally:
                            self.websocket = None

                except (
                    websockets.exceptions.ConnectionClosed,
                    websockets.exceptions.WebSocketException,
                ) as e:
                    self.stats["ws_reconnects"] += 1
                    if self.running:
                        logger.warning(
                            "[FILL_TRACKER] WS disconnected: %s. Reconnecting in %ds…",
                            e,
                            backoff,
                        )
                        await asyncio.sleep(backoff)
                        backoff = min(backoff * 2, _MAX_BACKOFF)

                except Exception as e:
                    self.stats["ws_reconnects"] += 1
                    if self.running:
                        logger.error(
                            "[FILL_TRACKER] Unexpected error: %s. Reconnecting in %ds…",
                            e,
                            backoff,
                        )
                        await asyncio.sleep(backoff)
                        backoff = min(backoff * 2, _MAX_BACKOFF)

        except asyncio.CancelledError:
            logger.info("[FILL_TRACKER] Shutting down")
        finally:
            self.running = False
            self._close_csv()

    async def _wait_for_auth(self, ws: websockets.WebSocketClientProtocol) -> None:
        try:
            for _ in range(5):
                raw = await asyncio.wait_for(ws.recv(), timeout=5.0)
                try:
                    resp = json.loads(raw)
                    if isinstance(resp, dict) and resp.get("type") == "auth":
                        if resp.get("success"):
                            logger.info("[FILL_TRACKER] Authenticated successfully")
                        else:
                            logger.error("[FILL_TRACKER] Auth failed: %s", resp)
                        return
                except json.JSONDecodeError:
                    pass
        except asyncio.TimeoutError:
            logger.warning("[FILL_TRACKER] Auth response timeout (continuing)")

    async def stop(self) -> None:
        """Gracefully shut down the fill tracker."""
        self.running = False
        if self.websocket:
            await self.websocket.close()
