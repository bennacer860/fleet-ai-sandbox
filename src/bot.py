"""Main bot orchestrator — wires all components and manages lifecycle.

Replaces ``SweeperBot`` with a fully event-driven architecture.
Components communicate exclusively through the ``EventBus``.
"""

from __future__ import annotations

import asyncio
import time
from typing import Any

from .config import (
    DB_PATH,
    DRY_RUN,
    HEALTH_FILE_PATH,
    MAX_DAILY_LOSS,
    MAX_ORDERS_PER_MINUTE,
    MAX_POSITION_PER_MARKET,
    MAX_TOTAL_EXPOSURE,
)
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
from .utils.market_data import get_market_evaluation, get_min_order_size
from .markets.fifteen_min import (
    MarketSelection,
    SUPPORTED_DURATIONS,
    extract_market_end_ts,
    get_current_interval_utc,
    get_market_slug,
    get_next_interval_utc,
)
from .strategy.base import Strategy, StrategyContext
from .strategy.sweep import SweepStrategy
from .utils.slug_helpers import slugs_for_timestamp

logger = get_logger(__name__)

MAX_RETRIES = 10
RETRY_BASE_DELAY = 5
SUB_CHECK_INTERVAL = 30
GRACE_PERIOD_SECONDS = 5 * 60


