"""Market-data WebSocket gateway.

Connects to the Polymarket CLOB market WebSocket, subscribes to token
IDs, and publishes typed events (BookUpdate, TickSizeChange) onto the
EventBus.

Refactored from the original ``MultiEventMonitor``, keeping the
connection management and subscription logic but removing all CSV I/O,
callback patterns, and display logic.
"""

from __future__ import annotations

import asyncio
import os
import time
from datetime import datetime, timezone
from typing import Any, Optional

import orjson
import websockets

from ..core.event_bus import EventBus
from ..core.events import BookUpdate, MarketMeta, MarketResolved, TickSizeChange
from ..gamma_client import (
    fetch_event_by_slug,
    get_market_token_ids,
    get_outcomes,
    get_winning_token_id,
    is_market_ended,
)
from ..logging_config import get_logger

logger = get_logger(__name__)

# When True, spoof browser-like headers on the WS upgrade request.
# Required on cloud/datacenter IPs (e.g. AWS EC2) where Cloudflare blocks
# raw API connections. Set WS_BROWSER_HEADERS=true in .env for prod;
# leave unset (or false) for local development.
_BROWSER_HEADERS: dict[str, str] | None = (
    {
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/122.0.0.0 Safari/537.36",
        "Origin": "https://polymarket.com",
    }
    if os.environ.get("WS_BROWSER_HEADERS", "").lower() in ("true", "1", "yes")
    else None
)

WS_URL = "wss://ws-subscriptions-clob.polymarket.com/ws/market"

