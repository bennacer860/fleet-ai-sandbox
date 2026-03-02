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
from ..markets.fifteen_min import detect_duration_from_slug, extract_market_end_ts
from .base import Strategy, StrategyContext

logger = get_logger(__name__)

SWEEP_TICK_SIZE = "0.001"
DEFAULT_PRICE_THRESHOLD = 0.99
MAX_ORDER_PRICE = 0.999
FALLBACK_MIN_ORDER_SIZE = 5.0


class SweepStrategy(Strategy):
    """Endgame sweep: buy at 0.999 when tick_size drops to 0.001."""

    def __init__(
        self,
        price_threshold: float = DEFAULT_PRICE_THRESHOLD,
        order_price: float = MAX_ORDER_PRICE,
    ) -> None:
        self._price_threshold = price_threshold
        self._order_price = order_price
        self.last_skip_reason: str | None = None
        self.last_best_price: float | None = None
        self.last_watching: bool = False
        self._watching: dict[str, dict] = {}

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
            self._watching[event.slug] = eval_data
            self.last_watching = True
            logger.info(
                "[SWEEP] %s: price %.3f < threshold %.2f — monitoring until %.2f",
                event.slug, best_price, self._price_threshold, self._price_threshold,
            )
            self.last_skip_reason = (
                f"price {best_price:.3f} < {self._price_threshold:.2f} — monitoring"
            )
            return None

        return self._build_order(event.slug, eval_data)

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

        if best_price < self._price_threshold:
            return None

        eval_data.update({
            "prices": prices,
            "best_idx": best_idx,
            "best_price": best_price,
            "best_outcome": outcomes[best_idx] if best_idx < len(outcomes) else "?",
            "best_token_id": tids[best_idx],
        })

        del self._watching[event.slug]

        logger.info(
            "[SWEEP] %s bid reached %.3f (>= %.2f) — placing order",
            event.slug, best_price, self._price_threshold,
        )

        return self._build_order(event.slug, eval_data)

    async def on_market_resolved(
        self, event: MarketResolved, ctx: StrategyContext
    ) -> None:
        self._watching.pop(event.slug, None)

    # ── Internal helpers ──────────────────────────────────────────────────

    def _build_order(self, slug: str, eval_data: dict) -> list[OrderIntent] | None:
        """Apply TTE gate, post-expiry doubling, and build the OrderIntent."""
        best_price = eval_data["best_price"]
        best_token = eval_data["best_token_id"]
        best_outcome = eval_data["best_outcome"]
        order_size = eval_data.get("min_order_size", FALLBACK_MIN_ORDER_SIZE)

        end_ts = extract_market_end_ts(slug)
        tte = (end_ts - time.time()) if end_ts is not None else None
        if tte is not None:
            duration_s = (detect_duration_from_slug(slug) or 15) * 60
            window_s = duration_s / 10
            if tte > window_s:
                logger.info(
                    "[SWEEP] %s: TTE %.1fs > window %.1fs — too early, skipping",
                    slug, tte, window_s,
                )
                self.last_skip_reason = f"TTE {tte:.1f}s > {window_s:.0f}s window (last 1/10th)"
                return None

        if tte is not None and tte < 0:
            order_size *= 2.0
            logger.info(
                "[SWEEP] Post-expiry signal for %s (%.1fs late) — doubling size to %.2f",
                slug, abs(tte), order_size,
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
