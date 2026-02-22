"""Continuous monitor for recurring crypto up/down markets (5-min, 15-min, etc.)."""

import asyncio
from typing import Optional
import time

from .multi_event_monitor import MultiEventMonitor
from ..markets.fifteen_min import (
    get_market_slug,
    get_current_interval_utc,
    get_next_interval_utc,
    MarketSelection,
    SUPPORTED_DURATIONS,
)
from ..logging_config import get_logger
from ..utils.slug_helpers import slugs_for_timestamp

logger = get_logger(__name__)

# Time constants for market management
# Grace period after market end before removal (seconds)
GRACE_PERIOD_SECONDS = 5 * 60

# Default check interval for continuous monitor (seconds)
# How often to check for new markets and remove ended ones
DEFAULT_CONTINUOUS_CHECK_INTERVAL = 30

# Default market status check interval for MultiEventMonitor (seconds)
# How often to check if markets are still active
DEFAULT_MARKET_STATUS_CHECK_INTERVAL = 60


class ContinuousCryptoMonitor:
    """Monitor that continuously tracks current and upcoming crypto markets at any duration."""

    def __init__(
        self,
        market_selections: list[MarketSelection],
        duration_minutes: int = 15,
        output_file: str | None = None,
        ws_url: Optional[str] = None,
        check_interval: int = DEFAULT_CONTINUOUS_CHECK_INTERVAL,
        market_events_file: str = "market_events.csv",
    ):
        """
        Initialize the continuous crypto market monitor.

        Args:
            market_selections: List of crypto assets to monitor (e.g., ["BTC", "ETH"])
            duration_minutes: Market duration in minutes (5 or 15, default 15)
            output_file: CSV file to save bid data
            ws_url: Optional WebSocket URL override
            check_interval: How often to check for new markets and remove ended ones (seconds)
            market_events_file: CSV file to save market events

        Raises:
            ValueError: If duration_minutes is not in SUPPORTED_DURATIONS.
        """
        if duration_minutes not in SUPPORTED_DURATIONS:
            raise ValueError(
                f"Unsupported duration: {duration_minutes}m. "
                f"Supported: {sorted(SUPPORTED_DURATIONS)}"
            )

        self.market_selections = market_selections
        self.duration_minutes = duration_minutes
        self.interval_seconds = duration_minutes * 60
        self.output_file = output_file or f"sweeper_analysis_{duration_minutes}min.csv"
        self.ws_url = ws_url
        self.check_interval = check_interval
        self.market_events_file = market_events_file
        self.running = False
        self.monitor: Optional[MultiEventMonitor] = None
        
        # Track which timestamp periods we're monitoring for each market
        self.monitored_timestamps: dict[MarketSelection, set[int]] = {
            selection: set() for selection in market_selections
        }

    def get_slugs_for_timestamp(self, timestamp: int) -> list[str]:
        """Get slugs for a specific interval period for all selected markets.

        Delegates to :func:`src.utils.slug_helpers.slugs_for_timestamp`.
        """
        return slugs_for_timestamp(self.market_selections, self.duration_minutes, timestamp)

    async def manage_subscriptions(self):
        """Periodically check for new markets to subscribe to and old ones to unsubscribe from."""
        while self.running:
            await asyncio.sleep(self.check_interval)
            
            if not self.monitor or not self.monitor.running:
                continue
            
            logger.debug("Checking for new %dm markets to subscribe and old ones to unsubscribe...", self.duration_minutes)
            
            current_timestamp = get_current_interval_utc(self.duration_minutes)
            next_timestamp = get_next_interval_utc(self.duration_minutes)
            current_time = int(time.time())
            
            # Collect slugs to add and remove
            slugs_to_add = []
            slugs_to_remove = []
            
            for selection in self.market_selections:
                # Check if we need to add the next market (proactive subscription)
                if next_timestamp not in self.monitored_timestamps[selection]:
                    try:
                        slug = get_market_slug(selection, self.duration_minutes, next_timestamp)
                        slugs_to_add.append(slug)
                        self.monitored_timestamps[selection].add(next_timestamp)
                        logger.info("Will add next %dm market for %s: %s", self.duration_minutes, selection, slug)
                    except ValueError as e:
                        logger.error("Failed to generate next slug for %s: %s", selection, e)
                
                # Check if we need to add the current market (if not already added)
                if current_timestamp not in self.monitored_timestamps[selection]:
                    try:
                        slug = get_market_slug(selection, self.duration_minutes, current_timestamp)
                        slugs_to_add.append(slug)
                        self.monitored_timestamps[selection].add(current_timestamp)
                        logger.info("Will add current %dm market for %s: %s", self.duration_minutes, selection, slug)
                    except ValueError as e:
                        logger.error("Failed to generate current slug for %s: %s", selection, e)
                
                # Check for old markets that have ended and should be removed
                timestamps_to_remove = []
                for timestamp in self.monitored_timestamps[selection]:
                    # Markets that ended more than grace period ago should be removed
                    market_end_time = timestamp + self.interval_seconds
                    if current_time > market_end_time + GRACE_PERIOD_SECONDS:
                        try:
                            slug = get_market_slug(selection, self.duration_minutes, timestamp)
                            # Check if this market is actually inactive
                            if slug in self.monitor.market_active and not self.monitor.market_active[slug]:
                                slugs_to_remove.append(slug)
                                timestamps_to_remove.append(timestamp)
                                logger.info("Will remove ended %dm market for %s: %s", self.duration_minutes, selection, slug)
                        except ValueError as e:
                            logger.error("Failed to generate slug for removal %s: %s", selection, e)
                
                # Remove timestamps from our tracking
                for timestamp in timestamps_to_remove:
                    self.monitored_timestamps[selection].discard(timestamp)
            
            # Add new markets
            if slugs_to_add:
                await self.monitor.add_markets(slugs_to_add)
            
            # Remove old markets
            if slugs_to_remove:
                await self.monitor.remove_markets(slugs_to_remove)

    async def run(self):
        """Run the continuous monitor."""
        logger.info(
            "Starting continuous %d-minute monitor for: %s",
            self.duration_minutes,
            ", ".join(self.market_selections),
        )
        
        self.running = True
        
        # Get slugs for current AND next periods
        current_timestamp = get_current_interval_utc(self.duration_minutes)
        next_timestamp = get_next_interval_utc(self.duration_minutes)
        
        initial_slugs = []
        
        # Add current period markets
        current_slugs = self.get_slugs_for_timestamp(current_timestamp)
        for selection in self.market_selections:
            self.monitored_timestamps[selection].add(current_timestamp)
        initial_slugs.extend(current_slugs)
        
        # Add next period markets (proactive subscription)
        next_slugs = self.get_slugs_for_timestamp(next_timestamp)
        for selection in self.market_selections:
            self.monitored_timestamps[selection].add(next_timestamp)
        initial_slugs.extend(next_slugs)
        
        if not initial_slugs:
            logger.error("No valid slugs generated. Cannot start monitoring.")
            return
        
        logger.info(
            "Initial monitoring setup for current and next %d-minute markets: %s",
            self.duration_minutes,
            ", ".join(initial_slugs),
        )
        
        # Create monitor that will run continuously
        self.monitor = MultiEventMonitor(
            event_slugs=initial_slugs,
            output_file=self.output_file,
            ws_url=self.ws_url,
            check_interval=DEFAULT_MARKET_STATUS_CHECK_INTERVAL,
            market_events_file=self.market_events_file,
        )
        
        # Start subscription management task
        subscription_task = asyncio.create_task(self.manage_subscriptions())
        
        try:
            # Run the monitor (it will keep running until manually stopped)
            await self.monitor.run()
        except KeyboardInterrupt:
            logger.info("Continuous monitor stopped by user")
        finally:
            self.running = False
            subscription_task.cancel()
            try:
                await subscription_task
            except asyncio.CancelledError:
                pass
        
        logger.info("Continuous monitor stopped")

    def run_sync(self):
        """Run the monitor synchronously (blocking)."""
        try:
            asyncio.run(self.run())
        except KeyboardInterrupt:
            logger.info("Continuous monitor stopped by user")
            self.running = False


# Backward-compatibility alias
ContinuousFifteenMinMonitor = ContinuousCryptoMonitor
