"""Endgame sweep strategy — refactored into the Strategy plugin interface.

Detects when a market's tick size drops to 0.001 (approaching settlement)
and, if the leading outcome price exceeds a configurable threshold,
places a BUY order at 0.999 to capture the spread to $1.00.

Port of the original ``sweep_signal.py`` + ``order_executor.py`` logic
into the new architecture.  Pure logic — no I/O, no side effects.
"""

from __future__ import annotations

import time

from ..core.events import BookUpdate, MarketResolved, TickSizeChange
from ..core.models import OrderIntent, Side
from ..logging_config import get_logger
from ..markets.fifteen_min import (
    detect_duration_from_slug,
    extract_market_end_ts,
    extract_market_from_slug,
)
from ..config import (
    DEFAULT_TRADE_SIZE,
    POST_EXPIRY_MULTIPLIER,
    PROXIMITY_FILTER_ENABLED,
    PROXIMITY_MIN_DISTANCE,
)
from .base import Strategy, StrategyContext

logger = get_logger(__name__)

SWEEP_TICK_SIZE = "0.001"
DEFAULT_PRICE_THRESHOLD = 0.99
DEFAULT_EARLY_TICK_THRESHOLD = 0.995
MAX_ORDER_PRICE = 0.999
FALLBACK_MIN_ORDER_SIZE = 5.0


