"""Post-expiry strategy for daily city temperature markets."""

from __future__ import annotations

import time
from typing import Any

from ..config import (
    POST_EXPIRY_MULTIPLIER,
    TEMPERATURE_POST_EXPIRY_PRICE_THRESHOLD,
    TRADE_SIZE_TEMPERATURE,
)
from ..core.events import BookUpdate, MarketResolved, TickSizeChange
from ..core.models import OrderIntent, Side
from ..logging_config import get_logger
from .base import Strategy, StrategyContext
from .registry import StrategySpec, register_strategy

logger = get_logger(__name__)

SWEEP_TICK_SIZE = "0.001"
MAX_ORDER_PRICE = 0.999
FALLBACK_MIN_ORDER_SIZE = 5.0


class PostExpiryTemperatureStrategy(Strategy):
    """Post-expiry sweep strategy scoped to city temperature markets."""

    def __init__(
        self,
        *,
        hot_tokens: set[str] | None = None,
        order_price: float = MAX_ORDER_PRICE,
        price_threshold: float = TEMPERATURE_POST_EXPIRY_PRICE_THRESHOLD,
    ) -> None:
        self._hot_tokens: set[str] = hot_tokens if hot_tokens is not None else set()
        self._order_price = order_price
        self._price_threshold = price_threshold
        self._watching: dict[str, dict[str, Any]] = {}
        self._ordered_slugs: set[str] = set()
        self.last_skip_reason: str | None = None

    def name(self) -> str:
        return "post_expiry_temperature"

    def classify_submission(self, event: Any) -> str:
        if isinstance(event, TickSizeChange):
            return "immediate_tick"
        return "watched_expiry"

    async def on_tick_size_change(
        self, event: TickSizeChange, ctx: StrategyContext
    ) -> list[OrderIntent] | None:
        self.last_skip_reason = None
        if event.new_tick_size != SWEEP_TICK_SIZE:
            self.last_skip_reason = f"not a sweep signal (tick_size={event.new_tick_size})"
            return None

        eval_data = self._get_eval(event.slug, ctx)
        if eval_data is None:
            self.last_skip_reason = "no eval data available"
            return None

        self._start_watching(event.slug, eval_data)
        return self._check_and_build_order(event.slug, eval_data, ctx)

    async def on_book_update(
        self, event: BookUpdate, ctx: StrategyContext
    ) -> list[OrderIntent] | None:
        if event.slug not in self._watching:
            return None
        eval_data = self._watching[event.slug]
        self._refresh_prices(eval_data, ctx)
        return self._check_and_build_order(event.slug, eval_data, ctx)

    async def on_market_resolved(
        self, event: MarketResolved, ctx: StrategyContext
    ) -> None:
        self._stop_watching(event.slug)
        self._ordered_slugs.discard(event.slug)

    async def poll(self, ctx: StrategyContext) -> list[OrderIntent] | None:
        intents: list[OrderIntent] = []
        for slug, eval_data in list(self._watching.items()):
            self._refresh_prices(eval_data, ctx)
            maybe = self._check_and_build_order(slug, eval_data, ctx)
            if maybe:
                intents.extend(maybe)
        return intents or None

    def _refresh_prices(self, eval_data: dict[str, Any], ctx: StrategyContext) -> None:
        tids = eval_data["token_ids"]
        outcomes = eval_data["outcomes"]
        prices = list(eval_data["prices"])
        for i, tid in enumerate(tids):
            rt = ctx.best_prices.get(tid, {}).get("bid")
            if rt is not None and rt > 0:
                prices[i] = rt
        if not any(p > 0 for p in prices):
            return
        best_idx = max(range(len(prices)), key=lambda i: prices[i])
        eval_data.update(
            {
                "prices": prices,
                "best_idx": best_idx,
                "best_price": prices[best_idx],
                "best_outcome": outcomes[best_idx] if best_idx < len(outcomes) else "?",
                "best_token_id": tids[best_idx],
            }
        )

    def _check_and_build_order(
        self, slug: str, eval_data: dict[str, Any], ctx: StrategyContext
    ) -> list[OrderIntent] | None:
        if slug in self._ordered_slugs:
            return None
        meta = ctx.market_meta.get(slug, {})
        if meta.get("market_family") != "city_temperature":
            self.last_skip_reason = "not a city temperature market"
            return None

        safe_expiry_ts = meta.get("safe_expiry_ts") or meta.get("gamma_end_ts")
        if safe_expiry_ts is None:
            self.last_skip_reason = "missing safe_expiry_ts"
            return None
        tte = float(safe_expiry_ts) - time.time()
        if tte > 0:
            self.last_skip_reason = f"waiting for safe expiry (TTE: {tte:.1f}s)"
            return None

        best_price = float(eval_data["best_price"])
        best_token = str(eval_data["best_token_id"])
        best_outcome = str(eval_data["best_outcome"])
        if best_price < self._price_threshold:
            self.last_skip_reason = (
                f"price {best_price:.3f} < {self._price_threshold:.2f} — waiting for convergence"
            )
            return None

        min_size = float(eval_data.get("min_order_size", FALLBACK_MIN_ORDER_SIZE))
        order_size = max(TRADE_SIZE_TEMPERATURE, min_size) * POST_EXPIRY_MULTIPLIER
        all_tids = eval_data.get("token_ids", (best_token,))
        tick_sizes = [ctx.tick_sizes.get(t, 0.01) for t in all_tids]
        market_tick_size = min(tick_sizes)
        safe_order_price = (
            self._order_price if market_tick_size < 0.01 else min(self._order_price, 0.99)
        )

        logger.info(
            "[POST_EXPIRY_TEMP] Expiration passed for %s. %s @ %.3f -> BUY %.4f x %.2f",
            slug,
            best_outcome,
            best_price,
            safe_order_price,
            order_size,
        )
        self._ordered_slugs.add(slug)
        self._stop_watching(slug)
        return [
            OrderIntent(
                token_id=best_token,
                price=safe_order_price,
                size=order_size,
                side=Side.BUY,
                strategy=self.name(),
                slug=slug,
                tick_size=market_tick_size,
            )
        ]

    def _start_watching(self, slug: str, eval_data: dict[str, Any]) -> None:
        self._watching[slug] = eval_data
        self._hot_tokens.update(eval_data.get("token_ids", ()))

    def _stop_watching(self, slug: str) -> None:
        eval_data = self._watching.pop(slug, None)
        if not eval_data:
            return
        tids = set(eval_data.get("token_ids", ()))
        still_hot: set[str] = set()
        for other in self._watching.values():
            still_hot.update(other.get("token_ids", ()))
        self._hot_tokens -= tids - still_hot

    def _get_eval(self, slug: str, ctx: StrategyContext) -> dict[str, Any] | None:
        cached = ctx.eval_cache.get(slug)
        if cached:
            eval_data = dict(cached)
        else:
            meta = ctx.market_meta.get(slug)
            if not meta:
                return None
            token_ids = tuple(meta.get("token_ids", ()))
            outcomes = tuple(meta.get("outcomes", ()))
            if len(token_ids) < 2:
                return None
            eval_data = {
                "token_ids": token_ids,
                "outcomes": outcomes,
                "prices": [0.0] * len(token_ids),
                "min_order_size": FALLBACK_MIN_ORDER_SIZE,
            }

        self._refresh_prices(eval_data, ctx)
        if not eval_data.get("best_token_id"):
            return None
        return eval_data


register_strategy(
    "post_expiry_temperature",
    StrategySpec(
        factory=lambda hot_tokens, **_: [
            PostExpiryTemperatureStrategy(hot_tokens=hot_tokens)
        ],
        uses_proximity=False,
    ),
)
