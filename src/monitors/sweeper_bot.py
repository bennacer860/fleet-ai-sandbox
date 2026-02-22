"""Endgame-sweep trading bot.

Continuously monitors both 5-minute and 15-minute Polymarket crypto markets
through a single WebSocket connection.  When a ``tick_size_change`` event fires
with ``new_tick_size == "0.001"``, the bot evaluates the market and, if the most
likely outcome has a price >= the configured threshold (default 0.9), places a
minimum-size limit BUY order on that outcome.

The bot auto-rolls to new market windows exactly like ``ContinuousCryptoMonitor``.
"""

import asyncio
import time
from typing import Optional

from .multi_event_monitor import MultiEventMonitor
from ..markets.fifteen_min import (
    get_market_slug,
    get_current_interval_utc,
    get_next_interval_utc,
    MarketSelection,
    SUPPORTED_DURATIONS,
)
from ..strategy.sweep_signal import (
    DEFAULT_PRICE_THRESHOLD,
    should_place_sweep_order,
)
from ..strategy.order_executor import execute_sweep_order
from ..logging_config import get_logger
from ..utils.slug_helpers import slugs_for_timestamp

logger = get_logger(__name__)

# ── Constants ─────────────────────────────────────────────────────────────────

# Grace period after market end before unsubscribing (seconds)
GRACE_PERIOD_SECONDS = 5 * 60

# How often the subscription manager checks for new/expired markets (seconds)
DEFAULT_SUB_CHECK_INTERVAL = 30

# How often the MultiEventMonitor checks market status via API (seconds)
DEFAULT_MARKET_STATUS_CHECK_INTERVAL = 60

# How often a summary of tracked slugs is logged (seconds)
SLUG_SUMMARY_INTERVAL = 60


# ── SweeperBot ────────────────────────────────────────────────────────────────


