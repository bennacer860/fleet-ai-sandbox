"""Multi-event WebSocket monitor for Polymarket markets."""

import asyncio
import csv
import json
from datetime import datetime
from typing import Any, Callable, Optional
import websockets

from ..config import GAMMA_API
from ..gamma_client import (
    fetch_event_by_slug,
    get_market_token_ids,
    is_market_ended,
    get_winning_token_id,
    get_outcomes,
)
from ..logging_config import get_logger
from ..utils.timestamps import get_timestamps, format_slug_with_est_time

logger = get_logger(__name__)

# Type alias for tick-size-change callbacks.
# Signature: (slug, asset_id, old_tick_size, new_tick_size, timestamp_ms) -> None
TickSizeChangeCallback = Callable[[str, str, str, str, int], None]

# WebSocket endpoint for Polymarket CLOB
WS_URL = "wss://ws-subscriptions-clob.polymarket.com/ws/market"

# Target price level to monitor
TARGET_PRICE = 0.999

# Price comparison tolerance for floating point comparison
# Used when checking if a price matches TARGET_PRICE
PRICE_TOLERANCE = 0.0001

# Maximum depth of the order book to process
MAX_ORDERBOOK_DEPTH = 5

# Maximum depth of bids and asks to display
MAX_DISPLAY_DEPTH = 5

# Default market status check interval (seconds)
# How often to check if markets are still active
DEFAULT_CHECK_INTERVAL = 60

# Time window for considering ticker change as "recent" (milliseconds)
# Used to identify sweeper activity after ticker changes
TICKER_CHANGE_WINDOW_MS = 5000  # 5 seconds

# Separator line length for console output
SEPARATOR_LENGTH = 60


