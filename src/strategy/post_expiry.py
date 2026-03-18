"""Post-expiry sweep strategy.

Detects when a market's tick size drops to 0.001.
Waits until just after the market's expiration time.
Buys the most expensive token (the one that won the market).
"""

from __future__ import annotations

import time
import asyncio

from ..core.events import BookUpdate, MarketResolved, TickSizeChange
from ..core.models import OrderIntent, Side
from ..logging_config import get_logger
from ..markets.fifteen_min import extract_market_end_ts
from ..config import DEFAULT_TRADE_SIZE

from .base import Strategy, StrategyContext

logger = get_logger(__name__)

SWEEP_TICK_SIZE = "0.001"
MAX_ORDER_PRICE = 0.999
FALLBACK_MIN_ORDER_SIZE = 5.0

class PostExpirySweepStrategy(Strategy):
    """Buys the winning token just after expiration if tick size is 0.001."""

    def __init__(
        self,
        order_price: float = MAX_ORDER_PRICE,
        hot_tokens: set[str] | None = None,
    ) -> None:
        self._order_price = order_price
        self._watching: dict[str, dict] = {}
        self._hot_tokens: set[str] = hot_tokens if hot_tokens is not None else set()
        self.last_skip_reason: str | None = None
        self.last_watching: bool = False
        self.last_best_price: float | None = None

    def name(self) -> str:
        return "post_expiry"

    async def on_tick_size_change(
        self, event: TickSizeChange, ctx: StrategyContext
    ) -> list[OrderIntent] | None:
        self.last_skip_reason = None
        self.last_watching = False
        self.last_best_price = None

        if event.new_tick_size != SWEEP_TICK_SIZE:
            self.last_skip_reason = f"not a sweep signal (tick_size={event.new_tick_size})"
            return None

        eval_data = self._get_eval(event.slug, event.token_id, ctx)
        if eval_data is None:
            self.last_skip_reason = "no eval data available"
            return None

        self._start_watching(event.slug, eval_data)
        self.last_watching = True
        
        return self._check_and_build_order(event.slug, eval_data, ctx)

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

        eval_data.update({
            "prices": prices,
            "best_idx": best_idx,
            "best_price": best_price,
            "best_outcome": outcomes[best_idx] if best_idx < len(outcomes) else "?",
            "best_token_id": tids[best_idx],
        })

        return self._check_and_build_order(event.slug, eval_data, ctx)

    async def on_market_resolved(
        self, event: MarketResolved, ctx: StrategyContext
    ) -> None:
        self._stop_watching(event.slug)

    def _start_watching(self, slug: str, eval_data: dict) -> None:
        self._watching[slug] = eval_data
        tids = eval_data.get("token_ids", ())
        self._hot_tokens.update(tids)

    def _stop_watching(self, slug: str) -> None:
        eval_data = self._watching.pop(slug, None)
        if eval_data:
            tids = set(eval_data.get("token_ids", ()))
            still_hot = set()
            for other_data in self._watching.values():
                still_hot.update(other_data.get("token_ids", ()))
            self._hot_tokens -= (tids - still_hot)

    def _check_and_build_order(
        self, slug: str, eval_data: dict, ctx: StrategyContext
    ) -> list[OrderIntent] | None:
        end_ts = extract_market_end_ts(slug)
        if end_ts is None:
            self.last_skip_reason = "cannot determine expiration time"
            return None

        tte = end_ts - time.time()
        if tte >= 0:
            self.last_skip_reason = f"waiting for expiration (TTE: {tte:.1f}s)"
            return None

        # Expiration passed! Buy the most expensive token.
        best_price = eval_data["best_price"]
        best_token = eval_data["best_token_id"]
        best_outcome = eval_data["best_outcome"]
        self.last_best_price = best_price

        min_size = eval_data.get("min_order_size", FALLBACK_MIN_ORDER_SIZE)
        order_size = max(DEFAULT_TRADE_SIZE, min_size)

        logger.info(
            "[POST_EXPIRY] Expiration passed for %s. Winning outcome: %s @ %.3f → BUY %.4f x %.2f",
            slug, best_outcome, best_price, self._order_price, order_size,
        )

        self._stop_watching(slug)

        return [OrderIntent(
            token_id=best_token,
            price=self._order_price,
            size=order_size,
            side=Side.BUY,
            strategy=self.name(),
            slug=slug,
            tick_size=float(SWEEP_TICK_SIZE),
        )]

    def _get_eval(
        self, slug: str, token_id: str, ctx: StrategyContext
    ) -> dict | None:
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
