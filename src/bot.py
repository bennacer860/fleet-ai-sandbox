"""Main bot orchestrator — wires all components and manages lifecycle.

Replaces ``SweeperBot`` with a fully event-driven architecture.
Components communicate exclusively through the ``EventBus``.
"""

from __future__ import annotations

import asyncio
import os
import time
from typing import Any

from .core.event_bus import EventBus
from .core.events import (
    BookUpdate,
    MarketResolved,
    OrderFill,
    OrderLive,
    OrderStatus,
    OrderTerminal,
    TickSizeChange,
)
from .utils.timestamps import format_slug_with_est_time
from .core.models import OrderIntent
from .execution.order_manager import OrderManager
from .execution.position_tracker import PositionTracker
from .execution.risk_manager import RiskConfig, RiskManager
from .gateway.crypto_ws import CryptoWebSocket
from .gateway.market_ws import MarketWebSocket
from .gateway.rest_client import AsyncRestClient
from .gateway.user_ws import UserWebSocket
from .logging_config import get_logger
from .monitoring.alerting import AlertManager
from .monitoring.dashboard import Dashboard
from .monitoring.health import HealthMonitor
from .monitoring.metrics import Metrics
from .storage.database import init_db
from .storage.persistence import AsyncPersistence
from .utils.market_data import fetch_strike_price, get_market_evaluation
from .clob_client import precache_token_data, get_cached_min_order_size
from .execution.auto_claimer import AutoClaimer
from .markets.fifteen_min import (
    MarketSelection,
    SUPPORTED_DURATIONS,
    detect_duration_from_slug,
    extract_market_end_ts,
    extract_market_from_slug,
    get_current_interval_utc,
    get_market_slug,
    get_next_interval_utc,
)
from .utils.crypto_price import set_ws_prices
from .strategy.base import Strategy, StrategyContext
from .strategy.sweep import SweepStrategy
from .strategy.post_expiry import PostExpirySweepStrategy
from .strategy.aggressive_post_expiry import AggressivePostExpirySweepStrategy
from .utils.slug_helpers import slugs_for_timestamp
from .utils.telegram_notifier import TelegramNotifier
from .config import (
    DB_PATH,
    DRY_RUN,
    FUNDER,
    HEALTH_FILE_PATH,
    MAX_DAILY_LOSS,
    MAX_ORDERS_PER_MINUTE,
    MAX_POSITION_PER_MARKET,
    MAX_TOTAL_EXPOSURE,
    PRIVATE_KEY,
    TELEGRAM_BOT_TOKEN,
    TELEGRAM_CHAT_ID,
    TELEGRAM_NOTIFICATIONS_ENABLED,
    TELEGRAM_ENABLED,

)

logger = get_logger(__name__)

MAX_RETRIES = 10
RETRY_BASE_DELAY = 5
SUB_CHECK_INTERVAL = 30
GRACE_PERIOD_SECONDS = 5 * 60

# Lazy subscription: only subscribe to markets with duration >= this
# threshold when they are within LAZY_SUB_LEAD_S of expiry.
LAZY_SUB_MIN_DURATION = 30   # minutes — applies to 30m, 60m, etc.
LAZY_SUB_LEAD_S = 15 * 60    # subscribe 15 minutes before expiry


