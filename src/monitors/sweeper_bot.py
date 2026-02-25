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
from ..utils.market_data import get_market_evaluation, get_min_order_size
from ..strategy.order_executor import execute_sweep_order
from ..logging_config import get_logger
from ..utils.slug_helpers import slugs_for_timestamp
from ..utils.timestamps import format_slug_with_est_time
from ..utils.decision_logger import DecisionLogger

logger = get_logger(__name__)

# ANSI Color Codes for console output
C_RED = "\033[91m"
C_RESET = "\033[0m"

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
        decision_log_file: str = "bot_decisions.csv",
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
            decision_log_file: CSV file for detailed strategy decisions.
        """
        self.market_selections = market_selections
        self.durations = durations or sorted(SUPPORTED_DURATIONS)
        self.price_threshold = price_threshold
        self.output_file = output_file
        self.ws_url = ws_url
        self.sub_check_interval = sub_check_interval
        self.dry_run = dry_run
        self.decision_log_file = decision_log_file

        self.decision_logger = DecisionLogger(decision_log_file)

        self.running = False
        self.monitor: Optional[MultiEventMonitor] = None

        # Pre-fetched evaluation data keyed by slug.
        # Populated in the background when each market opens so that
        # _on_tick_size_change can skip the Gamma API HTTP call entirely.
        self._eval_cache: dict[str, dict] = {}

        # Per-duration tracking: {duration -> {selection -> set[timestamp]}}
        self._monitored_ts: dict[int, dict[MarketSelection, set[int]]] = {
            dur: {sel: set() for sel in market_selections}
            for dur in self.durations
        }

    # ── Eval pre-cache ────────────────────────────────────────────────────

    async def _prefetch_eval(self, slug: str) -> None:
        """Fetch and cache market evaluation data for *slug* in the background.

        This runs as a fire-and-forget task right after a market is added so
        that when ``tick_size_change`` fires the strategy can skip the
        synchronous Gamma HTTP call and do a plain dict lookup instead.
        """
        if slug in self._eval_cache:
            return  # already cached
        try:
            # Run the blocking HTTP call in a thread so we don't stall the loop
            eval_data = await asyncio.get_event_loop().run_in_executor(
                None, get_market_evaluation, slug
            )
            if eval_data:
                self._eval_cache[slug] = eval_data
                logger.debug("[CACHE] Pre-fetched eval for %s", slug)
            else:
                logger.warning("[CACHE] Pre-fetch returned no data for %s", slug)
        except Exception:
            logger.exception("[CACHE] Pre-fetch failed for %s", slug)

    def _launch_prefetch(self, slugs: list[str]) -> None:
        """Schedule background prefetch tasks for a list of slugs."""
        loop = asyncio.get_event_loop()
        for slug in slugs:
            loop.create_task(self._prefetch_eval(slug))

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

        # Kick off background pre-fetches so eval data is ready before the
        # first tick_size_change event fires.
        self._launch_prefetch(slugs)
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
                        formatted = self.monitor._format_slug_with_est_time(slug)
                        logger.info("Queuing next %dm market for %s: %s", duration, sel, formatted)
                    except ValueError as exc:
                        logger.error("Slug generation failed (%s/%dm next): %s", sel, duration, exc)

                # Ensure current window is subscribed
                if cur_ts not in tracked:
                    try:
                        slug = get_market_slug(sel, duration, cur_ts)
                        slugs_to_add.append(slug)
                        tracked.add(cur_ts)
                        formatted = self.monitor._format_slug_with_est_time(slug)
                        logger.info("Queuing current %dm market for %s: %s", duration, sel, formatted)
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
                                formatted = self.monitor._format_slug_with_est_time(slug)
                                logger.info("Queuing removal of %dm market for %s: %s", duration, sel, formatted)
                        except ValueError:
                            expired.append(ts)
                for ts in expired:
                    tracked.discard(ts)

            if slugs_to_add:
                await self.monitor.add_markets(slugs_to_add)
                # Pre-fetch eval data for new slugs in the background
                self._launch_prefetch(slugs_to_add)
            if slugs_to_remove:
                await self.monitor.remove_markets(slugs_to_remove)
                # Evict stale cache entries for removed markets
                for slug in slugs_to_remove:
                    self._eval_cache.pop(slug, None)

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
        trigger = f"tick_size:{old_tick_size}->{new_tick_size}"
        formatted_slug = format_slug_with_est_time(slug, timestamp_ms)

        # ── Guard: ignore spurious same→same tick_size events ─────────────
        if old_tick_size == new_tick_size:
            logger.debug(
                "[TICK_SIZE] Ignoring duplicate signal %s->%s for %s",
                old_tick_size, new_tick_size, slug,
            )
            self.decision_logger.log_decision(
                event_slug=slug,
                formatted_slug=formatted_slug,
                trigger=trigger,
                decision="SKIP",
                reason=f"Duplicate signal (tick_size unchanged at {new_tick_size})",
            )
            return
        # ──────────────────────────────────────────────────────────────────

        logger.info(
            "%s[TICK_SIZE] Processing callback for %s: %s -> %s (token=%s…)%s",
            C_RED,
            formatted_slug,
            old_tick_size,
            new_tick_size,
            asset_id[:20],
            C_RESET,
        )

        # 1. Market evaluation — use the pre-fetched cache when available
        #    so we skip the synchronous Gamma HTTP call in the hot path.
        eval_data = self._eval_cache.get(slug)
        if eval_data:
            logger.debug("[CACHE] Using pre-fetched eval for %s", slug)
            # Refresh the raw_prices_compact with current cached prices
            if "outcomes" in eval_data and "prices" in eval_data:
                eval_data["raw_prices_compact"] = "|".join(
                    [f"{o}:{p:.3f}" for o, p in zip(eval_data["outcomes"], eval_data["prices"])]
                )
        else:
            logger.info("[CACHE] Cache miss for %s – fetching live from Gamma", slug)
            eval_data = get_market_evaluation(slug)

        if not eval_data:
            self.decision_logger.log_decision(
                event_slug=slug,
                formatted_slug=formatted_slug,
                trigger=trigger,
                decision="SKIP",
                reason="Market evaluation failed (Gamma API error or missing data)"
            )
            return

        # ── HYBRID EVALUATION: Override with WebSocket prices ───────────
        if self.monitor:
            realtime_updated = False
            prices = eval_data["prices"]
            token_ids = eval_data["token_ids"]
            outcomes = eval_data["outcomes"]
            
            for i, tid in enumerate(token_ids):
                rt_price = self.monitor.get_realtime_price(tid)
                if rt_price is not None:
                    # Log if the difference is significant
                    if abs(rt_price - prices[i]) > 0.05:
                        logger.info(
                            " [RT_PRICE] Overriding %s: Gamma=%.3f -> WS=%.3f",
                            outcomes[i], prices[i], rt_price
                        )
                    prices[i] = rt_price
                    realtime_updated = True
            
            if realtime_updated:
                # Recalculate best outcome
                best_idx = 0
                best_p = prices[0]
                for i, p in enumerate(prices):
                    if p > best_p:
                        best_p = p
                        best_idx = i
                
                eval_data.update({
                    "best_idx": best_idx,
                    "best_price": best_p,
                    "best_outcome": outcomes[best_idx],
                    "best_token_id": token_ids[best_idx],
                    "raw_prices_compact": "|".join([f"{o}:{p:.3f}*" for o, p in zip(outcomes, prices)])
                })
                eval_data["is_realtime"] = True
        # ───────────────────────────────────────────────────────────────

        # 2. Strategy evaluation
        order_params = should_place_sweep_order(
            slug=slug,
            new_tick_size=new_tick_size,
            price_threshold=self.price_threshold,
            eval_data=eval_data
        )

        if order_params is None:
            # This should only happen if tick size is not 0.001
            self.decision_logger.log_decision(
                event_slug=slug,
                formatted_slug=formatted_slug,
                trigger=trigger,
                decision="SKIP",
                reason=f"Not a sweep trigger (tick_size={new_tick_size})",
                raw_prices=eval_data.get("raw_prices_compact", "")
            )
            return

        if order_params.get("skip"):
            self.decision_logger.log_decision(
                event_slug=slug,
                formatted_slug=formatted_slug,
                trigger=trigger,
                decision="SKIP",
                reason=order_params.get("reason", "Threshold not met"),
                best_outcome=eval_data["best_outcome"],
                best_price=eval_data["best_price"],
                threshold=self.price_threshold,
                price_source="WebSocket" if eval_data.get("is_realtime") else "Gamma",
                raw_prices=eval_data.get("raw_prices_compact", "")
            )
            return

        # 3. Decision: PLACE ORDER
        logger.info(
            "[STRATEGY] Sweep signal confirmed for %s – placing order: %s @ %.4f x %.2f",
            formatted_slug,
            order_params["outcome"],
            order_params["price"],
            order_params["size"],
        )

        resp = execute_sweep_order(
            token_id=order_params["token_id"],
            price=order_params["price"],
            size=order_params["size"],
            slug=order_params["slug"],
            outcome=order_params["outcome"],
            dry_run=self.dry_run,
            tick_size=float(new_tick_size),
        )

        # 4. Log trade execution details
        token_id = order_params["token_id"]
        status = "FAILED"
        order_id = ""
        
        # ── DEDUP: execute_sweep_order returns None when already ordered ──
        if resp is None and not self.dry_run:
            self.decision_logger.log_decision(
                event_slug=slug,
                formatted_slug=formatted_slug,
                trigger=trigger,
                decision="SKIP",
                reason="DEDUP: Already ordered this market this session",
                best_outcome=order_params["outcome"],
                best_price=eval_data["best_price"],
                threshold=self.price_threshold,
                price_source="WebSocket" if eval_data.get("is_realtime") else "Gamma",
                raw_prices=eval_data.get("raw_prices_compact", "")
            )
            return
        # ──────────────────────────────────────────────────────────────────

        if resp:
            if resp.get("success"):
                order_id = resp.get("orderId", "unknown")
                status = "SUCCESS"
            else:
                status = f"REJECTED: {resp.get('errorMsg')}"
        elif self.dry_run:
            status = "DRY_RUN"

        # Dedicated Decision Log
        self.decision_logger.log_decision(
            event_slug=slug,
            formatted_slug=formatted_slug,
            trigger=trigger,
            decision="TRADE",
            reason="Signal & Price eligibility met",
            best_outcome=order_params["outcome"],
            best_price=eval_data["best_price"],
            threshold=self.price_threshold,
            limit_price=order_params["price"],
            order_id=order_id if order_id else status,
            price_source="WebSocket" if eval_data.get("is_realtime") else "Gamma",
            raw_prices=eval_data.get("raw_prices_compact", "")
        )

        # Unified CSV Log
        if self.monitor:
            self.monitor.log_unified_event(
                slug=slug,
                event_type="bot_trade",
                token_id=token_id,
                price=order_params["price"],
                size=order_params["size"],
                side="BUY",
                error_message=f"{status} | {order_id}" if order_id else status
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