class SweeperBot:
    """Orchestrator that combines continuous multi-duration monitoring with the
    endgame-sweep trading strategy.

    Features:
        * Manages a **single** ``MultiEventMonitor`` WebSocket connection.
        * Tracks both 5-min and 15-min markets simultaneously.
        * Auto-rolls to current + next windows per duration.
        * Detects the sweep signal (tick_size -> 0.001) and places orders.
        * Periodically logs which slugs are currently being tracked.
    """

    def __init__(
        self,
        market_selections: list[MarketSelection],
        durations: Optional[list[int]] = None,
        price_threshold: float = DEFAULT_PRICE_THRESHOLD,
        output_file: str = "sweeper_trades.csv",
        ws_url: Optional[str] = None,
        sub_check_interval: int = DEFAULT_SUB_CHECK_INTERVAL,
        dry_run: bool = False,
    ):
        """
        Args:
            market_selections: Crypto assets to monitor (e.g. ``["BTC", "ETH"]``).
            durations: Market durations in minutes (default ``[5, 15]``).
            price_threshold: Minimum outcome price to trigger an order.
            output_file: CSV file for unified WebSocket event log.
            ws_url: Optional WebSocket URL override.
            sub_check_interval: Seconds between subscription management checks.
            dry_run: If *True*, log order decisions without submitting.
        """
        self.market_selections = market_selections
        self.durations = durations or sorted(SUPPORTED_DURATIONS)
        self.price_threshold = price_threshold
        self.output_file = output_file
        self.ws_url = ws_url
        self.sub_check_interval = sub_check_interval
        self.dry_run = dry_run

        self.running = False
        self.monitor: Optional[MultiEventMonitor] = None

        # Per-duration tracking: {duration -> {selection -> set[timestamp]}}
        self._monitored_ts: dict[int, dict[MarketSelection, set[int]]] = {
            dur: {sel: set() for sel in market_selections}
            for dur in self.durations
        }

    # ── Initial slug generation ───────────────────────────────────────────

    def _initial_slugs(self) -> list[str]:
        """Build the initial list of slugs (current + next window per duration)."""
        slugs: list[str] = []
        for dur in self.durations:
            cur_ts = get_current_interval_utc(dur)
            nxt_ts = get_next_interval_utc(dur)

            cur_slugs = slugs_for_timestamp(self.market_selections, dur, cur_ts)
            nxt_slugs = slugs_for_timestamp(self.market_selections, dur, nxt_ts)

            for sel in self.market_selections:
                self._monitored_ts[dur][sel].add(cur_ts)
                self._monitored_ts[dur][sel].add(nxt_ts)

            slugs.extend(cur_slugs)
            slugs.extend(nxt_slugs)
        return slugs

    # ── Subscription management (per duration) ────────────────────────────

    async def _manage_subscriptions_for_duration(self, duration: int) -> None:
        """Periodically add new / remove expired markets for one duration."""
        interval_seconds = duration * 60
        while self.running:
            await asyncio.sleep(self.sub_check_interval)
            if not self.monitor or not self.monitor.running:
                continue

            cur_ts = get_current_interval_utc(duration)
            nxt_ts = get_next_interval_utc(duration)
            now = int(time.time())

            slugs_to_add: list[str] = []
            slugs_to_remove: list[str] = []

            for sel in self.market_selections:
                tracked = self._monitored_ts[duration][sel]

                # Ensure next window is subscribed
                if nxt_ts not in tracked:
                    try:
                        slug = get_market_slug(sel, duration, nxt_ts)
                        slugs_to_add.append(slug)
                        tracked.add(nxt_ts)
                        logger.info("Queuing next %dm market for %s: %s", duration, sel, slug)
                    except ValueError as exc:
                        logger.error("Slug generation failed (%s/%dm next): %s", sel, duration, exc)

                # Ensure current window is subscribed
                if cur_ts not in tracked:
                    try:
                        slug = get_market_slug(sel, duration, cur_ts)
                        slugs_to_add.append(slug)
                        tracked.add(cur_ts)
                        logger.info("Queuing current %dm market for %s: %s", duration, sel, slug)
                    except ValueError as exc:
                        logger.error("Slug generation failed (%s/%dm current): %s", sel, duration, exc)

                # Prune expired windows
                expired: list[int] = []
                for ts in tracked:
                    end_time = ts + interval_seconds
                    if now > end_time + GRACE_PERIOD_SECONDS:
                        try:
                            slug = get_market_slug(sel, duration, ts)
                            if slug in self.monitor.market_active and not self.monitor.market_active[slug]:
                                slugs_to_remove.append(slug)
                                expired.append(ts)
                                logger.info("Queuing removal of %dm market for %s: %s", duration, sel, slug)
                        except ValueError:
                            expired.append(ts)
                for ts in expired:
                    tracked.discard(ts)

            if slugs_to_add:
                await self.monitor.add_markets(slugs_to_add)
            if slugs_to_remove:
                await self.monitor.remove_markets(slugs_to_remove)

    # ── Periodic slug summary ─────────────────────────────────────────────

    async def _log_tracked_slugs(self) -> None:
        """Periodically log the list of actively tracked market slugs."""
        while self.running:
            await asyncio.sleep(SLUG_SUMMARY_INTERVAL)
            if not self.monitor:
                continue
            active = [
                slug for slug, is_active in self.monitor.market_active.items() if is_active
            ]
            if active:
                logger.info(
                    "Currently tracking %d active market(s): %s",
                    len(active),
                    ", ".join(active),
                )
            else:
                logger.info("No active markets currently tracked.")

    # ── Tick-size callback (strategy entry point) ─────────────────────────

    def _on_tick_size_change(
        self,
        slug: str,
        asset_id: str,
        old_tick_size: str,
        new_tick_size: str,
        timestamp_ms: int,
    ) -> None:
        """Called by ``MultiEventMonitor`` on every ``tick_size_change`` event."""
        logger.info(
            "Tick-size change for %s: %s -> %s (token=%s…)",
            slug,
            old_tick_size,
            new_tick_size,
            asset_id[:20],
        )

        order_params = should_place_sweep_order(
            slug=slug,
            new_tick_size=new_tick_size,
            price_threshold=self.price_threshold,
        )

        if order_params is None:
            logger.debug("No sweep order for %s (signal/price not met)", slug)
            return

        logger.info(
            "Sweep signal confirmed for %s – placing order: %s @ %.4f x %.2f",
            slug,
            order_params["outcome"],
            order_params["price"],
            order_params["size"],
        )

        execute_sweep_order(
            token_id=order_params["token_id"],
            price=order_params["price"],
            size=order_params["size"],
            slug=order_params["slug"],
            outcome=order_params["outcome"],
            dry_run=self.dry_run,
        )

    # ── Main run loop ─────────────────────────────────────────────────────

    async def run(self) -> None:
        """Start the sweeper bot (async entry point)."""
        logger.info("=" * 60)
        logger.info("SWEEPER BOT starting")
        logger.info("  Markets : %s", ", ".join(self.market_selections))
        logger.info("  Durations: %s", ", ".join(f"{d}min" for d in self.durations))
        logger.info("  Price threshold: %.2f", self.price_threshold)
        logger.info("  Dry-run : %s", self.dry_run)
        logger.info("=" * 60)

        self.running = True

        # Build initial slugs for current + next windows across all durations
        initial_slugs = self._initial_slugs()
        if not initial_slugs:
            logger.error("No initial slugs generated – cannot start.")
            return

        logger.info("Initial slugs (%d): %s", len(initial_slugs), ", ".join(initial_slugs))

        # Create the single shared monitor (verbose=False to reduce noise)
        # keep_alive=True ensures the WS stays open between market windows
        self.monitor = MultiEventMonitor(
            event_slugs=initial_slugs,
            output_file=self.output_file,
            ws_url=self.ws_url,
            check_interval=DEFAULT_MARKET_STATUS_CHECK_INTERVAL,
            verbose=False,
            keep_alive=True,
        )

        # Register strategy callback
        self.monitor.register_tick_size_callback(self._on_tick_size_change)
        logger.info("WebSocket market monitoring enabled (tick_size_change callback registered)")

        # Spawn background tasks
        tasks: list[asyncio.Task] = []

        # One subscription-manager per duration
        for dur in self.durations:
            tasks.append(asyncio.create_task(self._manage_subscriptions_for_duration(dur)))

        # Periodic slug summary
        tasks.append(asyncio.create_task(self._log_tracked_slugs()))

        try:
            await self.monitor.run()
        except KeyboardInterrupt:
            logger.info("Sweeper bot stopped by user")
        finally:
            self.running = False
            for t in tasks:
                t.cancel()
            await asyncio.gather(*tasks, return_exceptions=True)
            logger.info("Sweeper bot stopped")

    def run_sync(self) -> None:
        """Start the sweeper bot (blocking, synchronous wrapper)."""
        try:
            asyncio.run(self.run())
        except KeyboardInterrupt:
            logger.info("Sweeper bot stopped by user")
            self.running = False
