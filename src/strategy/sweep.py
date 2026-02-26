"""Endgame sweep strategy — refactored into the Strategy plugin interface.

Detects when a market's tick size drops to 0.001 (approaching settlement)
and, if the leading outcome price exceeds a configurable threshold,
places a BUY order at 0.999 to capture the spread to $1.00.

Port of the original ``sweep_signal.py`` + ``order_executor.py`` logic
into the new architecture.  Pure logic — no I/O, no side effects.
"""

from __future__ import annotations

from ..core.events import BookUpdate, MarketResolved, TickSizeChange
from ..core.models import OrderIntent, Side
from ..logging_config import get_logger
from .base import Strategy, StrategyContext

logger = get_logger(__name__)

SWEEP_TICK_SIZE = "0.001"
DEFAULT_PRICE_THRESHOLD = 0.95
MAX_ORDER_PRICE = 0.999
FALLBACK_MIN_ORDER_SIZE = 1.0


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

    def name(self) -> str:
        return "sweep"

    async def on_tick_size_change(
        self, event: TickSizeChange, ctx: StrategyContext
    ) -> list[OrderIntent] | None:
        self.last_skip_reason = None
        self.last_best_price = None

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
            logger.info(
                "[SWEEP] %s: price %.3f < threshold %.2f — skip",
                event.slug, best_price, self._price_threshold,
            )
            self.last_skip_reason = (
                f"price {best_price:.3f} < threshold {self._price_threshold:.2f}"
            )
            return None

        order_size = eval_data.get("min_order_size", FALLBACK_MIN_ORDER_SIZE)

        logger.info(
            "[SWEEP] Signal for %s: %s @ %.3f → BUY %.4f x %.2f",
            event.slug, best_outcome, best_price, self._order_price, order_size,
        )

        return [OrderIntent(
            token_id=best_token,
            price=self._order_price,
            size=order_size,
            side=Side.BUY,
            strategy=self.name(),
            slug=event.slug,
            tick_size=float(event.new_tick_size),
        )]

    async def on_book_update(
        self, event: BookUpdate, ctx: StrategyContext
    ) -> list[OrderIntent] | None:
        return None

    # ── Internal helpers ──────────────────────────────────────────────────

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