class Bot:
    """Top-level orchestrator that creates, wires, and manages all components."""

    def __init__(
        self,
        slugs: list[str],
        strategies: list[Strategy] | None = None,
        strategy_name: str = "sweep",
        dry_run: bool | None = None,
        db_path: str | None = None,
        dashboard_enabled: bool = False,
        price_threshold: float = 0.95,
        early_tick_threshold: float = 0.995,
        market_selections: list[MarketSelection] | None = None,
        durations: list[int] | None = None,
        claim_min_value: float | None = None,
        claim_interval: float = 60.0,
        persist: bool = True,
    ) -> None:
        self.dry_run = dry_run if dry_run is not None else DRY_RUN
        self.db_path = db_path or DB_PATH
        self.dashboard_enabled = dashboard_enabled
        self._persist = persist
        self.loop: asyncio.AbstractEventLoop | None = None
        self._slugs = slugs
        self._market_selections = market_selections or []
        self._durations = durations or sorted(SUPPORTED_DURATIONS)

        self._monitored_ts: dict[int, dict[str, set[int]]] = {
            dur: {sel: set() for sel in self._market_selections}
            for dur in self._durations
        }

        self._deferred_ts: dict[int, dict[str, set[int]]] = {
            dur: {sel: set() for sel in self._market_selections}
            for dur in self._durations
        }

        self.event_bus = EventBus()

        if self._persist:
            conn = init_db(self.db_path)
            self.persistence: AsyncPersistence | None = AsyncPersistence(conn)
        else:
            conn = None
            self.persistence = None
            logger.info("[BOT] Persistence disabled — running fully in-memory")

        risk_config = RiskConfig(
            max_position_per_market=MAX_POSITION_PER_MARKET,
            max_total_exposure=MAX_TOTAL_EXPOSURE,
            max_orders_per_minute=MAX_ORDERS_PER_MINUTE,
            max_daily_loss=MAX_DAILY_LOSS,
        )
        self.risk_manager = RiskManager(risk_config)

        self.rest_client = AsyncRestClient()

        self.order_manager = OrderManager(
            event_bus=self.event_bus,
            rest_client=self.rest_client,
            risk_manager=self.risk_manager,
            persistence=self.persistence,
            dry_run=self.dry_run,
        )
        if conn is not None:
            self.order_manager.load_dedup_from_db(conn)

        # Wire stale-order Telegram alert
        def _on_stale_order(slug: str, order_id: str, age_s: float) -> None:
            if not self.telegram.enabled:
                return
            display = format_slug_with_est_time(slug)
            body = (
                f"📍 <b>Market:</b> <code>{display}</code>\n"
                f"🆔 <b>Order:</b> <code>{order_id[:16]}…</code>\n"
                f"⏱ <b>Pending for:</b> {age_s:.0f}s before force-expired\n"
                f"💸 <b>Exposure released</b>"
            )
            asyncio.run_coroutine_threadsafe(
                self.telegram.push_message(self._telegram_msg("🕐", "STALE ORDER EXPIRED", body)),
                self.loop,
            )
        self.order_manager.on_stale_order = _on_stale_order

        self.position_tracker = PositionTracker(
            persistence=self.persistence,
        )
        if conn is not None:
            self.position_tracker.load_positions_from_db(conn)

        # Shared set: token IDs here get full BookUpdate events published
        # on the event bus.  Tokens NOT in this set still get best_prices
        # updated (cheap), but skip the expensive sort + event publish.
        # This prevents long-duration market traffic from starving
        # time-critical short-duration events.
        self._hot_tokens: set[str] = set()

        self.market_ws = MarketWebSocket(
            event_bus=self.event_bus,
            initial_slugs=list(self._slugs),
            book_event_filter=self._hot_tokens,
        )
        self.user_ws = UserWebSocket(event_bus=self.event_bus)

        crypto_assets = list(set(self._market_selections)) or None
        self.crypto_ws = CryptoWebSocket(assets=crypto_assets)

        if strategies:
            self.strategies = strategies
        else:
            if strategy_name == "post_expiry":
                self.strategies = [
                    PostExpirySweepStrategy(
                        price_threshold=price_threshold,
                        hot_tokens=self._hot_tokens,
                    )
                ]
            elif strategy_name == "aggressive_post_expiry":
                self.strategies = [
                    AggressivePostExpirySweepStrategy(
                        price_threshold=price_threshold,
                        hot_tokens=self._hot_tokens,
                    )
                ]
            else:
                self.strategies = [
                    SweepStrategy(
                        price_threshold=price_threshold,
                        early_tick_threshold=early_tick_threshold,
                        hot_tokens=self._hot_tokens,
                    )
                ]

        self.health_monitor = HealthMonitor(
            heartbeat_path=HEALTH_FILE_PATH,
            context_fn=self._health_context,
        )
        self.alert_manager = AlertManager()

        self._strategy_ctx = StrategyContext(dry_run=self.dry_run)
        self._eval_cache: dict[str, dict[str, Any]] = {}

        self.dashboard: Dashboard | None = None
        if dashboard_enabled:
            self.dashboard = Dashboard(
                market_ws=self.market_ws,
                user_ws=self.user_ws,
                crypto_ws=self.crypto_ws,
                order_manager=self.order_manager,
                position_tracker=self.position_tracker,
                risk_manager=self.risk_manager,
                dry_run=self.dry_run,
                profile=int(os.environ.get("ACTIVE_PROFILE", 0)) or None,
                funder=FUNDER,
                claim_min_value=claim_min_value,
                auto_claimer=None,  # set after auto_claimer is created below
                eval_cache=self._eval_cache,
                strategy_name=strategy_name,
            )

        self.telegram = TelegramNotifier(
            token=TELEGRAM_BOT_TOKEN,
            chat_id=TELEGRAM_CHAT_ID,
            enabled=TELEGRAM_ENABLED,
        )
        self._profile = os.environ.get("ACTIVE_PROFILE") or "0"

        # Auto-claimer (None = disabled)
        self.auto_claimer: AutoClaimer | None = None
        if claim_min_value is not None:
            self.auto_claimer = AutoClaimer(
                min_value=claim_min_value,
                interval=claim_interval,
                funder=FUNDER,
                private_key=PRIVATE_KEY,
            )
            if self.dashboard:
                self.dashboard._auto_claimer = self.auto_claimer
                # Push claim events to dashboard feed
                dashboard_ref = self.dashboard
                def _on_claim(title: str, balance: float | None, tx_hash: str) -> None:
                    logger.info("[BOT] _on_claim callback triggered for %s (bal=%s)", title, balance)
                    # Dashboard push (thread-safe for display)
                    bal_str = f"  [dim]bal=${balance:.2f}[/dim]" if balance is not None else ""
                    dashboard_ref.push_event(
                        f"💎 [bold green]CLAIMED[/bold green]  {title}{bal_str}  tx={tx_hash[:10]}…"
                    )
                    
                    # Telegram notification (must be thread-safe for asyncio)
                    if self.telegram.enabled:
                        bal_tele = f"\n💰 <b>Balance: ${balance:.2f}</b>" if balance is not None else ""
                        body = (
                            f"📦 <b>Market:</b> <code>{title}</code>\n"
                            f"🔗 <b>Tx:</b> <a href='https://polygonscan.com/tx/{tx_hash}'>{tx_hash[:10]}...</a>{bal_tele}"
                        )
                        msg = self._telegram_msg("🟢", "WINNINGS COLLECTED", body)
                        logger.debug("[BOT] Sending claim notification to Telegram")
                        asyncio.run_coroutine_threadsafe(self.telegram.push_message(msg), self.loop)
                    else:
                        logger.debug("[BOT] Telegram disabled, skipping claim notification")
                self.auto_claimer.on_claim = _on_claim
        self._metrics = Metrics.get()
        self._tasks: list[asyncio.Task[Any]] = []

    def _telegram_msg(self, color_emoji: str, title: str, body: str) -> str:
        """Build a Telegram message with profile and color indicator."""
        return (
            f"{color_emoji} <b>{title}</b>\n"
            f"👤 <b>Profile:</b> <code>{self._profile}</code>\n"
            f"━━━━━━━━━━━━━━━\n"
            f"{body}"
        )

    @staticmethod
    def _clean_reason(reason: str) -> str:
        """Strip technical technical wrappers from error messages for the dashboard."""
        if not reason:
            return ""
        # Remove common prefixes
        r = reason
        prefixes = ["EXCEPTION: ", "Exception: ", "PolyApiException: ", "AttributeError: "]
        for p in prefixes:
            if r.startswith(p):
                r = r[len(p):]
        # Remove PolyApiException class format if it persists
        if "PolyApiException[" in r:
            import re
            match = re.search(r"error_message=({.*?})", r)
            if match:
                try:
                    import ast
                    d = ast.literal_eval(match.group(1))
                    r = d.get('error') or d.get('errorMsg') or r
                except Exception:
                    pass
        return r

    @staticmethod
    def _fmt_price(price: float) -> str:
        if price >= 100:
            return f"${price:,.2f}"
        elif price >= 1:
            return f"${price:.4f}"
        return f"${price:.6f}"

    @classmethod
    def _format_proximity(
        cls,
        spot: float | None,
        strike: float | None,
        proximity: float | None = None,
        age_ms: float | None = None,
    ) -> str:
        parts: list[str] = []
        if spot is not None:
            parts.append(f"spot={cls._fmt_price(spot)}")
        else:
            parts.append("spot=STALE")
        if strike is not None:
            parts.append(f"strike={cls._fmt_price(strike)}")
        else:
            parts.append("strike=--")
        if proximity is not None:
            parts.append(f"prox={proximity:.3%}")
        if age_ms is not None:
            parts.append(f"age={age_ms:.0f}ms")
        return "  " + " ".join(parts)

    def _proximity_for_slug(self, slug: str) -> str:
        """Build a proximity display string from the current context for *slug*."""
        asset = extract_market_from_slug(slug)
        if not asset:
            return ""
        spot = self._strategy_ctx.crypto_prices.get(asset)
        eval_data = self._eval_cache.get(slug) or {}
        strike = eval_data.get("price_to_beat")
        prox = abs(spot - strike) / strike if spot and strike and strike > 0 else None
        return self._format_proximity(spot, strike, prox)

    # ── Eval pre-fetch (min_order_size) ───────────────────────────────────

    @staticmethod
    def _fetch_eval_with_min_size(slug: str) -> dict[str, Any] | None:
        """Fetch eval data; min_order_size comes from the pre-cached value."""
        eval_data = get_market_evaluation(slug)
        if eval_data:
            # min_order_size is cached by precache_token_data() at market init.
            # This is an instant dict lookup — zero HTTP calls.
            eval_data["min_order_size"] = get_cached_min_order_size(
                eval_data["best_token_id"]
            )
        return eval_data

    async def _prefetch_eval(self, slug: str) -> None:
        """Background-fetch eval + min_order_size for a single market.

        Also pre-caches neg_risk and fee_rate_bps in the ClobClient so
        that ``create_order()`` never needs HTTP calls for these values
        during the latency-critical order placement path.
        """
        if slug in self._eval_cache:
            return
        try:
            loop = asyncio.get_event_loop()
            eval_data = await loop.run_in_executor(
                None, self._fetch_eval_with_min_size, slug
            )
            if eval_data:
                self._eval_cache[slug] = eval_data
                logger.debug(
                    "[CACHE] Pre-fetched eval for %s (min_size=%.2f)",
                    slug, eval_data.get("min_order_size", -1),
                )
                if eval_data.get("price_to_beat") is None:
                    asyncio.create_task(self._deferred_strike_fetch(slug))
            else:
                logger.warning("[CACHE] Pre-fetch returned no data for %s", slug)
        except Exception:
            logger.exception("[CACHE] Pre-fetch failed for %s", slug)

        # Pre-cache neg_risk + fee_rate in the ClobClient for all token IDs
        # so the library won't need HTTP calls during order placement.
        token_ids = list(self.market_ws.token_ids.get(slug, []))
        if token_ids:
            try:
                await asyncio.get_event_loop().run_in_executor(
                    None, precache_token_data, token_ids
                )
            except Exception:
                logger.warning("[CACHE] precache_token_data failed for %s", slug)

    _STRIKE_SETTLE_BUFFER_S = 5

    async def _deferred_strike_fetch(self, slug: str) -> None:
        """Wait until the market's start time has passed, then fetch the strike price.

        The Binance kline for the start candle doesn't exist until the
        market actually begins.  Instead of blind retries, we parse the
        start timestamp from the slug and sleep until that moment (plus
        a small buffer for the candle to appear on Binance).
        """
        end_ts = extract_market_end_ts(slug)
        duration = detect_duration_from_slug(slug)
        if end_ts is None or duration is None:
            logger.warning("[STRIKE] Cannot parse start time from slug %s — skipping deferred fetch", slug)
            return

        start_ts = end_ts - duration * 60
        wait_s = start_ts - time.time() + self._STRIKE_SETTLE_BUFFER_S
        if wait_s > 0:
            logger.info(
                "[STRIKE] Deferring strike fetch for %s — market starts in %.1fs",
                slug, wait_s - self._STRIKE_SETTLE_BUFFER_S,
            )
            await asyncio.sleep(wait_s)

        if slug not in self._eval_cache:
            return
        if self._eval_cache[slug].get("price_to_beat") is not None:
            return

        try:
            price = await asyncio.get_event_loop().run_in_executor(
                None, fetch_strike_price, slug
            )
            if price is not None:
                self._eval_cache[slug]["price_to_beat"] = price
                logger.info("[STRIKE] Deferred fetch succeeded for %s: $%.6f", slug, price)
            else:
                logger.warning("[STRIKE] Deferred fetch returned None for %s", slug)
        except Exception:
            logger.warning("[STRIKE] Deferred fetch failed for %s", slug, exc_info=True)

    def _launch_prefetch(self, slugs: list[str]) -> None:
        """Fire-and-forget background prefetch for a batch of slugs."""
        for slug in slugs:
            asyncio.create_task(self._prefetch_eval(slug))

    # ── Wiring ────────────────────────────────────────────────────────────

    def _wire_subscriptions(self) -> None:
        bus = self.event_bus

        bus.subscribe(TickSizeChange, self._on_tick_size_change)
        bus.subscribe(BookUpdate, self._on_book_update)
        bus.subscribe(MarketResolved, self._on_market_resolved)

        bus.subscribe(OrderFill, self.order_manager.on_order_fill)
        bus.subscribe(OrderFill, self._dashboard_on_fill)
        bus.subscribe(OrderLive, self.order_manager.on_order_live)
        bus.subscribe(OrderTerminal, self.order_manager.on_order_terminal)
        bus.subscribe(OrderTerminal, self._dashboard_on_terminal)

        bus.subscribe(OrderFill, self.position_tracker.on_fill)
        bus.subscribe(OrderFill, self._on_order_fill_notify_strategy)
        bus.subscribe(OrderTerminal, self._on_order_terminal_notify_strategy)
        # NOTE: PositionTracker no longer subscribes to BookUpdate events.
        # best_prices are synced periodically from market_ws.best_prices
        # in _metrics_loop, avoiding the event bus overhead for every
        # book update across all tokens.
        bus.subscribe(MarketResolved, self.position_tracker.on_market_resolved)

        bus.subscribe(TickSizeChange, self._metrics_tick_size)
        bus.subscribe(BookUpdate, self._metrics_book_update)

    # ── Strategy dispatchers ──────────────────────────────────────────────

    async def _on_tick_size_change(self, event: TickSizeChange) -> None:
        handler_start_ns = time.time_ns()
        self._update_context()
        self._strategy_ctx.tick_sizes[event.token_id] = float(event.new_tick_size)
        for strategy in self.strategies:
            try:
                intents = await strategy.on_tick_size_change(event, self._strategy_ctx)
                if intents:
                    await self._submit_intents(intents, event, handler_start_ns, strategy=strategy)
                else:
                    reason = getattr(strategy, "last_skip_reason", None) or "no signal"
                    
                    if self.persistence and getattr(strategy, "last_skip_reason", None):
                        self.persistence.enqueue(
                            "INSERT INTO decisions (timestamp, strategy, slug, trigger, decision, reason, dry_run) VALUES (?, ?, ?, ?, ?, ?, ?)",
                            (
                                time.time(),
                                strategy.name(),
                                event.slug,
                                "tick_size_change",
                                "SKIP",
                                reason,
                                1 if self.dry_run else 0,
                            ),
                        )
                        
                    if self.dashboard:
                        display_slug = format_slug_with_est_time(event.slug)
                        price = getattr(strategy, "last_best_price", None)
                        price_str = f"  price={price:.3f}" if price is not None else ""
                        watching = getattr(strategy, "last_watching", False)
                        if watching:
                            self.dashboard.push_event(
                                f"👀 [yellow]WATCHING[/yellow]  {display_slug}{price_str}  waiting for bid >= threshold"
                            )
                        else:
                            if "stale" in reason.lower():
                                reason_fmt = f"[bold red]BLOCKED: {reason}[/bold red]"
                            elif "proximity" in reason.lower():
                                reason_fmt = f"[bold magenta]BLOCKED: {reason}[/bold magenta]"
                            else:
                                reason_fmt = reason
                            self.dashboard.push_event(
                                f"⏭️ [dim]NO_TRADE[/dim]  {display_slug}{price_str}  {reason_fmt}"
                            )
            except Exception:
                logger.exception("Strategy %s error on tick_size_change", strategy.name())

    async def _on_book_update(self, event: BookUpdate) -> None:
        handler_start_ns = time.time_ns()
        self._update_context()
        for strategy in self.strategies:
            try:
                intents = await strategy.on_book_update(event, self._strategy_ctx)
                if intents:
                    await self._submit_intents(intents, event, handler_start_ns, strategy=strategy)
            except Exception:
                logger.exception("Strategy %s error on book_update", strategy.name())

    async def _on_market_resolved(self, event: MarketResolved) -> None:
        self._update_context()
        for strategy in self.strategies:
            try:
                await strategy.on_market_resolved(event, self._strategy_ctx)
            except Exception:
                logger.exception("Strategy %s error on market_resolved", strategy.name())

        await self.market_ws.remove_markets([event.slug])
        self._eval_cache.pop(event.slug, None)

        if self.dashboard:
            display_slug = format_slug_with_est_time(event.slug)
            self.dashboard.push_event(
                f"🏁 [blue]RESOLVED[/blue]  {display_slug}  unsubscribed"
            )

        # No Telegram for market resolved (noise)

    async def _submit_intents(
        self,
        intents: list[OrderIntent],
        event: Any,
        handler_start_ns: int | None = None,
        strategy: Strategy | None = None,
    ) -> None:
        tick_event_ns = getattr(event, "timestamp_ns", None)
        for intent in intents:
            display_slug = format_slug_with_est_time(intent.slug)
            state = await self.order_manager.submit(intent)

            if state is not None:
                state.tick_event_ns = tick_event_ns
                state.handler_start_ns = handler_start_ns
                state.market_end_ts = extract_market_end_ts(intent.slug)
                state.market = extract_market_from_slug(intent.slug)
                bp = self._strategy_ctx.best_prices.get(intent.token_id, {})
                state.best_bid = bp.get("bid")
                state.best_ask = bp.get("ask")

                if strategy is not None:
                    state.spot_price = getattr(strategy, "last_spot_price", None)
                    state.strike_price = getattr(strategy, "last_price_to_beat", None)
                    state.proximity = getattr(strategy, "last_proximity", None)
                    state.spot_price_age_ms = getattr(strategy, "last_price_age_ms", None)

                self.order_manager.re_persist(state)

            if state is None and self.dashboard:
                stats = self.order_manager.stats
                if stats.get("dedup_skips", 0) > 0:
                    self.dashboard.push_event(
                        f"🛡️ [yellow]SKIP[/yellow]  {display_slug}  DEDUP: already ordered this session"
                    )
                elif stats.get("risk_blocks", 0) > 0:
                    risk_reason = self.order_manager._last_risk_reason or "limit exceeded"
                    self.dashboard.push_event(
                        f"⚠️ [yellow]SKIP[/yellow]  {display_slug}  RISK: {risk_reason}"
                    )
                    # Telegram alert for risk block
                    if self.telegram.enabled:
                        body = (
                            f"📍 <b>Market:</b> <code>{display_slug}</code>\n"
                            f"🚫 <b>Reason:</b> <code>{risk_reason}</code>"
                        )
                        await self.telegram.push_message(self._telegram_msg("⚠️", "ORDER RISK BLOCKED", body))
                continue

            if state and self.dashboard:
                tick_ms = state.tick_to_order_ms
                expiry_s = state.time_to_expiry_s
                self.dashboard.push_order_metrics(tick_ms, expiry_s)
                
                timing = ""
                if tick_ms is not None:
                    timing += f"  tick→order={tick_ms:.0f}ms"
                    q_ms = state.queue_wait_ms
                    e_ms = state.eval_ms
                    r_ms = state.signal_to_rest_ms
                    if q_ms is not None and e_ms is not None and r_ms is not None:
                        rest_detail = f"rest={r_ms:.0f}ms"
                        if state.sign_ms is not None and state.post_ms is not None:
                            rest_detail = f"sign={state.sign_ms:.0f}ms post={state.post_ms:.0f}ms"
                        timing += f" (bus={q_ms:.0f}ms eval={e_ms:.0f}ms {rest_detail})"
                if expiry_s is not None:
                    timing += f"  expires={expiry_s:.0f}s"

                if state.is_terminal:
                    reason = self._clean_reason(state.rejection_reason or state.status.value)
                    self.dashboard.push_event(
                        f"🛑 [red]{state.status.value}[/red]  {display_slug}  {reason}{timing}"
                    )
                else:
                    bid_str = f"{state.best_bid:.3f}" if state.best_bid is not None else "--"
                    ask_str = f"{state.best_ask:.3f}" if state.best_ask is not None else "--"
                    prox_str = self._format_proximity(
                        state.spot_price, state.strike_price,
                        state.proximity, state.spot_price_age_ms,
                    )
                    self.dashboard.push_event(
                        f"📤 [green]SUBMITTED[/green]  {display_slug}  "
                        f"{intent.side.value} {intent.price:.4f} x {intent.size:.2f}  "
                        f"(bid={bid_str} ask={ask_str}){timing}{prox_str}"
                    )
            
            # Telegram notification for submission (only if not dry-run for cleaner feed)
            if state and not self.dry_run:
                if state.is_terminal:
                    reason = self._clean_reason(state.rejection_reason or state.status.value)
                    body = (
                        f"📍 <b>Market:</b> <code>{display_slug}</code>\n"
                        f"❌ <b>Reason:</b> <code>{reason}</code>"
                    )
                    await self.telegram.push_message(self._telegram_msg("🔴", f"ORDER {state.status.value}", body))
                else:
                    body = (
                        f"📍 <b>Market:</b> <code>{display_slug}</code>\n"
                        f"🔄 <b>{intent.side.value}</b>: ${intent.price:.4f} × {intent.size:.2f} shares"
                    )
                    await self.telegram.push_message(self._telegram_msg("🟡", "ORDER SUBMITTED", body))

            if state and not state.is_terminal:
                self.position_tracker.register_order(
                    order_id=state.order_id,
                    token_id=intent.token_id,
                    slug=intent.slug,
                    strategy=intent.strategy,
                    side=intent.side.value,
                    price=intent.price,
                    size=intent.size,
                    spot_price=state.spot_price,
                )

            # Notify aggressive strategy of order result so it can retry
            if strategy is not None and hasattr(strategy, "notify_order_result"):
                if state is None or state.is_terminal:
                    strategy.notify_order_result(intent.slug, filled=False)
                elif state.status == OrderStatus.FILLED:
                    strategy.notify_order_result(intent.slug, filled=True)
                else:
                    # Submitted/live — keep has_live_order=True until terminal
                    pass

    # ── Strategy poll loop ────────────────────────────────────────────────

    async def _strategy_poll_loop(self) -> None:
        """Periodically call poll() on all strategies for timer-driven logic."""
        from .config import AGGRESSIVE_POLL_INTERVAL_S
        while True:
            await asyncio.sleep(AGGRESSIVE_POLL_INTERVAL_S)
            self._update_context()
            for strategy in self.strategies:
                try:
                    intents = await strategy.poll(self._strategy_ctx)
                    if intents:
                        await self._submit_intents(intents, None, strategy=strategy)
                except Exception:
                    logger.exception("Strategy %s error on poll", strategy.name())

    # ── Dashboard order lifecycle events ────────────────────────────────────

    async def _on_order_fill_notify_strategy(self, event: OrderFill) -> None:
        """Notify strategies when an order fills so they can stop retrying."""
        state = self.order_manager.active_orders.get(event.order_id)
        if not state:
            return
        slug = state.intent.slug
        is_filled = state.status == OrderStatus.FILLED
        for strategy in self.strategies:
            if hasattr(strategy, "notify_order_result"):
                strategy.notify_order_result(slug, filled=is_filled)

    async def _on_order_terminal_notify_strategy(self, event: OrderTerminal) -> None:
        """Notify strategies when an order terminates so they can retry."""
        state = self.order_manager.active_orders.get(event.order_id)
        if not state:
            return
        slug = state.intent.slug
        is_filled = state.status == OrderStatus.FILLED
        for strategy in self.strategies:
            if hasattr(strategy, "notify_order_result"):
                strategy.notify_order_result(slug, filled=is_filled)

    async def _dashboard_on_fill(self, event: OrderFill) -> None:
        if not self.dashboard:
            return
        state = self.order_manager.active_orders.get(event.order_id)
        slug = state.intent.slug if state else "?"
        display = format_slug_with_est_time(slug) if slug != "?" else "?"
        label = "FILLED" if state and state.status == OrderStatus.FILLED else "PARTIAL"
        color = "green" if label == "FILLED" else "cyan"
        emoji = "🎯" if label == "FILLED" else "🌓"
        self.dashboard.push_event(
            f"{emoji} [{color}]{label}[/{color}]  {display}  "
            f"@ {event.fill_price:.4f} x {event.fill_size:.2f}"
        )

        # Telegram notification
        body = (
            f"📍 <b>Market:</b> <code>{display}</code>\n"
            f"💵 <b>Price:</b> ${event.fill_price:.4f}\n"
            f"📦 <b>Size:</b> {event.fill_size:.2f} shares"
        )
        color = "🟢" if label == "FILLED" else "🔵"
        await self.telegram.push_message(self._telegram_msg(color, f"ORDER {label}", body))

    async def _dashboard_on_terminal(self, event: OrderTerminal) -> None:
        if not self.dashboard:
            return
        state = self.order_manager.active_orders.get(event.order_id)
        slug = state.intent.slug if state else "?"
        display = format_slug_with_est_time(slug) if slug != "?" else "?"
        reason = self._clean_reason(event.reason or event.status.value)
        self.dashboard.push_event(
            f"[red]{event.status.value}[/red]  {display}  {reason}"
        )

        # Telegram notification
        body = (
            f"📍 <b>Market:</b> <code>{display}</code>\n"
            f"❌ <b>Reason:</b> <code>{reason}</code>"
        )
        await self.telegram.push_message(self._telegram_msg("🔴", f"ORDER {event.status.value}", body))

    # ── Context maintenance ───────────────────────────────────────────────

    def _update_context(self) -> None:
        self._strategy_ctx.positions = dict(self.position_tracker.positions)
        self._strategy_ctx.best_prices = dict(self.market_ws.best_prices)
        self._strategy_ctx.eval_cache = self._eval_cache
        meta: dict[str, dict[str, Any]] = {}
        for slug, tids in self.market_ws.token_ids.items():
            outcomes = tuple(
                self.market_ws.token_outcomes.get(tid, "?") for tid in tids
            )
            cond = self.market_ws.condition_ids.get(slug, "")
            meta[slug] = {
                "token_ids": tuple(tids),
                "outcomes": outcomes,
                "condition_id": cond,
            }
        self._strategy_ctx.market_meta = meta
        self._strategy_ctx.crypto_prices = dict(self.crypto_ws.latest_prices)
        self._strategy_ctx.crypto_price_ts = dict(self.crypto_ws.last_update_ts)

    # ── Metrics collectors ────────────────────────────────────────────────

    async def _metrics_tick_size(self, event: TickSizeChange) -> None:
        self._metrics.inc("tick_size_changes")
        if self.dashboard:
            if event.latency_ms is not None:
                self.dashboard.push_latency(event.latency_ms)
            display_slug = format_slug_with_est_time(event.slug)
            bp = self._strategy_ctx.best_prices.get(event.token_id, {})
            bid = bp.get("bid")
            price_tag = f"  bid={bid:.3f}" if bid is not None else ""
            lat_tag = f"  (lat: {event.latency_ms:.1f}ms)" if event.latency_ms is not None else ""
            prox_tag = self._proximity_for_slug(event.slug)
            self.dashboard.push_event(
                f"📊 TICK_SIZE  {display_slug}  "
                f"{event.old_tick_size} → {event.new_tick_size}{price_tag}{lat_tag}{prox_tag}"
            )

    async def _metrics_book_update(self, event: BookUpdate) -> None:
        self._metrics.inc("ws_messages_received")

    async def _metrics_loop(self) -> None:
        """Periodic gauge updates for dashboard/health."""
        while True:
            await asyncio.sleep(2)
            set_ws_prices(self.crypto_ws.latest_prices, self.crypto_ws.last_update_ts)
            active = sum(1 for v in self.market_ws.market_active.values() if v)
            self._metrics.set("active_markets", active)
            self._metrics.set("ws_market_connected", 1.0 if self.market_ws.connected else 0.0)
            self._metrics.set("ws_market_msg_age_s", self.market_ws.last_message_age_s)
            self._metrics.set("persistence_pending", float(self.persistence.pending) if self.persistence else 0.0)
            self._metrics.set("orders_pending", float(self.order_manager.pending_count))
            self._metrics.set("books_filtered", float(self.market_ws._books_filtered))

            # Sync best_prices into PositionTracker (replaces per-event subscription)
            for tid, prices in self.market_ws.best_prices.items():
                bid = prices.get("bid")
                if bid is not None:
                    self.position_tracker._best_prices[tid] = bid

    # ── Subscription management (market rolling) ───────────────────────────

    def _seed_monitored_timestamps(self) -> None:
        """Populate _monitored_ts from the initial slugs so the subscription
        manager knows which windows are already tracked."""
        for dur in self._durations:
            cur_ts = get_current_interval_utc(dur)
            nxt_ts = get_next_interval_utc(dur)
            for sel in self._market_selections:
                self._monitored_ts[dur][sel].add(cur_ts)
                self._monitored_ts[dur][sel].add(nxt_ts)

    async def _manage_subscriptions(self) -> None:
        """Periodically add new market windows and prune expired ones."""
        while True:
            await asyncio.sleep(SUB_CHECK_INTERVAL)
            for dur in self._durations:
                await self._manage_subscriptions_for_duration(dur)

    async def _manage_subscriptions_for_duration(self, duration: int) -> None:
        interval_seconds = duration * 60
        cur_ts = get_current_interval_utc(duration)
        nxt_ts = get_next_interval_utc(duration)
        now = int(time.time())

        # For long durations, only subscribe when close to expiry.
        use_lazy = duration >= LAZY_SUB_MIN_DURATION

        slugs_to_add: list[str] = []
        slugs_to_remove: list[str] = []

        for sel in self._market_selections:
            tracked = self._monitored_ts[duration][sel]
            deferred = self._deferred_ts[duration][sel]

            # ── Check timestamps we want to track ──
            for ts in (cur_ts, nxt_ts):
                if ts in tracked or ts in deferred:
                    continue  # already handled

                end_time = ts + interval_seconds
                time_to_expiry = end_time - now
                label = "next" if ts == nxt_ts else "current"

                if use_lazy and time_to_expiry > LAZY_SUB_LEAD_S:
                    # Too far from expiry — defer, don't subscribe yet.
                    deferred.add(ts)
                    try:
                        slug = get_market_slug(sel, duration, ts)
                        display = format_slug_with_est_time(slug)
                        logger.info(
                            "[SUB] Deferring %s %dm market for %s: %s (%.0fm to expiry)",
                            label, duration, sel, display, time_to_expiry / 60,
                        )
                    except ValueError:
                        pass
                    continue

                # Subscribe now.
                try:
                    slug = get_market_slug(sel, duration, ts)
                    slugs_to_add.append(slug)
                    tracked.add(ts)
                    deferred.discard(ts)
                    display = format_slug_with_est_time(slug)
                    logger.info("[SUB] Adding %s %dm market for %s: %s", label, duration, sel, display)
                except ValueError as exc:
                    logger.error("Slug generation failed (%s/%dm %s): %s", sel, duration, label, exc)

            # ── Promote deferred timestamps that are now close enough ──
            if use_lazy:
                newly_ready: list[int] = []
                for ts in deferred:
                    end_time = ts + interval_seconds
                    time_to_expiry = end_time - now
                    if time_to_expiry <= LAZY_SUB_LEAD_S:
                        try:
                            slug = get_market_slug(sel, duration, ts)
                            slugs_to_add.append(slug)
                            tracked.add(ts)
                            newly_ready.append(ts)
                            display = format_slug_with_est_time(slug)
                            logger.info(
                                "[SUB] Promoting deferred %dm market for %s: %s (%.0fm to expiry)",
                                duration, sel, display, time_to_expiry / 60,
                            )
                        except ValueError as exc:
                            logger.error("Slug generation failed on promotion (%s/%dm): %s", sel, duration, exc)
                            newly_ready.append(ts)
                for ts in newly_ready:
                    deferred.discard(ts)

            # ── Prune expired markets ──
            expired: list[int] = []
            for ts in tracked:
                end_time = ts + interval_seconds
                if now > end_time + GRACE_PERIOD_SECONDS:
                    try:
                        slug = get_market_slug(sel, duration, ts)
                        if slug in self.market_ws.market_active and not self.market_ws.market_active[slug]:
                            slugs_to_remove.append(slug)
                            expired.append(ts)
                            display = format_slug_with_est_time(slug)
                            logger.info("[SUB] Removing expired %dm market for %s: %s", duration, sel, display)
                    except ValueError:
                        expired.append(ts)
            for ts in expired:
                tracked.discard(ts)

            # Also clean stale deferred entries.
            stale_deferred = [ts for ts in deferred if now > ts + interval_seconds]
            for ts in stale_deferred:
                deferred.discard(ts)

        if slugs_to_add:
            await self.market_ws.add_markets(slugs_to_add)
            self._launch_prefetch(slugs_to_add)
            if self.dashboard:
                for slug in slugs_to_add:
                    display = format_slug_with_est_time(slug)
                    self.dashboard.push_event(f"📍 MARKET_ADD  {display}")

        if slugs_to_remove:
            await self.market_ws.remove_markets(slugs_to_remove)
            for slug in slugs_to_remove:
                self._eval_cache.pop(slug, None)

    # ── Health context ────────────────────────────────────────────────────

    def _health_context(self) -> dict[str, Any]:
        return {
            "dry_run": self.dry_run,
            "ws_market_connected": self.market_ws.connected,
            "ws_user_connected": self.user_ws.connected,
            "ws_crypto_connected": self.crypto_ws.connected,
            "active_markets": sum(1 for v in self.market_ws.market_active.values() if v),
            "pending_orders": self.order_manager.pending_count,
            "order_stats": self.order_manager.stats,
            "session_pnl": self.position_tracker.session_pnl,
        }

    # ── Supervised task wrapper ───────────────────────────────────────────

    async def _supervised_task(self, name: str, coro: Any) -> None:
        backoff = RETRY_BASE_DELAY
        retries = 0
        while retries < MAX_RETRIES:
            try:
                await coro()
                return
            except asyncio.CancelledError:
                raise
            except Exception:
                retries += 1
                logger.exception(
                    "[SUPERVISOR] %s crashed (attempt %d/%d) — restarting in %ds",
                    name, retries, MAX_RETRIES, backoff,
                )
                await asyncio.sleep(backoff)
                backoff = min(backoff * 2, 120)
        logger.critical("[SUPERVISOR] %s exceeded max retries — giving up", name)

    # ── Lifecycle ─────────────────────────────────────────────────────────

    async def run(self) -> None:
        self.loop = asyncio.get_running_loop()
        logger.info("=" * 60)
        logger.info("POLYMARKET HFT BOT v1 starting")
        _profile = os.getenv("ACTIVE_PROFILE")
        if _profile:
            logger.info("  Profile  : %s", _profile)
        logger.info("  Slugs    : %d", len(self._slugs))
        logger.info("  Strategies: %s", ", ".join(s.name() for s in self.strategies))
        logger.info("  Dry-run  : %s", self.dry_run)
        logger.info("  Dashboard: %s", self.dashboard_enabled)
        logger.info("  Persist  : %s", self._persist)
        if self._persist:
            logger.info("  DB path  : %s", self.db_path)
        logger.info("=" * 60)

        self._wire_subscriptions()
        self._seed_monitored_timestamps()
        self._launch_prefetch(self._slugs)

        for strategy in self.strategies:
            await strategy.startup()

        self._tasks = [
            asyncio.create_task(self._supervised_task("event_bus", self.event_bus.run)),
            *(
                [asyncio.create_task(self._supervised_task("persistence", self.persistence.drain_loop))]
                if self.persistence else []
            ),
            asyncio.create_task(self._supervised_task("market_ws", self.market_ws.run)),
            asyncio.create_task(self._supervised_task("crypto_ws", self.crypto_ws.run)),
            asyncio.create_task(self._supervised_task("health", self.health_monitor.run)),
            asyncio.create_task(self._supervised_task("alerts", self.alert_manager.run)),
            asyncio.create_task(self._supervised_task("metrics_loop", self._metrics_loop)),
            asyncio.create_task(self._supervised_task("sub_manager", self._manage_subscriptions)),
            asyncio.create_task(self._supervised_task("strategy_poll", self._strategy_poll_loop)),
        ]

        if not self.dry_run:
            self._tasks.append(
                asyncio.create_task(self._supervised_task("user_ws", self.user_ws.run))
            )
            self._tasks.append(
                asyncio.create_task(self._supervised_task("order_reconciler", self.order_manager.reconcile_orders))
            )

        if self.dashboard:
            self._tasks.append(
                asyncio.create_task(self._supervised_task("dashboard", self.dashboard.run))
            )

        if self.auto_claimer:
            self._tasks.append(
                asyncio.create_task(self._supervised_task("auto_claimer", self.auto_claimer.run))
            )

        try:
            await asyncio.gather(*self._tasks)
        except asyncio.CancelledError:
            pass
        finally:
            await self.shutdown()

    async def shutdown(self) -> None:
        logger.info("Shutting down...")

        for strategy in self.strategies:
            try:
                await strategy.shutdown()
            except Exception:
                logger.exception("Strategy shutdown error")

        await self.market_ws.stop()
        await self.user_ws.stop()
        await self.event_bus.stop()
        await self.telegram.stop()
        if self.persistence:
            await self.persistence.stop()
        await self.health_monitor.stop()
        await self.alert_manager.stop()

        if self.dashboard:
            await self.dashboard.stop()

        for task in self._tasks:
            task.cancel()
        await asyncio.gather(*self._tasks, return_exceptions=True)

        logger.info("Shutdown complete")

    def run_sync(self) -> None:
        """Blocking entry point with automatic retry."""
        for attempt in range(1, MAX_RETRIES + 1):
            try:
                asyncio.run(self.run())
                return
            except KeyboardInterrupt:
                logger.info("Bot stopped by user")
                return
            except Exception:
                logger.exception(
                    "Bot crashed (attempt %d/%d) — restarting in %ds",
                    attempt, MAX_RETRIES, RETRY_BASE_DELAY * attempt,
                )
                time.sleep(RETRY_BASE_DELAY * attempt)