_BASE_BACKOFF = 5
_MAX_BACKOFF = 60
HEARTBEAT_CHECK_INTERVAL_S = 2
HEARTBEAT_RESUBSCRIBE_AFTER_S = 4
HEARTBEAT_RECONNECT_AFTER_S = 6
_DATA_CHANNELS: tuple[str, ...] = ("book", "price_change", "tick_size_change")


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
        self._last_data_message_time: float = 0.0
        self._connected_since: float = 0.0
        self._last_channel_message_time: dict[str, float] = {c: 0.0 for c in _DATA_CHANNELS}
        self._msg_count = 0
        self._reconnect_count = 0
        self._resubscribe_count = 0
        self._resubscribe_attempted_for_stale = False
        self._last_tick_size: dict[str, tuple[str, str]] = {}  # slug -> (slug, new_ts)
        self._books_filtered = 0  # counter for filtered-out book updates
        self._books_processed = 0  # counter for successfully processed book updates
        self._last_top_by_token: dict[str, tuple[float, float]] = {}

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
    def resubscribe_count(self) -> int:
        return self._resubscribe_count

    @property
    def last_message_age_s(self) -> float:
        if self._last_message_time == 0:
            return -1
        return time.monotonic() - self._last_message_time

    @property
    def last_data_message_age_s(self) -> float:
        """Seconds since last market-data event (book/price_change/tick_size)."""
        if self._last_data_message_time == 0:
            return -1
        return time.monotonic() - self._last_data_message_time

    def channel_message_ages_s(self) -> dict[str, float]:
        """Per-channel message age in seconds (-1 means never seen)."""
        now = time.monotonic()
        ages: dict[str, float] = {}
        for channel, ts in self._last_channel_message_time.items():
            ages[channel] = -1 if ts == 0 else (now - ts)
        return ages

    def get_realtime_price(self, asset_id: str) -> float | None:
        prices = self.best_prices.get(asset_id)
        if prices and prices["bid"] > 0:
            return prices["bid"]
        return None

    def _mark_data_message(self, channel: str) -> None:
        """Record market-data activity timestamps for watchdog/diagnostics."""
        now = time.monotonic()
        self._last_data_message_time = now
        self._resubscribe_attempted_for_stale = False
        if channel in self._last_channel_message_time:
            self._last_channel_message_time[channel] = now

    def _all_token_ids(self) -> list[str]:
        all_tids: list[str] = []
        for tids in self.token_ids.values():
            all_tids.extend(tids)
        return all_tids

    @staticmethod
    def _build_subscribe_message(asset_ids: list[str]) -> str:
        return orjson.dumps({
            "type": "subscribe",
            "assets_ids": asset_ids,
            "channels": ["book", "price_change", "tick_size_change"],
            "custom_feature_enabled": False,
        }).decode("utf-8")

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

        if new_tids:
            if self._websocket:
                all_tids: list[str] = []
                all_tids = self._all_token_ids()
                msg = self._build_subscribe_message(all_tids)
                await self._websocket.send(msg)
                logger.info(
                    "[MARKET_WS_SUB] Re-subscribed ALL %d tokens (%d new) on live WS "
                    "(channels: book, price_change, tick_size_change)",
                    len(all_tids),
                    len(new_tids),
                )
            else:
                logger.warning("[MARKET_WS_SUB] No active WS connection — %d tokens NOT subscribed", len(new_tids))

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
            msg = orjson.dumps({"type": "unsubscribe", "assets_ids": tids_to_unsub}).decode("utf-8")
            await self._websocket.send(msg)

    # ── Message processing ────────────────────────────────────────────────

    def _process_book(self, data: dict[str, Any]) -> None:
        asset_id = data.get("asset_id")
        if not asset_id:
            return
        slug = self.slug_by_token.get(asset_id)
        if not slug or not self.market_active.get(slug, False):
            if self._msg_count % 500 == 0:
                logger.debug(
                    "[BOOK_DROP] asset=%s… slug=%s active=%s known_slugs=%d",
                    str(asset_id)[:20], slug,
                    self.market_active.get(slug) if slug else "N/A",
                    len(self.slug_by_token),
                )
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
        self._books_processed += 1
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

        latency_ms: float | None = None
        try:
            exchange_ts = int(data.get("timestamp", 0))
            if exchange_ts > 0:
                latency_ms = (time.time() * 1000) - exchange_ts
        except (ValueError, TypeError):
            pass

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

        if latency_ms is not None:
            logger.info("[TICK_SIZE] %s: %s -> %s (token=%s…) [lat: %.1fms]", slug, old_ts, new_ts, asset_id[:20], latency_ms)
        else:
            logger.info("[TICK_SIZE] %s: %s -> %s (token=%s…)", slug, old_ts, new_ts, asset_id[:20])

        self.event_bus.publish_nowait(TickSizeChange(
            condition_id=cond,
            slug=slug,
            token_id=asset_id,
            old_tick_size=old_ts,
            new_tick_size=new_ts,
            latency_ms=latency_ms,
        ))

    def _process_price_change(self, data: dict[str, Any]) -> None:
        """Handle price_change batches and keep best_prices fresh.

        Polymarket emits frequent top-of-book updates via ``price_change``
        events. These include ``best_bid`` / ``best_ask`` per asset_id.
        """
        changes = data.get("price_changes") or []
        if not isinstance(changes, list):
            return

        for ch in changes:
            if not isinstance(ch, dict):
                continue
            asset_id = ch.get("asset_id")
            if not asset_id:
                continue

            slug = self.slug_by_token.get(asset_id)
            if not slug or not self.market_active.get(slug, False):
                continue

            try:
                best_bid = float(ch.get("best_bid", 0) or 0)
                best_ask = float(ch.get("best_ask", 0) or 0)
            except (ValueError, TypeError):
                continue

            if best_bid <= 0 and best_ask <= 0:
                continue

            prev = self._last_top_by_token.get(asset_id)
            cur = (best_bid, best_ask)
            if prev == cur:
                continue

            self._last_top_by_token[asset_id] = cur
            self.best_prices[asset_id] = {"bid": best_bid, "ask": best_ask}

            if self.book_event_filter is not None and asset_id not in self.book_event_filter:
                self._books_filtered += 1
                continue

            # Emit a lightweight BookUpdate so strategies react to live top-of-book changes.
            cond = self.condition_by_token.get(asset_id, "")
            bids = ((best_bid, 0.0),) if best_bid > 0 else tuple()
            asks = ((best_ask, 0.0),) if best_ask > 0 else tuple()
            self._books_processed += 1
            self.event_bus.publish_nowait(BookUpdate(
                token_id=asset_id,
                condition_id=cond,
                slug=slug,
                bids=bids,
                asks=asks,
                best_bid=best_bid,
                best_ask=best_ask,
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

    async def _attempt_inplace_resubscribe_recovery(
        self,
        ws: websockets.WebSocketClientProtocol,
        reason: str,
        silence: float,
    ) -> None:
        """Try one in-place re-subscribe during a stale episode."""
        try:
            if self._websocket is not ws:
                return

            all_tids = self._all_token_ids()
            if not all_tids:
                return

            await ws.send(self._build_subscribe_message(all_tids))
            self._resubscribe_count += 1
            logger.warning(
                "[HEARTBEAT] %s for %.0fs — attempting in-place re-subscribe (%d tokens) [count=%d]",
                reason,
                silence,
                len(all_tids),
                self._resubscribe_count,
            )
            self._resubscribe_attempted_for_stale = True
        except Exception:
            logger.warning("[HEARTBEAT] In-place re-subscribe attempt failed", exc_info=True)

    async def _heartbeat_watchdog(self) -> None:
        while self._running:
            await asyncio.sleep(HEARTBEAT_CHECK_INTERVAL_S)
            if not self._websocket:
                continue

            # Reconnect when market-data stalls even if control frames (PONG)
            # continue to arrive. This avoids half-open "connected but frozen"
            # sessions that only recover on manual restart.
            if self._last_data_message_time == 0:
                if self._connected_since == 0:
                    continue
                silence = time.monotonic() - self._connected_since
                reason = "No market-data messages since connect"
            else:
                silence = time.monotonic() - self._last_data_message_time
                reason = "No market-data message"

            if (
                silence > HEARTBEAT_RESUBSCRIBE_AFTER_S
                and not self._resubscribe_attempted_for_stale
            ):
                ws = self._websocket
                if ws:
                    await self._attempt_inplace_resubscribe_recovery(ws, reason, silence)

            if silence > HEARTBEAT_RECONNECT_AFTER_S:
                logger.warning(
                    "[HEARTBEAT] %s for %.0fs — forcing reconnect", reason, silence
                )
                try:
                    await self._websocket.close()
                except Exception:
                    pass

    # ── Main run loop ─────────────────────────────────────────────────────

    async def _send_app_pings(self, ws: websockets.WebSocketClientProtocol) -> None:
        """Send application-level PING payloads Polymarket expects for keep-alive."""
        while self._running and self._websocket is ws:
            try:
                await asyncio.sleep(10)
                if self._websocket is ws:
                    await ws.send("PING")
            except Exception:
                break

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
                all_tids = self._all_token_ids()

                if not all_tids:
                    await asyncio.sleep(backoff)
                    continue

                try:
                    async with websockets.connect(
                        self.ws_url,
                        ping_interval=20,
                        ping_timeout=30,
                        extra_headers=_BROWSER_HEADERS,
                    ) as ws:
                        self._websocket = ws
                        self._connected_since = time.monotonic()
                        self._last_data_message_time = 0.0
                        self._resubscribe_attempted_for_stale = False
                        self._last_channel_message_time = {c: 0.0 for c in _DATA_CHANNELS}
                        backoff = _BASE_BACKOFF

                        sub_msg = self._build_subscribe_message(all_tids)
                        await ws.send(sub_msg)
                        logger.info(
                            "[WS_MARKET] Connected, subscribed to %d tokens "
                            "(channels: book, price_change, tick_size_change)",
                            len(all_tids),
                        )

                        ping_task = asyncio.create_task(self._send_app_pings(ws))
                        try:
                            async for raw in ws:
                                if not self._running:
                                    break

                                self._last_message_time = time.monotonic()
                                self._msg_count += 1

                                if raw == "INVALID OPERATION" or raw == "PONG":
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
                                    if msg_type in _DATA_CHANNELS:
                                        self._mark_data_message(msg_type)

                                    if msg_type == "book":
                                        self._process_book(item)
                                    elif msg_type == "price_change":
                                        self._process_price_change(item)
                                    elif msg_type == "tick_size_change":
                                        self._process_tick_size(item)
                        finally:
                            ping_task.cancel()
                            await asyncio.gather(ping_task, return_exceptions=True)
                            self._websocket = None
                            self._connected_since = 0.0

                except (
                    websockets.exceptions.ConnectionClosed,
                    websockets.exceptions.WebSocketException,
                ) as e:
                    self._reconnect_count += 1
                    logger.warning("[WS_MARKET] Disconnected: %s — reconnect #%d in %ds", e, self._reconnect_count, backoff)
                    if self._running:
                        await asyncio.sleep(backoff)
                        backoff = min(backoff * 2, _MAX_BACKOFF)

                except Exception as e:
                    self._reconnect_count += 1
                    logger.error("[WS_MARKET] Unexpected error: %s — reconnect #%d in %ds", e, self._reconnect_count, backoff)
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
            try:
                await self._websocket.close()
            except Exception:
                pass