class Bot:
    """Top-level orchestrator that creates, wires, and manages all components."""

    def __init__(
        self,
        slugs: list[str],
        strategies: list[Strategy] | None = None,
        dry_run: bool | None = None,
        db_path: str | None = None,
        dashboard_enabled: bool = False,
        price_threshold: float = 0.95,
        market_selections: list[MarketSelection] | None = None,
        durations: list[int] | None = None,
    ) -> None:
        self.dry_run = dry_run if dry_run is not None else DRY_RUN
        self.db_path = db_path or DB_PATH
        self.dashboard_enabled = dashboard_enabled
        self._slugs = slugs
        self._market_selections = market_selections or []
        self._durations = durations or sorted(SUPPORTED_DURATIONS)

        self._monitored_ts: dict[int, dict[str, set[int]]] = {
            dur: {sel: set() for sel in self._market_selections}
            for dur in self._durations
        }

        self.event_bus = EventBus()

        conn = init_db(self.db_path)

        self.persistence = AsyncPersistence(conn)

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
        self.order_manager.load_dedup_from_db(conn)

        self.position_tracker = PositionTracker(persistence=self.persistence)
        self.position_tracker.load_positions_from_db(conn)

        self.market_ws = MarketWebSocket(
            event_bus=self.event_bus,
            initial_slugs=list(self._slugs),
        )
        self.user_ws = UserWebSocket(event_bus=self.event_bus)

        self.strategies: list[Strategy] = strategies or [
            SweepStrategy(price_threshold=price_threshold)
        ]

        self.health_monitor = HealthMonitor(
            heartbeat_path=HEALTH_FILE_PATH,
            context_fn=self._health_context,
        )
        self.alert_manager = AlertManager()

        self.dashboard: Dashboard | None = None
        if dashboard_enabled:
            self.dashboard = Dashboard(
                market_ws=self.market_ws,
                user_ws=self.user_ws,
                order_manager=self.order_manager,
                position_tracker=self.position_tracker,
                risk_manager=self.risk_manager,
                dry_run=self.dry_run,
            )

        self._strategy_ctx = StrategyContext(dry_run=self.dry_run)
        self._eval_cache: dict[str, dict[str, Any]] = {}
        self._metrics = Metrics.get()
        self._tasks: list[asyncio.Task[Any]] = []

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

    # ── Eval pre-fetch (min_order_size) ───────────────────────────────────

    @staticmethod
    def _fetch_eval_with_min_size(slug: str) -> dict[str, Any] | None:
        """Fetch eval data AND min_order_size in one blocking call."""
        eval_data = get_market_evaluation(slug)
        if eval_data:
            eval_data["min_order_size"] = get_min_order_size(
                eval_data["best_token_id"]
            )
        return eval_data

    async def _prefetch_eval(self, slug: str) -> None:
        """Background-fetch eval + min_order_size for a single market."""
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
            else:
                logger.warning("[CACHE] Pre-fetch returned no data for %s", slug)
        except Exception:
            logger.exception("[CACHE] Pre-fetch failed for %s", slug)

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
        bus.subscribe(BookUpdate, self.position_tracker.on_book_update)
        bus.subscribe(MarketResolved, self.position_tracker.on_market_resolved)

        bus.subscribe(TickSizeChange, self._metrics_tick_size)
        bus.subscribe(BookUpdate, self._metrics_book_update)

    # ── Strategy dispatchers ──────────────────────────────────────────────

    async def _on_tick_size_change(self, event: TickSizeChange) -> None:
        self._update_context()
        for strategy in self.strategies:
            try:
                intents = await strategy.on_tick_size_change(event, self._strategy_ctx)
                if intents:
                    await self._submit_intents(intents, event)
                elif self.dashboard:
                    display_slug = format_slug_with_est_time(event.slug)
                    reason = getattr(strategy, "last_skip_reason", None) or "no signal"
                    price = getattr(strategy, "last_best_price", None)
                    price_str = f"  price={price:.3f}" if price is not None else ""
                    self.dashboard.push_event(
                        f"[dim]NO_TRADE[/dim]  {display_slug}{price_str}  {reason}"
                    )
            except Exception:
                logger.exception("Strategy %s error on tick_size_change", strategy.name())

    async def _on_book_update(self, event: BookUpdate) -> None:
        self._update_context()
        for strategy in self.strategies:
            try:
                intents = await strategy.on_book_update(event, self._strategy_ctx)
                if intents:
                    await self._submit_intents(intents, event)
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
                f"[blue]RESOLVED[/blue]  {display_slug}  unsubscribed"
            )

    async def _submit_intents(self, intents: list[OrderIntent], event: Any) -> None:
        tick_event_ns = getattr(event, "timestamp_ns", None)
        for intent in intents:
            display_slug = format_slug_with_est_time(intent.slug)
            state = await self.order_manager.submit(intent)

            if state is not None:
                state.tick_event_ns = tick_event_ns
                state.market_end_ts = extract_market_end_ts(intent.slug)
                self.order_manager.re_persist(state)

            if state is None and self.dashboard:
                stats = self.order_manager.stats
                if stats.get("dedup_skips", 0) > 0:
                    self.dashboard.push_event(
                        f"[yellow]SKIP[/yellow]  {display_slug}  DEDUP: already ordered this session"
                    )
                elif stats.get("risk_blocks", 0) > 0:
                    self.dashboard.push_event(
                        f"[yellow]SKIP[/yellow]  {display_slug}  RISK: limit exceeded"
                    )
                continue

            if state and self.dashboard:
                tick_ms = state.tick_to_order_ms
                expiry_s = state.time_to_expiry_s
                timing = ""
                if tick_ms is not None:
                    timing += f"  tick→order={tick_ms:.0f}ms"
                if expiry_s is not None:
                    timing += f"  expires={expiry_s:.0f}s"

                if state.is_terminal:
                    reason = self._clean_reason(state.rejection_reason or state.status.value)
                    self.dashboard.push_event(
                        f"[red]{state.status.value}[/red]  {display_slug}  {reason}{timing}"
                    )
                else:
                    self.dashboard.push_event(
                        f"[green]SUBMITTED[/green]  {display_slug}  "
                        f"{intent.side.value} {intent.price:.4f} x {intent.size:.2f}{timing}"
                    )

            if state and not state.is_terminal:
                self.position_tracker.register_order(
                    order_id=state.order_id,
                    token_id=intent.token_id,
                    slug=intent.slug,
                    strategy=intent.strategy,
                    side=intent.side.value,
                    price=intent.price,
                    size=intent.size,
                )

    # ── Dashboard order lifecycle events ────────────────────────────────────

    async def _dashboard_on_fill(self, event: OrderFill) -> None:
        if not self.dashboard:
            return
        state = self.order_manager.active_orders.get(event.order_id)
        slug = state.intent.slug if state else "?"
        display = format_slug_with_est_time(slug) if slug != "?" else "?"
        label = "FILLED" if state and state.status == OrderStatus.FILLED else "PARTIAL"
        color = "green" if label == "FILLED" else "cyan"
        self.dashboard.push_event(
            f"[{color}]{label}[/{color}]  {display}  "
            f"@ {event.fill_price:.4f} x {event.fill_size:.2f}"
        )

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

    # ── Metrics collectors ────────────────────────────────────────────────

    async def _metrics_tick_size(self, event: TickSizeChange) -> None:
        self._metrics.inc("tick_size_changes")
        if self.dashboard:
            display_slug = format_slug_with_est_time(event.slug)
            bp = self._strategy_ctx.best_prices.get(event.token_id, {})
            bid = bp.get("bid")
            price_tag = f"  bid={bid:.3f}" if bid is not None else ""
            self.dashboard.push_event(
                f"TICK_SIZE  {display_slug}  "
                f"{event.old_tick_size} → {event.new_tick_size}{price_tag}"
            )

    async def _metrics_book_update(self, event: BookUpdate) -> None:
        self._metrics.inc("ws_messages_received")

    async def _metrics_loop(self) -> None:
        """Periodic gauge updates for dashboard/health."""
        while True:
            await asyncio.sleep(2)
            active = sum(1 for v in self.market_ws.market_active.values() if v)
            self._metrics.set("active_markets", active)
            self._metrics.set("ws_market_connected", 1.0 if self.market_ws.connected else 0.0)
            self._metrics.set("ws_market_msg_age_s", self.market_ws.last_message_age_s)
            self._metrics.set("persistence_pending", float(self.persistence.pending))
            self._metrics.set("orders_pending", float(self.order_manager.pending_count))

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

        slugs_to_add: list[str] = []
        slugs_to_remove: list[str] = []

        for sel in self._market_selections:
            tracked = self._monitored_ts[duration][sel]

            if nxt_ts not in tracked:
                try:
                    slug = get_market_slug(sel, duration, nxt_ts)
                    slugs_to_add.append(slug)
                    tracked.add(nxt_ts)
                    display = format_slug_with_est_time(slug)
                    logger.info("[SUB] Adding next %dm market for %s: %s", duration, sel, display)
                except ValueError as exc:
                    logger.error("Slug generation failed (%s/%dm next): %s", sel, duration, exc)

            if cur_ts not in tracked:
                try:
                    slug = get_market_slug(sel, duration, cur_ts)
                    slugs_to_add.append(slug)
                    tracked.add(cur_ts)
                    display = format_slug_with_est_time(slug)
                    logger.info("[SUB] Adding current %dm market for %s: %s", duration, sel, display)
                except ValueError as exc:
                    logger.error("Slug generation failed (%s/%dm current): %s", sel, duration, exc)

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

        if slugs_to_add:
            await self.market_ws.add_markets(slugs_to_add)
            self._launch_prefetch(slugs_to_add)
            if self.dashboard:
                for slug in slugs_to_add:
                    display = format_slug_with_est_time(slug)
                    self.dashboard.push_event(f"MARKET_ADD  {display}")

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
        logger.info("=" * 60)
        logger.info("POLYMARKET HFT BOT v1 starting")
        logger.info("  Slugs    : %d", len(self._slugs))
        logger.info("  Strategies: %s", ", ".join(s.name() for s in self.strategies))
        logger.info("  Dry-run  : %s", self.dry_run)
        logger.info("  Dashboard: %s", self.dashboard_enabled)
        logger.info("  DB path  : %s", self.db_path)
        logger.info("=" * 60)

        self._wire_subscriptions()
        self._seed_monitored_timestamps()
        self._launch_prefetch(self._slugs)

        for strategy in self.strategies:
            await strategy.startup()

        self._tasks = [
            asyncio.create_task(self._supervised_task("event_bus", self.event_bus.run)),
            asyncio.create_task(self._supervised_task("persistence", self.persistence.drain_loop)),
            asyncio.create_task(self._supervised_task("market_ws", self.market_ws.run)),
            asyncio.create_task(self._supervised_task("health", self.health_monitor.run)),
            asyncio.create_task(self._supervised_task("alerts", self.alert_manager.run)),
            asyncio.create_task(self._supervised_task("metrics_loop", self._metrics_loop)),
            asyncio.create_task(self._supervised_task("stale_reaper", self.order_manager.reap_stale_orders)),
            asyncio.create_task(self._supervised_task("sub_manager", self._manage_subscriptions)),
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