class SweepStrategy(Strategy):
    """Endgame sweep: buy at 0.999 when tick_size drops to 0.001."""

    def __init__(
        self,
        price_threshold: float = DEFAULT_PRICE_THRESHOLD,
        order_price: float = MAX_ORDER_PRICE,
        early_tick_threshold: float = DEFAULT_EARLY_TICK_THRESHOLD,
        hot_tokens: set[str] | None = None,
    ) -> None:
        self._price_threshold = price_threshold
        self._early_tick_threshold = early_tick_threshold
        self._order_price = order_price
        self.last_skip_reason: str | None = None
        self.last_best_price: float | None = None
        self.last_watching: bool = False
        self._too_early: bool = False
        self.last_spot_price: float | None = None
        self.last_price_to_beat: float | None = None
        self.last_proximity: float | None = None
        self.last_price_age_ms: float | None = None
        self._watching: dict[str, dict] = {}
        # Shared set with MarketWebSocket — tokens in this set get full
        # BookUpdate events published; all others are filtered early.
        self._hot_tokens: set[str] = hot_tokens if hot_tokens is not None else set()

    def name(self) -> str:
        return "sweep"

    async def on_tick_size_change(
        self, event: TickSizeChange, ctx: StrategyContext
    ) -> list[OrderIntent] | None:
        self.last_skip_reason = None
        self.last_best_price = None
        self.last_watching = False

        if event.old_tick_size == event.new_tick_size:
            self.last_skip_reason = "duplicate tick_size (unchanged)"
            return None

        if not self._is_sweep_signal(event.new_tick_size):
            self.last_skip_reason = f"not a sweep signal (tick_size={event.new_tick_size})"
            return None

        eval_data = self._get_eval(event.slug, event.token_id, ctx)
        if eval_data is None:
            logger.warning("[SWEEP] No eval data for %s — skipping", event.slug)
            self.last_skip_reason = "no eval data available"
            return None

        best_price = eval_data["best_price"]
        best_token = eval_data["best_token_id"]
        best_outcome = eval_data["best_outcome"]
        self.last_best_price = best_price

        if best_price < self._price_threshold:
            self._start_watching(event.slug, eval_data)
            self.last_watching = True
            logger.info(
                "[SWEEP] %s: price %.3f < threshold %.2f — monitoring until %.2f",
                event.slug, best_price, self._price_threshold, self._price_threshold,
            )
            self.last_skip_reason = (
                f"price {best_price:.3f} < {self._price_threshold:.2f} — monitoring"
            )
            return None

        result = self._build_order(event.slug, eval_data, ctx)
        if result is None and self._too_early:
            eval_data["tte_early"] = True
            self._start_watching(event.slug, eval_data)
            self.last_watching = True
            logger.info(
                "[SWEEP] %s: TTE too early, price %.3f >= threshold — watching with stricter threshold %.3f",
                event.slug, best_price, self._early_tick_threshold,
            )
            return None
        return result

    async def on_book_update(
        self, event: BookUpdate, ctx: StrategyContext
    ) -> list[OrderIntent] | None:
        if event.slug not in self._watching:
            return None

        eval_data = self._watching[event.slug]
        tids = eval_data["token_ids"]
        outcomes = eval_data["outcomes"]
        prices = list(eval_data["prices"])

        for i, tid in enumerate(tids):
            rt = ctx.best_prices.get(tid, {}).get("bid")
            if rt is not None and rt > 0:
                prices[i] = rt

        best_idx = max(range(len(prices)), key=lambda i: prices[i])
        best_price = prices[best_idx]

        threshold = (
            self._early_tick_threshold
            if eval_data.get("tte_early")
            else self._price_threshold
        )
        if best_price < threshold:
            return None

        eval_data.update({
            "prices": prices,
            "best_idx": best_idx,
            "best_price": best_price,
            "best_outcome": outcomes[best_idx] if best_idx < len(outcomes) else "?",
            "best_token_id": tids[best_idx],
        })

        result = self._build_order(event.slug, eval_data, ctx)
        if result is not None:
            self._stop_watching(event.slug)
            logger.info(
                "[SWEEP] %s bid reached %.3f (>= %.3f) — placing order",
                event.slug, best_price, threshold,
            )
        elif self._too_early:
            eval_data["tte_early"] = True
        return result

    async def on_market_resolved(
        self, event: MarketResolved, ctx: StrategyContext
    ) -> None:
        self._stop_watching(event.slug)

    # ── Hot-token management ───────────────────────────────────────────────

    def _start_watching(self, slug: str, eval_data: dict) -> None:
        """Add a market to the watch list and register its tokens as hot."""
        self._watching[slug] = eval_data
        tids = eval_data.get("token_ids", ())
        self._hot_tokens.update(tids)
        logger.debug("[SWEEP] Hot-tokens += %d (total %d)", len(tids), len(self._hot_tokens))

    def _stop_watching(self, slug: str) -> None:
        """Remove a market from the watch list and unregister its tokens."""
        eval_data = self._watching.pop(slug, None)
        if eval_data:
            tids = set(eval_data.get("token_ids", ()))
            # Only remove tokens that aren't still watched by another slug
            still_hot = set()
            for other_data in self._watching.values():
                still_hot.update(other_data.get("token_ids", ()))
            self._hot_tokens -= (tids - still_hot)
            logger.debug("[SWEEP] Hot-tokens -= %d (total %d)", len(tids - still_hot), len(self._hot_tokens))

    # ── Internal helpers ──────────────────────────────────────────────────

    _STALE_THRESHOLD_MS = 10_000

    def _build_order(
        self, slug: str, eval_data: dict, ctx: StrategyContext | None = None,
    ) -> list[OrderIntent] | None:
        """Apply TTE gate, proximity calc, post-expiry doubling, and build the OrderIntent."""
        self._too_early = False
        self.last_spot_price = None
        self.last_price_to_beat = None
        self.last_proximity = None
        self.last_price_age_ms = None

        best_price = eval_data["best_price"]
        best_token = eval_data["best_token_id"]
        best_outcome = eval_data["best_outcome"]
        
        min_size = eval_data.get("min_order_size", FALLBACK_MIN_ORDER_SIZE)
        order_size = max(DEFAULT_TRADE_SIZE, min_size)

        end_ts = extract_market_end_ts(slug)
        tte = (end_ts - time.time()) if end_ts is not None else None
        if tte is not None:
            duration_s = (detect_duration_from_slug(slug) or 15) * 60
            window_s = duration_s / 10
            if tte > window_s:
                logger.debug(
                    "[SWEEP] %s: TTE %.1fs > window %.1fs — too early",
                    slug, tte, window_s,
                )
                self._too_early = True
                self.last_skip_reason = f"TTE {tte:.1f}s > {window_s:.0f}s window (last 1/10th)"
                return None

        # ── Proximity calculation (always runs) ──────────────────────────
        asset = extract_market_from_slug(slug)
        price_to_beat = eval_data.get("price_to_beat")
        if price_to_beat is not None:
            self.last_price_to_beat = price_to_beat

        spot = None
        price_age_ms = None
        stale = False
        if ctx and asset:
            spot = ctx.crypto_prices.get(asset)
            ts = ctx.crypto_price_ts.get(asset)
            if ts is not None:
                price_age_ms = (time.monotonic() - ts) * 1000
                if price_age_ms > self._STALE_THRESHOLD_MS:
                    logger.debug(
                        "[SWEEP] %s spot price stale (%.0fms)",
                        asset, price_age_ms,
                    )
                    stale = True

        if spot is not None:
            self.last_spot_price = spot
            self.last_price_age_ms = price_age_ms

        if spot is not None and price_to_beat is not None and price_to_beat > 0:
            self.last_proximity = abs(spot - price_to_beat) / price_to_beat

        post_expiry = tte is not None and tte < 0

        if (
            PROXIMITY_FILTER_ENABLED
            and not post_expiry
            and not stale
            and self.last_proximity is not None
            and self.last_proximity < PROXIMITY_MIN_DISTANCE
        ):
            self.last_skip_reason = (
                f"proximity {self.last_proximity:.4%} < {PROXIMITY_MIN_DISTANCE:.4%}"
            )
            logger.info(
                "[SWEEP] %s: SKIP — %s (spot=$%.4f strike=$%.4f)",
                slug, self.last_skip_reason,
                self.last_spot_price, self.last_price_to_beat,
            )
            return None

        if post_expiry:
            order_size *= POST_EXPIRY_MULTIPLIER
            logger.info(
                "[SWEEP] Post-expiry signal for %s (%.1fs late) — multiplying size (x%.1f) to %.2f",
                slug, abs(tte), POST_EXPIRY_MULTIPLIER, order_size,
            )

        logger.info(
            "[SWEEP] Signal for %s: %s @ %.3f → BUY %.4f x %.2f",
            slug, best_outcome, best_price, self._order_price, order_size,
        )

        return [OrderIntent(
            token_id=best_token,
            price=self._order_price,
            size=order_size,
            side=Side.BUY,
            strategy=self.name(),
            slug=slug,
            tick_size=float(SWEEP_TICK_SIZE),
        )]

    @staticmethod
    def _is_sweep_signal(new_tick_size: str) -> bool:
        try:
            return float(new_tick_size) == float(SWEEP_TICK_SIZE)
        except (ValueError, TypeError):
            return False

    def _get_eval(
        self, slug: str, token_id: str, ctx: StrategyContext
    ) -> dict | None:
        """Build evaluation from cached data, overlaying real-time WS prices."""
        cached = ctx.eval_cache.get(slug)

        if cached:
            eval_data = dict(cached)
        else:
            meta = ctx.market_meta.get(slug)
            if not meta:
                return None

            token_ids: tuple[str, ...] = meta.get("token_ids", ())
            outcomes: tuple[str, ...] = meta.get("outcomes", ())

            if len(token_ids) < 2:
                return None

            eval_data = {
                "token_ids": token_ids,
                "outcomes": outcomes,
                "prices": [0.0] * len(token_ids),
                "min_order_size": FALLBACK_MIN_ORDER_SIZE,
            }

        tids = eval_data["token_ids"]
        outcomes = eval_data["outcomes"]
        prices = list(eval_data["prices"])

        for i, tid in enumerate(tids):
            rt = ctx.best_prices.get(tid, {}).get("bid")
            if rt is not None and rt > 0:
                if abs(rt - prices[i]) > 0.05:
                    logger.debug(
                        "[SWEEP] RT override %s: cached=%.3f → ws=%.3f",
                        outcomes[i] if i < len(outcomes) else "?",
                        prices[i], rt,
                    )
                prices[i] = rt

        if not any(p > 0 for p in prices):
            return None

        best_idx = max(range(len(prices)), key=lambda i: prices[i])

        eval_data.update({
            "prices": prices,
            "best_idx": best_idx,
            "best_price": prices[best_idx],
            "best_outcome": outcomes[best_idx] if best_idx < len(outcomes) else "?",
            "best_token_id": tids[best_idx],
        })
        return eval_data
