"""Market-data WebSocket gateway.

Connects to the Polymarket CLOB market WebSocket, subscribes to token
IDs, and publishes typed events (BookUpdate, TickSizeChange,
LastTradePrice) onto the EventBus.

Refactored from the original ``MultiEventMonitor``, keeping the
connection management and subscription logic but removing all CSV I/O,
callback patterns, and display logic.
"""

from __future__ import annotations

import asyncio
import time
from datetime import datetime, timezone
from typing import Any, Optional

import orjson
import websockets

from ..core.event_bus import EventBus
from ..core.events import BookUpdate, LastTradePrice, MarketMeta, MarketResolved, TickSizeChange
from ..gamma_client import (
    fetch_event_by_slug,
    get_market_token_ids,
    get_outcomes,
    get_winning_token_id,
    is_market_ended,
)
from ..logging_config import get_logger

logger = get_logger(__name__)

WS_URL = "wss://ws-subscriptions-clob.polymarket.com/ws/market"

_BASE_BACKOFF = 5
_MAX_BACKOFF = 60
HEARTBEAT_TIMEOUT_S = 90


class MarketWebSocket:
    """Manages a single WebSocket connection to the Polymarket market feed."""

    def __init__(
        self,
        event_bus: EventBus,
        initial_slugs: list[str] | None = None,
        ws_url: str | None = None,
        check_interval: int = 60,
        book_event_filter: set[str] | None = None,
    ) -> None:
        self.event_bus = event_bus
        self.ws_url = ws_url or WS_URL
        self.check_interval = check_interval

        # When set, only token IDs present in this set will trigger full
        # BookUpdate events on the EventBus.  All other tokens still get
        # their best_prices updated (cheap), but skip the expensive
        # sort + event-publish path.  When None, all tokens are published
        # (backwards-compatible default).
        self.book_event_filter: set[str] | None = book_event_filter

        self.token_ids: dict[str, list[str]] = {}
        self.slug_by_token: dict[str, str] = {}
        self.market_active: dict[str, bool] = {}
        self.condition_ids: dict[str, str] = {}
        self.condition_by_token: dict[str, str] = {}
        self.token_outcomes: dict[str, str] = {}
        self.best_prices: dict[str, dict[str, float]] = {}

        self._initial_slugs = initial_slugs or []
        self._websocket: Optional[websockets.WebSocketClientProtocol] = None
        self._running = False
        self._last_message_time: float = 0.0
        self._msg_count = 0
        self._last_tick_size: dict[str, tuple[str, str]] = {}  # slug -> (slug, new_ts)
        self._books_filtered = 0  # counter for filtered-out book updates

    # ── Public read-only state ────────────────────────────────────────────

    @property
    def connected(self) -> bool:
        return self._websocket is not None

    @property
    def message_count(self) -> int:
        return self._msg_count

    @property
    def last_message_age_s(self) -> float:
        if self._last_message_time == 0:
            return -1
        return time.monotonic() - self._last_message_time

    def get_realtime_price(self, asset_id: str) -> float | None:
        prices = self.best_prices.get(asset_id)
        if prices and prices["bid"] > 0:
            return prices["bid"]
        return None

    # ── Market initialisation ─────────────────────────────────────────────

    async def _fetch_token_ids(self, slug: str) -> list[str]:
        loop = asyncio.get_event_loop()
        event = await loop.run_in_executor(None, fetch_event_by_slug, slug)
        if not event:
            logger.error("Failed to fetch event for slug: %s", slug)
            return []

        markets = event.get("markets", [])
        if not markets:
            return []

        market = markets[0]
        tids = get_market_token_ids(market)
        outcomes = get_outcomes(market)
        condition_id = market.get("conditionId", market.get("condition_id", ""))

        if not tids:
            return []

        if condition_id:
            self.condition_ids[slug] = condition_id
            for tid in tids:
                self.condition_by_token[tid] = condition_id

        for i, tid in enumerate(tids):
            if i < len(outcomes):
                self.token_outcomes[tid] = outcomes[i]

        self.event_bus.publish_nowait(MarketMeta(
            slug=slug,
            condition_id=condition_id,
            token_ids=tuple(tids),
            outcomes=tuple(outcomes),
        ))

        return tids

    async def _init_markets(self) -> bool:
        for slug in self._initial_slugs:
            try:
                tids = await self._fetch_token_ids(slug)
                if tids:
                    self.token_ids[slug] = tids
                    self.market_active[slug] = True
                    for tid in tids:
                        self.slug_by_token[tid] = slug
                    logger.info("[MARKET_OPEN] %s (%d tokens)", slug, len(tids))
                else:
                    self.market_active[slug] = False
                    logger.warning("[MARKET_OPEN] Failed: %s", slug)
            except Exception:
                logger.exception("Error initialising %s", slug)
                self.market_active[slug] = False

        active = sum(1 for v in self.market_active.values() if v)
        if active == 0:
            logger.error("No markets initialised")
            return False
        logger.info("Initialised %d/%d markets", active, len(self._initial_slugs))
        return True

    # ── Dynamic market management ─────────────────────────────────────────

    async def add_markets(self, slugs: list[str]) -> None:
        new_tids: list[str] = []
        for slug in slugs:
            if slug in self.token_ids:
                continue
            try:
                tids = await self._fetch_token_ids(slug)
                if tids:
                    self.token_ids[slug] = tids
                    self.market_active[slug] = True
                    if slug not in self._initial_slugs:
                        self._initial_slugs.append(slug)
                    for tid in tids:
                        self.slug_by_token[tid] = slug
                    new_tids.extend(tids)
                    logger.info("[MARKET_ADD] %s (%d tokens)", slug, len(tids))
            except Exception:
                logger.exception("Error adding %s", slug)

        if new_tids and self._websocket:
            msg = orjson.dumps({
                "type": "subscribe",
                "assets_ids": new_tids,
                "channels": ["book", "tick_size_change"],
                "custom_feature_enabled": False,
            })
            await self._websocket.send(msg)

    async def remove_markets(self, slugs: list[str]) -> None:
        tids_to_unsub: list[str] = []
        for slug in slugs:
            if slug not in self.token_ids:
                continue
            tids = self.token_ids.pop(slug)
            tids_to_unsub.extend(tids)
            for tid in tids:
                self.slug_by_token.pop(tid, None)
            self.market_active.pop(slug, None)
            if slug in self._initial_slugs:
                self._initial_slugs.remove(slug)
            logger.info("[MARKET_REMOVE] %s", slug)

        if tids_to_unsub and self._websocket:
            msg = orjson.dumps({"type": "unsubscribe", "assets_ids": tids_to_unsub})
            await self._websocket.send(msg)

    # ── Message processing ────────────────────────────────────────────────

    def _process_book(self, data: dict[str, Any]) -> None:
        asset_id = data.get("asset_id")
        if not asset_id:
            return
        slug = self.slug_by_token.get(asset_id)
        if not slug or not self.market_active.get(slug, False):
            return

        raw_bids = data.get("bids") or []
        raw_asks = data.get("asks") or []

        if not isinstance(raw_bids, list) or not isinstance(raw_asks, list):
            return

        # ── Lightweight best-price extraction (O(n), no sorting) ──────
        # Always compute and cache best_bid/best_ask for all tokens so
        # that StrategyContext.best_prices and PositionTracker stay
        # current.  This is a cheap dict write.
        best_bid = max((float(b["price"]) for b in raw_bids), default=0.0)
        best_ask = min((float(a["price"]) for a in raw_asks), default=0.0)
        self.best_prices[asset_id] = {"bid": best_bid, "ask": best_ask}

        # ── Early exit for non-hot tokens ─────────────────────────────
        # If a book_event_filter is configured and this token is NOT in
        # it, skip the expensive sort + EventBus publish.  This prevents
        # long-duration market book traffic from flooding the event bus
        # and starving time-critical short-duration events.
        if self.book_event_filter is not None and asset_id not in self.book_event_filter:
            self._books_filtered += 1
            return

        # ── Full processing for hot tokens ────────────────────────────
        bids = tuple(
            (float(b["price"]), float(b["size"]))
            for b in sorted(raw_bids, key=lambda x: float(x["price"]), reverse=True)[:10]
        )
        asks = tuple(
            (float(a["price"]), float(a["size"]))
            for a in sorted(raw_asks, key=lambda x: float(x["price"]))[:10]
        )

        cond = self.condition_by_token.get(asset_id, "")
        self.event_bus.publish_nowait(BookUpdate(
            token_id=asset_id,
            condition_id=cond,
            slug=slug,
            bids=bids,
            asks=asks,
            best_bid=best_bid,
            best_ask=best_ask,
        ))

    def _process_tick_size(self, data: dict[str, Any]) -> None:
        asset_id = data.get("asset_id")
        if not asset_id:
            return
        slug = self.slug_by_token.get(asset_id)
        if not slug:
            return

        old_ts = str(data.get("old_tick_size", ""))
        new_ts = str(data.get("new_tick_size", ""))
        cond = self.condition_by_token.get(asset_id, "")

        # ── Dedup: only publish ONE tick_size_change per slug transition ──
        # Each market has 2 tokens and the WS often sends duplicate messages,
        # yielding up to 4 identical events.  We gate on (slug, new_tick_size)
        # so exactly one event reaches the strategy layer.
        dedup_key = (slug, new_ts)
        if dedup_key == self._last_tick_size.get(slug):
            logger.debug(
                "[TICK_SIZE] Dedup skip %s (already published %s)", slug, new_ts,
            )
            return
        self._last_tick_size[slug] = dedup_key

        logger.info("[TICK_SIZE] %s: %s -> %s (token=%s…)", slug, old_ts, new_ts, asset_id[:20])

        self.event_bus.publish_nowait(TickSizeChange(
            condition_id=cond,
            slug=slug,
            token_id=asset_id,
            old_tick_size=old_ts,
            new_tick_size=new_ts,
        ))

    def _process_last_trade(self, data: dict[str, Any]) -> None:
        asset_id = data.get("asset_id")
        if not asset_id:
            return
        slug = self.slug_by_token.get(asset_id)
        if not slug or not self.market_active.get(slug, False):
            return

        try:
            price = float(data.get("price", 0))
            size = float(data.get("size", 0))
        except (ValueError, TypeError):
            return

        side = str(data.get("side", "")).upper()

        self.event_bus.publish_nowait(LastTradePrice(
            token_id=asset_id,
            slug=slug,
            price=price,
            size=size,
            side=side,
        ))

    # ── Market status checker ─────────────────────────────────────────────

    async def _check_status_loop(self) -> None:
        while self._running:
            await asyncio.sleep(self.check_interval)
            loop = asyncio.get_event_loop()
            for slug in list(self.market_active):
                if not self.market_active.get(slug, False):
                    continue
                try:
                    event = await loop.run_in_executor(None, fetch_event_by_slug, slug)
                    if not event:
                        continue
                    markets = event.get("markets", [])
                    if not markets:
                        continue
                    market = markets[0]
                    if is_market_ended(market):
                        self.market_active[slug] = False
                        winning = get_winning_token_id(market)
                        cond = self.condition_ids.get(slug, "")
                        logger.info("[RESOLVED] %s (winner=%s)", slug, winning or "?")
                        if winning:
                            self.event_bus.publish_nowait(MarketResolved(
                                slug=slug,
                                condition_id=cond,
                                winning_token_id=winning,
                            ))
                except Exception:
                    logger.exception("Status check error for %s", slug)

    # ── Heartbeat watchdog ────────────────────────────────────────────────

    async def _heartbeat_watchdog(self) -> None:
        while self._running:
            await asyncio.sleep(HEARTBEAT_TIMEOUT_S / 2)
            if self._last_message_time == 0:
                continue
            silence = time.monotonic() - self._last_message_time
            if silence > HEARTBEAT_TIMEOUT_S and self._websocket:
                logger.warning(
                    "[HEARTBEAT] No message for %.0fs — forcing reconnect", silence
                )
                try:
                    await self._websocket.close()
                except Exception:
                    pass

    # ── Main run loop ─────────────────────────────────────────────────────

    async def run(self) -> None:
        success = await self._init_markets()
        if not success:
            return

        self._running = True

        status_task = asyncio.create_task(self._check_status_loop())
        watchdog_task = asyncio.create_task(self._heartbeat_watchdog())

        backoff = _BASE_BACKOFF
        try:
            while self._running:
                all_tids: list[str] = []
                for tids in self.token_ids.values():
                    all_tids.extend(tids)

                if not all_tids:
                    await asyncio.sleep(backoff)
                    continue

                try:
                    async with websockets.connect(
                        self.ws_url, ping_interval=None, ping_timeout=60
                    ) as ws:
                        self._websocket = ws
                        backoff = _BASE_BACKOFF

                        sub_msg = orjson.dumps({
                            "type": "subscribe",
                            "assets_ids": all_tids,
                            "channels": ["book", "tick_size_change"],
                            "custom_feature_enabled": False,
                        })
                        await ws.send(sub_msg)
                        logger.info("[WS_MARKET] Connected, subscribed to %d tokens (channels: book, tick_size_change)", len(all_tids))

                        try:
                            async for raw in ws:
                                if not self._running:
                                    break

                                self._last_message_time = time.monotonic()
                                self._msg_count += 1

                                if raw == "INVALID OPERATION":
                                    continue

                                try:
                                    data = orjson.loads(raw)
                                except Exception:
                                    continue

                                # Handle both single dicts and lists of events
                                items = data if isinstance(data, list) else [data]
                                for item in items:
                                    if not isinstance(item, dict):
                                        continue
                                        
                                    msg_type = item.get("event_type", item.get("type", ""))

                                    if msg_type == "book":
                                        self._process_book(item)
                                    elif msg_type == "tick_size_change":
                                        self._process_tick_size(item)
                                    elif msg_type == "last_trade_price":
                                        self._process_last_trade(item)
                        finally:
                            self._websocket = None

                except (
                    websockets.exceptions.ConnectionClosed,
                    websockets.exceptions.WebSocketException,
                ) as e:
                    logger.warning("[WS_MARKET] Disconnected: %s — reconnecting in %ds", e, backoff)
                    if self._running:
                        await asyncio.sleep(backoff)
                        backoff = min(backoff * 2, _MAX_BACKOFF)

                except Exception as e:
                    logger.error("[WS_MARKET] Unexpected error: %s — reconnecting in %ds", e, backoff)
                    if self._running:
                        await asyncio.sleep(backoff)
                        backoff = min(backoff * 2, _MAX_BACKOFF)

        except asyncio.CancelledError:
            pass
        finally:
            self._running = False
            status_task.cancel()
            watchdog_task.cancel()
            await asyncio.gather(status_task, watchdog_task, return_exceptions=True)
            logger.info("[WS_MARKET] Stopped")

    async def stop(self) -> None:
        self._running = False
        if self._websocket:
            await self._websocket.close()
