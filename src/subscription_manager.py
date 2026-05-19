"""Subscription management for rolling market windows.

Handles crypto markets (duration-based rolling with lazy subscription) and
stock markets (daily open/close rotation). Pure logic — no I/O, no async.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, datetime
from typing import TYPE_CHECKING

from .logging_config import get_logger
from .markets.fifteen_min import (
    SUPPORTED_DURATIONS,
    extract_market_end_ts,
    get_current_interval_utc,
    get_market_slug,
    get_next_interval_utc,
)

if TYPE_CHECKING:
    from .markets.fifteen_min import MarketSelection

logger = get_logger(__name__)

# Defaults matching current bot.py constants
DEFAULT_GRACE_PERIOD_S = 5 * 60
DEFAULT_LAZY_SUB_MIN_DURATION = 30  # minutes
DEFAULT_LAZY_SUB_LEAD_S = 15 * 60   # seconds


@dataclass(frozen=True, slots=True)
class SubscriptionDelta:
    """Changes computed by a tick."""

    slugs_to_add: tuple[str, ...]
    slugs_to_remove: tuple[str, ...]

    @property
    def is_empty(self) -> bool:
        return not self.slugs_to_add and not self.slugs_to_remove


@dataclass
class SubscriptionManager:
    """Pure subscription logic for rolling market windows.

    Call ``seed()`` once at startup, then ``tick()`` periodically. The manager
    tracks which timestamps are monitored/deferred and computes deltas.

    This class performs no I/O. The caller (Bot) acts on the returned deltas
    by calling market_ws, prefetch, dashboard, etc.
    """

    durations: list[int]
    market_selections: list[str]
    stock_tickers: list[str] = field(default_factory=list)
    grace_period_s: float = DEFAULT_GRACE_PERIOD_S
    lazy_sub_min_duration: int = DEFAULT_LAZY_SUB_MIN_DURATION
    lazy_sub_lead_s: float = DEFAULT_LAZY_SUB_LEAD_S

    # Internal state — initialized in __post_init__
    _monitored_ts: dict[int, dict[str, set[int]]] = field(init=False, repr=False)
    _deferred_ts: dict[int, dict[str, set[int]]] = field(init=False, repr=False)
    _stock_tracked: set[str] = field(init=False, repr=False)

    def __post_init__(self) -> None:
        self._monitored_ts = {
            dur: {sel: set() for sel in self.market_selections}
            for dur in self.durations
        }
        self._deferred_ts = {
            dur: {sel: set() for sel in self.market_selections}
            for dur in self.durations
        }
        self._stock_tracked = set()

    # ── Public interface ──────────────────────────────────────────────────────

    def seed(self, now_ts: float) -> list[str]:
        """Compute initial slugs at startup.

        Populates internal state and returns the list of slugs to subscribe.
        Call this once before the first ``tick()``.
        """
        now = int(now_ts)
        slugs: list[str] = []

        for dur in self.durations:
            slugs.extend(self._seed_duration(dur, now))

        # Stock markets (if any tickers configured)
        slugs.extend(self._seed_stocks(now_ts))

        return slugs

    def tick(
        self,
        now_ts: float,
        market_active: dict[str, bool] | None = None,
        today: date | None = None,
    ) -> SubscriptionDelta:
        """Compute subscription changes since last tick.

        Args:
            now_ts: Current Unix timestamp.
            market_active: Map of slug -> is_active from MarketWebSocket.
                           Used to avoid removing markets that haven't resolved yet.
            today: Current date (for stock rotation). If None, derived from now_ts in EST.

        Returns:
            Delta with slugs to add and remove.
        """
        now = int(now_ts)
        market_active = market_active or {}

        slugs_to_add: list[str] = []
        slugs_to_remove: list[str] = []

        # Crypto rolling windows
        for dur in self.durations:
            add, remove = self._tick_duration(dur, now, market_active)
            slugs_to_add.extend(add)
            slugs_to_remove.extend(remove)

        # Stock daily rotation
        if self.stock_tickers:
            if today is None:
                import pytz
                est = pytz.timezone("US/Eastern")
                today = datetime.fromtimestamp(now_ts, tz=est).date()
            add, remove = self._tick_stocks(now, today, market_active)
            slugs_to_add.extend(add)
            slugs_to_remove.extend(remove)

        return SubscriptionDelta(
            slugs_to_add=tuple(slugs_to_add),
            slugs_to_remove=tuple(slugs_to_remove),
        )

    @property
    def monitored_ts(self) -> dict[int, dict[str, set[int]]]:
        """Read-only access for dashboard coverage display."""
        return self._monitored_ts

    # ── Crypto duration logic ─────────────────────────────────────────────────

    def _seed_duration(self, duration: int, now: int) -> list[str]:
        """Seed monitored/deferred sets for a single duration."""
        cur_ts = get_current_interval_utc(duration)
        nxt_ts = get_next_interval_utc(duration)
        interval_s = duration * 60
        use_lazy = duration >= self.lazy_sub_min_duration

        seeds = [cur_ts, nxt_ts]
        if not use_lazy:
            prev_ts = cur_ts - interval_s
            prev_end = prev_ts + interval_s
            if now <= prev_end + self.grace_period_s:
                seeds.insert(0, prev_ts)

        slugs: list[str] = []

        for sel in self.market_selections:
            for ts in seeds:
                end_time = ts + interval_s
                time_to_expiry = end_time - now

                if time_to_expiry < -self.grace_period_s:
                    continue

                if use_lazy and time_to_expiry > self.lazy_sub_lead_s:
                    self._deferred_ts[duration][sel].add(ts)
                    logger.debug(
                        "[SUB] Seeding deferred %dm/%s ts=%d (%.0fm to expiry)",
                        duration, sel, ts, time_to_expiry / 60,
                    )
                else:
                    self._monitored_ts[duration][sel].add(ts)
                    try:
                        slug = get_market_slug(sel, duration, ts)
                        slugs.append(slug)
                        logger.debug(
                            "[SUB] Seeding active %dm/%s: %s",
                            duration, sel, slug,
                        )
                    except ValueError as exc:
                        logger.warning(
                            "[SUB] Seed slug generation failed (%s/%dm): %s",
                            sel, duration, exc,
                        )

        return slugs

    def _tick_duration(
        self,
        duration: int,
        now: int,
        market_active: dict[str, bool],
    ) -> tuple[list[str], list[str]]:
        """Process one tick for a single duration."""
        interval_s = duration * 60
        cur_ts = get_current_interval_utc(duration)
        nxt_ts = get_next_interval_utc(duration)
        prev_ts = cur_ts - interval_s
        use_lazy = duration >= self.lazy_sub_min_duration

        candidate_ts = [cur_ts, nxt_ts]
        if not use_lazy:
            prev_end = prev_ts + interval_s
            if now <= prev_end + self.grace_period_s:
                candidate_ts.insert(0, prev_ts)

        slugs_to_add: list[str] = []
        slugs_to_remove: list[str] = []

        for sel in self.market_selections:
            tracked = self._monitored_ts[duration][sel]
            deferred = self._deferred_ts[duration][sel]

            # ── Add new timestamps ──
            for ts in candidate_ts:
                if ts in tracked or ts in deferred:
                    continue

                end_time = ts + interval_s
                time_to_expiry = end_time - now

                if time_to_expiry < -self.grace_period_s:
                    continue

                if use_lazy and time_to_expiry > self.lazy_sub_lead_s:
                    deferred.add(ts)
                    logger.info(
                        "[SUB] Deferring %dm/%s ts=%d (%.0fm to expiry)",
                        duration, sel, ts, time_to_expiry / 60,
                    )
                    continue

                try:
                    slug = get_market_slug(sel, duration, ts)
                    slugs_to_add.append(slug)
                    tracked.add(ts)
                    deferred.discard(ts)
                    logger.info("[SUB] Adding %dm/%s: %s", duration, sel, slug)
                except ValueError as exc:
                    logger.error(
                        "[SUB] Slug generation failed (%s/%dm): %s",
                        sel, duration, exc,
                    )

            # ── Promote deferred timestamps ──
            if use_lazy:
                newly_ready: list[int] = []
                for ts in deferred:
                    end_time = ts + interval_s
                    time_to_expiry = end_time - now
                    if time_to_expiry <= self.lazy_sub_lead_s:
                        try:
                            slug = get_market_slug(sel, duration, ts)
                            slugs_to_add.append(slug)
                            tracked.add(ts)
                            newly_ready.append(ts)
                            logger.info(
                                "[SUB] Promoting %dm/%s: %s (%.0fm to expiry)",
                                duration, sel, slug, time_to_expiry / 60,
                            )
                        except ValueError as exc:
                            logger.error(
                                "[SUB] Promotion slug failed (%s/%dm): %s",
                                sel, duration, exc,
                            )
                            newly_ready.append(ts)
                for ts in newly_ready:
                    deferred.discard(ts)

            # ── Prune expired timestamps ──
            expired: list[int] = []
            for ts in tracked:
                end_time = ts + interval_s
                if now > end_time + self.grace_period_s:
                    try:
                        slug = get_market_slug(sel, duration, ts)
                        # Only remove if market is known and inactive
                        if slug in market_active and not market_active[slug]:
                            slugs_to_remove.append(slug)
                            expired.append(ts)
                            logger.info(
                                "[SUB] Removing expired %dm/%s: %s",
                                duration, sel, slug,
                            )
                    except ValueError:
                        expired.append(ts)
            for ts in expired:
                tracked.discard(ts)

            # Clean stale deferred entries
            stale = [ts for ts in deferred if now > ts + interval_s]
            for ts in stale:
                deferred.discard(ts)

        return slugs_to_add, slugs_to_remove

    # ── Stock logic ───────────────────────────────────────────────────────────

    def _seed_stocks(self, now_ts: float) -> list[str]:
        """Seed stock subscriptions at startup."""
        if not self.stock_tickers:
            return []

        import pytz
        from .markets.stocks import generate_stock_slugs_for_date

        est = pytz.timezone("US/Eastern")
        today = datetime.fromtimestamp(now_ts, tz=est).date()
        now = int(now_ts)

        slugs: list[str] = []
        for ticker in self.stock_tickers:
            for slug in generate_stock_slugs_for_date(ticker, today):
                end_ts = extract_market_end_ts(slug)
                if end_ts is None:
                    continue
                if now > end_ts + self.grace_period_s:
                    continue
                self._stock_tracked.add(slug)
                slugs.append(slug)
                logger.info("[STOCK_SUB] Seeding stock market: %s", slug)

        return slugs

    def _tick_stocks(
        self,
        now: int,
        today: date,
        market_active: dict[str, bool],
    ) -> tuple[list[str], list[str]]:
        """Process one tick for stock subscriptions."""
        from .markets.stocks import generate_stock_slugs_for_date

        slugs_to_add: list[str] = []
        slugs_to_remove: list[str] = []

        for ticker in self.stock_tickers:
            for slug in generate_stock_slugs_for_date(ticker, today):
                end_ts = extract_market_end_ts(slug)
                if end_ts is None:
                    continue

                if slug in self._stock_tracked:
                    if now > end_ts + self.grace_period_s:
                        # Only remove if inactive
                        if slug in market_active and not market_active[slug]:
                            slugs_to_remove.append(slug)
                            self._stock_tracked.discard(slug)
                            logger.info("[STOCK_SUB] Removing expired: %s", slug)
                    continue

                if now > end_ts + self.grace_period_s:
                    continue

                self._stock_tracked.add(slug)
                slugs_to_add.append(slug)
                logger.info("[STOCK_SUB] Adding: %s", slug)

        return slugs_to_add, slugs_to_remove