class MultiEventMonitor:
    """Monitor orderbook updates for multiple event slugs simultaneously."""

    def __init__(
        self,
        event_slugs: list[str],
        output_file: str = "sweeper_analysis.csv",
        ws_url: Optional[str] = None,
        check_interval: int = DEFAULT_CHECK_INTERVAL,
        market_events_file: Optional[str] = None,  # Deprecated, kept for backward compatibility
        verbose: bool = True,
        keep_alive: bool = False,
    ):
        """
        Initialize the multi-event monitor.

        Args:
            event_slugs: List of event slugs to monitor
            output_file: CSV file to save all events (bids, asks, market events) - unified format
            ws_url: Optional WebSocket URL override
            check_interval: How often to check if markets are still active (seconds)
            market_events_file: Deprecated - kept for backward compatibility, ignored
            verbose: If True, print detailed order-book and event data to stdout (default True)
            keep_alive: If True, do **not** close the WebSocket when all current
                markets have ended.  Used by continuous monitors / sweeper bot
                where new markets are added dynamically.
        """
        self.event_slugs = event_slugs
        self.output_file = output_file
        self.ws_url = ws_url or WS_URL
        self.check_interval = check_interval
        self.verbose = verbose
        self.keep_alive = keep_alive
        
        # Track token IDs and market status
        self.token_ids: dict[str, list[str]] = {}  # slug -> [token_ids]
        self.market_active: dict[str, bool] = {}  # slug -> is_active
        self.slug_by_token: dict[str, str] = {}  # token_id -> slug
        
        # Track condition IDs for cross-referencing with wallet trades
        self.condition_ids: dict[str, str] = {}  # slug -> condition_id
        self.condition_by_token: dict[str, str] = {}  # token_id -> condition_id
        self.raw_slugs: dict[str, str] = {}  # formatted_slug -> raw_slug (preserves timestamp)
        
        # Track bid/ask data
        self.previous_sizes = {}  # Track previous sizes at each price level and side
        
        # Track winning tokens for sweeper analysis
        self.winning_tokens: dict[str, str] = {}  # slug -> winning_token_id
        self.token_outcomes: dict[str, str] = {}  # token_id -> outcome label (e.g., "Up", "Down")
        self.last_ticker_change: dict[str, int] = {}  # token_id -> timestamp_ms of last ticker change
        
        # Unified CSV file handle
        self.csv_file = None
        self.csv_writer = None
        
        # WebSocket connection
        self.websocket = None
        self.running = False
        
        # Tick-size-change callbacks (external hooks, e.g. sweeper strategy)
        self._tick_size_callbacks: list[TickSizeChangeCallback] = []

    def register_tick_size_callback(self, callback: TickSizeChangeCallback) -> None:
        """Register a callback invoked on every tick_size_change event.

        Args:
            callback: Callable(slug, asset_id, old_tick_size, new_tick_size, timestamp_ms)
        """
        self._tick_size_callbacks.append(callback)

    def setup_csv(self):
        """Setup unified CSV file with headers for sweeper analysis."""
        self.csv_file = open(self.output_file, "a", newline="")
        self.csv_writer = csv.writer(self.csv_file)

        # Check if the file is empty to write headers
        if self.csv_file.tell() == 0:
            self.csv_writer.writerow([
                "event_slug",              # Formatted slug with EST date+time (e.g., "btc-15min-up-or-down-2026-02-20-16:15")
                "timestamp_ms",
                "timestamp_iso",
                "timestamp_est",
                "event_type",              # "bid", "ask", "last_trade_price", "tick_size_change", "market_resolved", "market_open", "error"
                "price",                   # For bids/asks/last_trade_price
                "size",                    # For bids/asks/last_trade_price
                "size_change",             # For bids/asks only, empty for other events
                "side",                    # "BID" or "ASK" or "BUY" or "SELL"
                "best_bid",                # Best bid at time of event
                "best_ask",                # Best ask at time of event
                "is_winning_token",        # True if this is the winning token (for bids/asks after resolution)
                "outcome",                 # Outcome label (e.g., "Up", "Down", "Yes", "No")
                "time_since_ticker_change_ms",  # Milliseconds since last tick_size_change
                "ticker_changed_recently",      # True if ticker changed within TICKER_CHANGE_WINDOW_MS
                "old_tick_size",           # For tick_size_change events
                "new_tick_size",           # For tick_size_change events
                "market_resolved",          # True if market has resolved
                "error_message",           # For error events
                # --- Long-value columns at the end for readability ---
                "raw_slug",                # Original API slug (e.g., "btc-updown-15m-1771550145") for reliable joins
                "condition_id",            # Market condition ID — primary key for cross-referencing wallet trades
                "token_id",                # Token ID (long hex string)
            ])
            self.csv_file.flush()
        logger.info("Unified CSV output initialized (append mode): %s", self.output_file)

    def close_csv(self):
        """Close CSV file."""
        if self.csv_file:
            self.csv_file.close()

    async def fetch_token_ids_for_slug(self, slug: str) -> list[str]:
        """
        Get CLOB token IDs for a market slug and track outcomes.
        
        Args:
            slug: Event slug
            
        Returns:
            List of token IDs for the market
        """
        try:
            event = fetch_event_by_slug(slug)
            if not event:
                logger.error("Failed to fetch event for slug: %s", slug)
                return []
            
            markets = event.get("markets", [])
            if not markets:
                logger.error("No markets found for slug: %s", slug)
                return []
            
            # Get token IDs from the first market
            market = markets[0]
            token_ids = get_market_token_ids(market)
            outcomes = get_outcomes(market)
            
            if not token_ids:
                logger.error("No token IDs found for slug: %s", slug)
                return []
            
            # Extract and store condition_id for cross-referencing with wallet trades
            condition_id = market.get("conditionId", market.get("condition_id", ""))
            if condition_id:
                self.condition_ids[slug] = condition_id
                for token_id in token_ids:
                    self.condition_by_token[token_id] = condition_id
                logger.info("Stored condition_id for %s: %s", slug, condition_id)
            else:
                logger.warning("No condition_id found for slug: %s", slug)
            
            # Store raw slug for later CSV output
            self.raw_slugs[slug] = slug
            
            # Track outcomes for each token
            for i, token_id in enumerate(token_ids):
                if i < len(outcomes):
                    self.token_outcomes[token_id] = outcomes[i]
            
            logger.info("Found %d token IDs for slug %s: %s (outcomes: %s, condition_id: %s)", 
                       len(token_ids), slug, token_ids, outcomes, condition_id)
            return token_ids
            
        except Exception as e:
            logger.error("Error fetching token IDs for slug %s: %s", slug, e)
            return []

    async def initialize_markets(self):
        """Initialize all markets by fetching their token IDs."""
        logger.info("Initializing %d markets...", len(self.event_slugs))
        
        for slug in self.event_slugs:
            try:
                token_ids = await self.fetch_token_ids_for_slug(slug)
                if token_ids:
                    self.token_ids[slug] = token_ids
                    self.market_active[slug] = True
                    
                    # Map token IDs to slugs for reverse lookup
                    for token_id in token_ids:
                        self.slug_by_token[token_id] = slug
                    
                    # Log market_open event for each token
                    for token_id in token_ids:
                        self.log_market_event(
                            slug=slug,
                            event_type="market_open",
                            asset_id=token_id
                        )
                        
                    logger.info("Initialized market: %s (active)", slug)
                else:
                    logger.warning("Failed to initialize market: %s", slug)
                    self.market_active[slug] = False
                    # Log error event
                    self.log_market_event(
                        slug=slug,
                        event_type="error",
                        error_message="Failed to fetch token IDs for market"
                    )
            except Exception as e:
                logger.error("Error initializing market %s: %s", slug, e)
                self.market_active[slug] = False
                # Log error event
                self.log_market_event(
                    slug=slug,
                    event_type="error",
                    error_message=f"Error initializing market: {str(e)}"
                )
        
        # Check if any markets were successfully initialized
        active_count = sum(1 for active in self.market_active.values() if active)
        if active_count == 0:
            logger.error("No markets successfully initialized. Cannot start monitoring.")
            return False
        
        logger.info("Successfully initialized %d/%d markets", active_count, len(self.event_slugs))
        return True

    async def add_markets(self, new_slugs: list[str]):
        """
        Dynamically add new markets to monitor.
        
        Args:
            new_slugs: List of new event slugs to add
        """
        if not self.websocket or not self.running:
            logger.warning("Cannot add markets: WebSocket not running")
            return
        
        logger.info("Adding %d new markets to monitor", len(new_slugs))
        
        new_token_ids = []
        for slug in new_slugs:
            # Skip if already monitoring
            if slug in self.token_ids:
                logger.debug("Already monitoring %s, skipping", slug)
                continue
            
            try:
                # Fetch token IDs for new slug
                token_ids = await self.fetch_token_ids_for_slug(slug)
                if token_ids:
                    self.token_ids[slug] = token_ids
                    self.market_active[slug] = True
                    self.event_slugs.append(slug)
                    
                    # Map token IDs to slugs
                    for token_id in token_ids:
                        self.slug_by_token[token_id] = slug
                    
                    # Log market_open event for each token
                    for token_id in token_ids:
                        self.log_market_event(
                            slug=slug,
                            event_type="market_open",
                            asset_id=token_id
                        )
                    
                    new_token_ids.extend(token_ids)
                    logger.info("Added market: %s with %d tokens", slug, len(token_ids))
                else:
                    logger.warning("Failed to add market: %s", slug)
                    # Log error event
                    self.log_market_event(
                        slug=slug,
                        event_type="error",
                        error_message="Failed to fetch token IDs for new market"
                    )
            except Exception as e:
                logger.error("Error adding market %s: %s", slug, e)
                # Log error event
                self.log_market_event(
                    slug=slug,
                    event_type="error",
                    error_message=f"Error adding market: {str(e)}"
                )
        
        # Subscribe to new token IDs
        if new_token_ids:
            try:
                # Note: 'assets_ids' field name is from Polymarket WebSocket API
                subscribe_msg = {
                    "type": "subscribe",
                    "assets_ids": new_token_ids,
                    "custom_feature_enabled": False
                }
                await self.websocket.send(json.dumps(subscribe_msg))
                logger.info("Subscribed to %d new token IDs", len(new_token_ids))
            except Exception as e:
                logger.error("Error subscribing to new markets: %s", e)
                # Log error for each new slug
                for slug in new_slugs:
                    if slug in self.token_ids:
                        self.log_market_event(
                            slug=slug,
                            event_type="error",
                            error_message=f"Error subscribing to market: {str(e)}"
                        )
    
    async def remove_markets(self, slugs_to_remove: list[str]):
        """
        Dynamically remove markets from monitoring.
        
        Args:
            slugs_to_remove: List of event slugs to remove
        """
        if not self.websocket or not self.running:
            logger.warning("Cannot remove markets: WebSocket not running")
            return
        
        logger.info("Removing %d markets from monitor", len(slugs_to_remove))
        
        token_ids_to_unsubscribe = []
        for slug in slugs_to_remove:
            if slug not in self.token_ids:
                logger.debug("Market %s not in monitor, skipping", slug)
                continue
            
            # Get token IDs to unsubscribe
            token_ids = self.token_ids[slug]
            token_ids_to_unsubscribe.extend(token_ids)
            
            # Remove from tracking
            for token_id in token_ids:
                self.slug_by_token.pop(token_id, None)
            
            self.token_ids.pop(slug, None)
            self.market_active.pop(slug, None)
            if slug in self.event_slugs:
                self.event_slugs.remove(slug)
            
            logger.info("Removed market: %s", slug)
        
        # Unsubscribe from token IDs
        if token_ids_to_unsubscribe:
            try:
                # Note: 'assets_ids' field name is from Polymarket WebSocket API
                unsubscribe_msg = {
                    "type": "unsubscribe",
                    "assets_ids": token_ids_to_unsubscribe,
                }
                await self.websocket.send(json.dumps(unsubscribe_msg))
                logger.info("Unsubscribed from %d token IDs", len(token_ids_to_unsubscribe))
            except Exception as e:
                logger.error("Error unsubscribing from markets: %s", e)

    async def check_market_status(self):
        """Periodically check if markets are still active and close websocket if all ended."""
        while self.running:
            await asyncio.sleep(self.check_interval)
            
            logger.debug("Checking market status for %d markets", len(self.event_slugs))
            
            for slug in self.event_slugs:
                if not self.market_active.get(slug, False):
                    continue  # Skip already inactive markets
                
                try:
                    event = fetch_event_by_slug(slug)
                    if not event:
                        logger.warning("Failed to fetch event for status check: %s", slug)
                        continue
                    
                    markets = event.get("markets", [])
                    if not markets:
                        logger.warning("No markets found for status check: %s", slug)
                        continue
                    
                    market = markets[0]
                    if is_market_ended(market):
                        logger.info("Market %s has ended. Marking as inactive.", slug)
                        self.market_active[slug] = False
                        
                        # Identify and track winning token
                        winning_token_id = get_winning_token_id(market)
                        if winning_token_id:
                            self.winning_tokens[slug] = winning_token_id
                            logger.info("Winning token for %s: %s", slug, winning_token_id)
                        
                        # Log market_resolved event for each token in this market
                        token_ids = self.token_ids.get(slug, [])
                        for token_id in token_ids:
                            self.log_unified_event(
                                slug=slug,
                                event_type="market_resolved",
                                token_id=token_id,
                                market_resolved=True
                            )
                        
                except Exception as e:
                    logger.error("Error checking market status for %s: %s", slug, e)
                    # Log error event
                    self.log_market_event(
                        slug=slug,
                        event_type="error",
                        error_message=f"Error checking market status: {str(e)}"
                    )
            
            # Check if all markets are inactive
            active_count = sum(1 for active in self.market_active.values() if active)
            if active_count == 0:
                if self.keep_alive:
                    logger.info(
                        "All current markets have ended but keep_alive=True – "
                        "waiting for new markets to be added."
                    )
                else:
                    logger.info("All markets have ended. Closing WebSocket connection.")
                    self.running = False
                    if self.websocket:
                        await self.websocket.close()
                    break
            else:
                logger.debug("%d/%d markets still active", active_count, len(self.event_slugs))

    def _get_timestamps(self) -> tuple[int, str, str]:
        """Get current timestamps in various formats.

        Delegates to :func:`src.utils.timestamps.get_timestamps`.

        Returns:
            Tuple of (timestamp_ms, timestamp_iso, timestamp_est)
        """
        return get_timestamps()
    
    def _format_slug_with_est_time(self, slug: str, timestamp_ms: Optional[int] = None) -> str:
        """Format event slug with EST date and time.

        Delegates to :func:`src.utils.timestamps.format_slug_with_est_time`.
        """
        return format_slug_with_est_time(slug, timestamp_ms)

    def _process_order_at_target_price(
        self,
        order: dict[str, Any],
        side: str,
        asset_id: str,
        slug: str,
        timestamp_ms: int,
        best_bid: str,
        best_ask: str,
    ) -> None:
        """
        Process a single order (bid or ask) at the target price level.
        
        Args:
            order: Order dict with 'price' and 'size' keys
            side: "BID" or "ASK"
            asset_id: Asset/token ID
            slug: Event slug
            timestamp_ms: Timestamp in milliseconds from message
            best_bid: Best bid price as string
            best_ask: Best ask price as string
        """
        try:
            price = float(order.get("price", 0))
            size = float(order.get("size", 0))
            
            # Check if this order is at our target price (>= 0.99 to catch sweepers and resolution)
            if price < 0.99:
                return
            
            # Calculate size change from previous
            cache_key = f"{asset_id}_{price}_{side}"
            previous_size = self.previous_sizes.get(cache_key, 0.0)
            size_change = size - previous_size
            
            # Only log if this is a new entry or increased size
            if size_change > 0:
                # Get current timestamps
                current_timestamp_ms, timestamp_iso, timestamp_est = self._get_timestamps()
                
                # Use message timestamp if available, otherwise current time
                event_timestamp_ms = timestamp_ms if timestamp_ms else current_timestamp_ms
                
                # Calculate sweeper analysis fields
                is_winning_token = (asset_id == self.winning_tokens.get(slug, ""))
                outcome = self.token_outcomes.get(asset_id, "")
                market_resolved = not self.market_active.get(slug, True)
                
                # Calculate time since ticker change
                last_ticker_change = self.last_ticker_change.get(asset_id, 0)
                time_since_ticker_change_ms = event_timestamp_ms - last_ticker_change if last_ticker_change > 0 else -1
                ticker_changed_recently = (time_since_ticker_change_ms >= 0 and 
                                          time_since_ticker_change_ms < TICKER_CHANGE_WINDOW_MS)
                
                # Format slug to include the hour in 24-hour format for logging
                now = datetime.utcnow()
                formatted_slug = f"{slug}-{now.strftime('%H')}:00"
                
                # Log to console with sweeper context
                sweeper_indicator = " [SWEEPER CANDIDATE]" if (is_winning_token and ticker_changed_recently) else ""
                logger.info(
                    "[%s] New %s at %.3f for %s (slug: %s): size=%.2f, change=+%.2f (best_bid=%s, best_ask=%s)%s",
                    timestamp_iso,
                    side,
                    price,
                    asset_id,
                    formatted_slug,
                    size,
                    size_change,
                    best_bid,
                    best_ask,
                    sweeper_indicator,
                )
                
                # Format slug with EST time using the event timestamp
                formatted_slug = self._format_slug_with_est_time(slug, event_timestamp_ms)
                
                # Write to unified CSV
                if self.csv_writer:
                    try:
                        # Placeholders for non-order/market fields
                        old_tick_size = ""
                        new_tick_size = ""
                        error_message = ""
                        
                        # Look up condition_id and raw slug for cross-referencing
                        condition_id = self.condition_by_token.get(asset_id, "")
                        raw_slug = self.raw_slugs.get(slug, slug)
                        
                        self.csv_writer.writerow([
                            formatted_slug,      # event_slug
                            event_timestamp_ms,
                            timestamp_iso,
                            timestamp_est,
                            side.lower(),         # event_type (bid/ask)
                            price,
                            size,
                            size_change,
                            side.upper(),         # side
                            best_bid,
                            best_ask,
                            str(is_winning_token).lower(),
                            outcome,
                            time_since_ticker_change_ms,
                            str(ticker_changed_recently).lower(),
                            old_tick_size,
                            new_tick_size,
                            str(market_resolved).lower(),
                            error_message,
                            # --- Long-value columns ---
                            raw_slug,
                            condition_id,
                            asset_id,             # token_id
                        ])
                        self.csv_file.flush()
                    except Exception as e:
                        logger.error("Failed to write to CSV: %s", e)
                else:
                    logger.warning("CSV Writer is None! Cannot write order update.")
            
            # Update previous size
            self.previous_sizes[cache_key] = size
            
        except (ValueError, KeyError) as e:
            logger.error("Error processing %s: %s", side.lower(), e)

    def process_book_update(self, data: dict[str, Any]):
        """
        Process a book update message.

        Args:
            data: WebSocket message data with format:
                {
                  "event_type": "book",
                  "asset_id": "...",
                  "market": "...",
                  "bids": [{"price": ".48", "size": "30"}, ...],
                  "asks": [{"price": ".52", "size": "25"}, ...],
                  "timestamp": "123456789000"
                }
        """
        if not isinstance(data, dict):
            logger.debug("Unexpected message format: %s", data)
            return

        # Extract asset ID to determine which market this is for
        asset_id = data.get("asset_id")
        if not asset_id:
            logger.debug("No asset_id in message")
            return
        
        # Look up the slug for this token
        slug = self.slug_by_token.get(asset_id)
        if not slug:
            logger.debug("Unknown asset_id: %s", asset_id)
            return
        
        # Check if this market is still active
        if not self.market_active.get(slug, False):
            logger.debug("Ignoring update for inactive market: %s", slug)
            return

        # Extract basic info
        try:
            timestamp_raw = data.get("timestamp")
            timestamp_ms = int(timestamp_raw) if timestamp_raw is not None else int(datetime.utcnow().timestamp() * 1000)
        except (ValueError, TypeError):
            timestamp_ms = int(datetime.utcnow().timestamp() * 1000)
        
        # Extract bids and asks arrays
        raw_bids = data.get("bids", [])
        raw_asks = data.get("asks", [])
        
        # Sort bids descending (highest price first) and asks ascending (lowest price first) before limiting
        bids = sorted(raw_bids, key=lambda x: float(x["price"]), reverse=True)[:MAX_ORDERBOOK_DEPTH]
        asks = sorted(raw_asks, key=lambda x: float(x["price"]))[:MAX_ORDERBOOK_DEPTH]
        
        # Calculate best_bid and best_ask
        best_bid = bids[0]["price"] if bids else "N/A"
        best_ask = asks[0]["price"] if asks else "N/A"
        
        # Process bids at target price
        for bid in bids:
            self._process_order_at_target_price(
                bid, "BID", asset_id, slug, timestamp_ms, best_bid, best_ask
            )
        
        # Process asks at target price
        for ask in asks:
            self._process_order_at_target_price(
                ask, "ASK", asset_id, slug, timestamp_ms, best_bid, best_ask
            )
        
        # Limit the depth of bids and asks for display
        bids_display = sorted(data.get("bids", []), key=lambda x: float(x["price"]), reverse=True)[:MAX_DISPLAY_DEPTH]
        asks_display = sorted(data.get("asks", []), key=lambda x: float(x["price"]))[:MAX_DISPLAY_DEPTH]

        # Print the 5 highest bids and 5 lowest asks as strings in the desired format
        if self.verbose:
            print(f"Top 5 Bids: {[f'price: {bid['price']}, size: {bid['size']}' for bid in bids_display]}")
            print(f"Top 5 Asks: {[f'price: {ask['price']}, size: {ask['size']}' for ask in asks_display]}")

    def log_unified_event(
        self,
        slug: str,
        event_type: str,
        token_id: str = "",
        price: Optional[float] = None,
        size: Optional[float] = None,
        size_change: Optional[float] = None,
        side: str = "",
        best_bid: str = "",
        best_ask: str = "",
        old_tick_size: str = "",
        new_tick_size: str = "",
        error_message: str = "",
        market_resolved: bool = False,
    ):
        """
        Log any event to the unified CSV file.
        
        Args:
            slug: Event slug
            event_type: Type of event ("bid", "ask", "tick_size_change", "market_resolved", "market_open", "error")
            token_id: Token ID, optional
            price: Price (for bids/asks), optional
            size: Size (for bids/asks), optional
            size_change: Size change (for bids/asks), optional
            side: "BID" or "ASK" (for bids/asks), optional
            best_bid: Best bid price, optional
            best_ask: Best ask price, optional
            old_tick_size: Old tick size (for tick_size_change), optional
            new_tick_size: New tick size (for tick_size_change), optional
            error_message: Error message (for errors), optional
            market_resolved: Whether market has resolved, optional
        """
        # Get current timestamps
        timestamp_ms, timestamp_iso, timestamp_est = self._get_timestamps()
        
        # Calculate sweeper analysis fields
        is_winning_token = (token_id == self.winning_tokens.get(slug, "")) if token_id else False
        outcome = self.token_outcomes.get(token_id, "") if token_id else ""
        
        # Calculate time since ticker change
        last_ticker_change = self.last_ticker_change.get(token_id, 0) if token_id else 0
        time_since_ticker_change_ms = timestamp_ms - last_ticker_change if last_ticker_change > 0 else -1
        ticker_changed_recently = (time_since_ticker_change_ms >= 0 and 
                                  time_since_ticker_change_ms < TICKER_CHANGE_WINDOW_MS)
        
        # Log to console
        logger.info(
            "[%s] Event: %s for %s (token: %s)",
            timestamp_iso,
            event_type,
            slug,
            token_id or "N/A",
        )
        
        # Format slug with EST time using the current timestamp
        formatted_slug = self._format_slug_with_est_time(slug, timestamp_ms)
        
        # Look up condition_id and raw slug for cross-referencing
        condition_id_val = self.condition_by_token.get(token_id, "") if token_id else ""
        if not condition_id_val:
            condition_id_val = self.condition_ids.get(slug, "")
        raw_slug = self.raw_slugs.get(slug, slug)
        
        # Write to unified CSV (long-value columns at the end for readability)
        if self.csv_writer:
            self.csv_writer.writerow([
                formatted_slug,  # event_slug
                timestamp_ms,
                timestamp_iso,
                timestamp_est,
                event_type,
                price if price is not None else "",
                size if size is not None else "",
                size_change if size_change is not None else "",
                side if side else "",
                best_bid if best_bid and best_bid != "N/A" else "",
                best_ask if best_ask and best_ask != "N/A" else "",
                str(is_winning_token).lower() if token_id else "",
                outcome if outcome else "",
                time_since_ticker_change_ms if time_since_ticker_change_ms >= 0 else "",
                str(ticker_changed_recently).lower() if token_id else "",
                old_tick_size if old_tick_size else "",
                new_tick_size if new_tick_size else "",
                str(market_resolved).lower(),
                error_message if error_message else "",
                # --- Long-value columns ---
                raw_slug,        # Original API slug for reliable joins
                condition_id_val,  # condition_id for cross-referencing wallet trades
                token_id if token_id else "",
            ])
            self.csv_file.flush()
            logger.debug("Event saved to unified CSV: %s", self.output_file)
    
    def log_market_event(
        self,
        slug: str,
        event_type: str,
        asset_id: str = "",
        old_tick_size: str = "",
        new_tick_size: str = "",
        error_message: str = ""
    ):
        """
        Legacy method for logging market events. Now calls log_unified_event.
        
        Args:
            slug: Event slug
            event_type: Type of event (market_open, market_resolved, tick_size_change, error)
            asset_id: Asset ID (token ID), optional
            old_tick_size: Old tick size for tick_size_change events, optional
            new_tick_size: New tick size for tick_size_change events, optional
            error_message: Error message for error events, optional
        """
        # Determine if market is resolved
        market_resolved = (event_type == "market_resolved" or 
                          not self.market_active.get(slug, True))
        
        self.log_unified_event(
            slug=slug,
            event_type=event_type,
            token_id=asset_id,
            old_tick_size=old_tick_size,
            new_tick_size=new_tick_size,
            error_message=error_message,
            market_resolved=market_resolved,
        )

    def process_ticker_change(self, data: dict[str, Any]):
        """
        Process a tick_size_change event.
        
        Args:
            data: WebSocket message data for tick_size_change event
        """
        if not isinstance(data, dict):
            logger.debug("Unexpected ticker change message format: %s", data)
            return
        
        # Extract asset ID to determine which market this is for
        asset_id = data.get("asset_id")
        if not asset_id:
            logger.debug("No asset_id in ticker change message")
            return
        
        # Look up the slug for this token
        slug = self.slug_by_token.get(asset_id)
        if not slug:
            logger.debug("Unknown asset_id in ticker change: %s", asset_id)
            return
        
        # Track ticker change timestamp for sweeper analysis
        try:
            timestamp_raw = data.get("timestamp")
            timestamp_ms = int(timestamp_raw) if timestamp_raw is not None else int(datetime.utcnow().timestamp() * 1000)
        except (ValueError, TypeError):
            timestamp_ms = int(datetime.utcnow().timestamp() * 1000)
            
        self.last_ticker_change[asset_id] = timestamp_ms
        
        # Log the tick_size_change event
        old_tick_size = str(data.get("old_tick_size", ""))
        new_tick_size = str(data.get("new_tick_size", ""))
        market_resolved = not self.market_active.get(slug, True)
        self.log_unified_event(
            slug=slug,
            event_type="tick_size_change",
            token_id=asset_id,
            old_tick_size=old_tick_size,
            new_tick_size=new_tick_size,
            market_resolved=market_resolved,
        )

        # Invoke registered callbacks
        for cb in self._tick_size_callbacks:
            try:
                cb(slug, asset_id, old_tick_size, new_tick_size, timestamp_ms)
            except Exception as cb_err:
                logger.error("Tick-size callback error: %s", cb_err)

    def process_last_trade_price(self, data: dict[str, Any]):
        """
        Process a last_trade_price event from the WebSocket.

        This captures the most recent trade executed on the market, including
        the price, size, and side (BUY/SELL).  Tracking these events lets us
        correlate sweeper order-book activity with actual fills and determine
        whether a tracked wallet's trade was a winning position.

        Args:
            data: WebSocket message data for last_trade_price event
        """
        if not isinstance(data, dict):
            logger.debug("Unexpected last_trade_price message format: %s", data)
            return

        asset_id = data.get("asset_id")
        if not asset_id:
            logger.debug("No asset_id in last_trade_price message")
            return

        slug = self.slug_by_token.get(asset_id)
        if not slug:
            logger.debug("Unknown asset_id in last_trade_price: %s", asset_id)
            return

        # Only log for active markets
        if not self.market_active.get(slug, False):
            logger.debug("Ignoring last_trade_price for inactive market: %s", slug)
            return

        price_raw = data.get("price")
        size_raw = data.get("size")
        side = str(data.get("side", "")).upper()  # "BUY" or "SELL"

        try:
            price = float(price_raw) if price_raw is not None else None
        except (ValueError, TypeError):
            price = None

        try:
            size = float(size_raw) if size_raw is not None else None
        except (ValueError, TypeError):
            size = None

        market_resolved = not self.market_active.get(slug, True)
        self.log_unified_event(
            slug=slug,
            event_type="last_trade_price",
            token_id=asset_id,
            price=price,
            size=size,
            side=side,
            market_resolved=market_resolved,
        )

    async def subscribe_and_monitor(self):
        """Connect to WebSocket and monitor book updates for all markets."""
        logger.info("Connecting to %s", self.ws_url)
        logger.info("Monitoring %d event slugs", len(self.event_slugs))
        logger.info("Target price: %.3f", TARGET_PRICE)
        logger.info("-" * SEPARATOR_LENGTH)

        self.setup_csv()

        # Initialize markets and fetch token IDs
        success = await self.initialize_markets()
        if not success:
            logger.error("Failed to initialize markets. Exiting.")
            return

        # Verify we have token IDs before starting
        initial_token_ids = []
        for token_ids in self.token_ids.values():
            initial_token_ids.extend(token_ids)
        
        if not initial_token_ids:
            logger.error("No token IDs to subscribe to. Exiting.")
            return

        logger.info("Subscribing to %d token IDs across %d markets", len(initial_token_ids), len(self.token_ids))

        self.running = True
        
        # Start market status checking task
        status_task = asyncio.create_task(self.check_market_status())

        # Reconnection back-off (seconds): 5 → 10 → 20 → … capped at 60
        _BASE_BACKOFF = 5
        _MAX_BACKOFF = 60
        backoff = _BASE_BACKOFF

        try:
            while self.running:
                try:
                    # Build current token list fresh on each (re)connect so that
                    # dynamically added/removed markets are reflected.
                    all_token_ids = []
                    for token_ids in self.token_ids.values():
                        all_token_ids.extend(token_ids)

                    if not all_token_ids:
                        if self.keep_alive:
                            logger.info("No token IDs yet (keep_alive). Waiting %ds…", backoff)
                            await asyncio.sleep(backoff)
                            continue
                        logger.warning("No token IDs to subscribe to. Waiting before retry...")
                        await asyncio.sleep(5)
                        continue

                    # Polymarket server sends pings every 30s.
                    # We disable client-side pings (ping_interval=None) to avoid "INVALID OPERATION" errors
                    # but keep ping_timeout to ensure we disconnect if the server stops sending pings.
                    async with websockets.connect(self.ws_url, ping_interval=None, ping_timeout=60) as websocket:
                        self.websocket = websocket
                        # Reset backoff on successful connect
                        backoff = _BASE_BACKOFF
                        try:
                            logger.info("WebSocket connected.")

                            # Subscribe to the book channel for all tokens
                            subscribe_msg = {
                                "type": "subscribe",
                                "assets_ids": all_token_ids,
                                "custom_feature_enabled": False
                            }
                            logger.info("Subscription message: %s", json.dumps(subscribe_msg))
                            await websocket.send(json.dumps(subscribe_msg))
                            logger.info("Subscribed to book updates for %d tokens", len(all_token_ids))

                            # Listen for updates
                            async for message in websocket:
                                if not self.running:
                                    break

                                try:
                                    # Check for "INVALID OPERATION" text message which might come from server
                                    if message == "INVALID OPERATION":
                                        logger.debug("Received 'INVALID OPERATION' from server (likely response to ping/frame), dragging on.")
                                        continue

                                    data = json.loads(message)

                                    # Check if the message is a list (empty message)
                                    if isinstance(data, list):
                                        logger.debug("Received empty list message")
                                        continue

                                    # Check if this is a book update
                                    msg_type = data.get("event_type", data.get("type", ""))

                                    if msg_type in ["book"]:
                                        # Extract asset ID to determine which market this is for
                                        asset_id = data.get("asset_id")
                                        if not asset_id:
                                            logger.debug("No asset_id in message")
                                            continue
                                        
                                        # Look up the slug for this token
                                        slug = self.slug_by_token.get(asset_id)
                                        if not slug:
                                            logger.debug("Unknown asset_id: %s", asset_id)
                                            continue
                                        
                                        # Check if this market is still active
                                        if not self.market_active.get(slug, False):
                                            logger.debug("Ignoring update for inactive market: %s", slug)
                                            continue

                                        if self.verbose:
                                            print(f"Detected Event Type: {msg_type} (Book Event) for market slug: {slug}")
                                        self.process_book_update(data)

                                    # Handle tick_size_change event
                                    if msg_type == "tick_size_change":
                                        if self.verbose:
                                            print(f"Tick Size Change Event Detected: {data}")
                                        self.process_ticker_change(data)

                                    # Handle last_trade_price event
                                    if msg_type == "last_trade_price":
                                        asset_id = data.get("asset_id", "")
                                        slug = self.slug_by_token.get(asset_id, "unknown")
                                        if self.verbose:
                                            print(f"Last Trade Price: {data.get('price')} size={data.get('size')} side={data.get('side')} market={slug}")
                                        self.process_last_trade_price(data)

                                except json.JSONDecodeError:
                                    logger.error("Failed to decode message: %s", message)
                                    # Log a single decode error (not market-specific)
                                    self.log_market_event(
                                        slug="N/A",
                                        event_type="error",
                                        error_message=f"Failed to decode WebSocket message: {message[:100]}"
                                    )
                                except Exception as e:
                                    logger.error("Error processing message: %s", e)
                                    # Determine if error is for a specific market
                                    try:
                                        data = json.loads(message) if isinstance(message, str) else message
                                        asset_id = data.get("asset_id", "")
                                        slug = self.slug_by_token.get(asset_id, "N/A")
                                        self.log_market_event(
                                            slug=slug,
                                            event_type="error",
                                            asset_id=asset_id,
                                            error_message=f"Error processing message: {str(e)}"
                                        )
                                    except Exception:
                                        # If we can't determine the market, log once without market
                                        self.log_market_event(
                                            slug="N/A",
                                            event_type="error",
                                            error_message=f"Error processing message: {str(e)}"
                                        )
                        finally:
                            # Clear the reference so concurrent tasks (add_markets,
                            # remove_markets, check_market_status) don't try to send
                            # on a closed connection.
                            self.websocket = None

                except websockets.exceptions.ConnectionClosedOK:
                    # Server closed the connection normally (e.g. market settlement).
                    logger.info("WebSocket closed normally by server.")
                    if self.running:
                        logger.info("Reconnecting in %ds…", backoff)
                        await asyncio.sleep(backoff)
                        backoff = min(backoff * 2, _MAX_BACKOFF)

                except websockets.exceptions.ConnectionClosedError as e:
                    logger.error("WebSocket connection closed unexpectedly: %s", e)
                    if self.running:
                        logger.info("Reconnecting in %ds…", backoff)
                        await asyncio.sleep(backoff)
                        backoff = min(backoff * 2, _MAX_BACKOFF)

                except websockets.exceptions.WebSocketException as e:
                    logger.error("WebSocket error: %s", e)
                    if self.running:
                        logger.info("Reconnecting in %ds…", backoff)
                        await asyncio.sleep(backoff)
                        backoff = min(backoff * 2, _MAX_BACKOFF)

                except Exception as e:
                    logger.error("Unexpected error in WebSocket loop: %s", e)
                    if self.running:
                        logger.info("Reconnecting in %ds…", backoff)
                        await asyncio.sleep(backoff)
                        backoff = min(backoff * 2, _MAX_BACKOFF)

        except asyncio.CancelledError:
            logger.info("Monitoring cancelled")
            
        finally:
            self.running = False
            # Cancel status checking task
            status_task.cancel()
            try:
                await status_task
            except asyncio.CancelledError:
                pass
                
            self.close_csv()

    async def run(self):
        """Run the monitor asynchronously."""
        try:
            await self.subscribe_and_monitor()
        except KeyboardInterrupt:
            logger.info("Monitoring stopped by user")
        finally:
            self.close_csv()

    def run_sync(self):
        """Run the monitor synchronously (blocking)."""
        try:
            asyncio.run(self.run())
        except KeyboardInterrupt:
            logger.info("Monitoring stopped by user")
        finally:
            self.close_csv()
