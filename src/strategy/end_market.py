"""End-market strategy.

Arms on sweep tick-size changes and guarantees a single order at/after expiry.
To keep the event bus nimble, this strategy only enables full BookUpdate
ingestion in the final 2 minutes before market expiry.
"""

from __future__ import annotations

import time

from ..core.events import BookUpdate, MarketResolved, TickSizeChange
from ..core.models import OrderIntent, Side
from ..logging_config import get_logger
from ..markets.fifteen_min import extract_market_end_ts
from .base import Strategy, StrategyContext

logger = get_logger(__name__)

SWEEP_TICK_SIZE = "0.001"
END_MARKET_ORDER_SIZE = 5.0
BOOK_INGESTION_LEAD_S = 120.0


class EndMarketStrategy(Strategy):
    """Submit one end-market order at/after expiry."""

    def __init__(self, hot_tokens: set[str] | None = None) -> None:
        self._watching: dict[str, dict] = {}
        self._hot_tokens: set[str] = hot_tokens if hot_tokens is not None else set()
        self._hot_slugs: set[str] = set()
        self._ordered_slugs: set[str] = set()
        self.last_skip_reason: str | None = None
        self.last_watching: bool = False
        self.last_best_price: float | None = None

    def name(self) -> str:
        return "end_market"

    async def on_tick_size_change(
        self, event: TickSizeChange, ctx: StrategyContext
    ) -> list[OrderIntent] | None:
        self.last_skip_reason = None
        self.last_watching = False
        self.last_best_price = None

        if event.new_tick_size != SWEEP_TICK_SIZE:
            self.last_skip_reason = f"not a sweep signal (tick_size={event.new_tick_size})"
            return None

        eval_data = self._get_eval(event.slug, ctx)
        if eval_data is None:
            self.last_skip_reason = "no eval data available"
            return None

        self._start_watching(event.slug, eval_data)
        self.last_watching = True
        self._maybe_activate_hot_tokens(event.slug, eval_data)
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
        all_intents: list[OrderIntent] = []
        for slug, eval_data in list(self._watching.items()):
            self._maybe_activate_hot_tokens(slug, eval_data)
            self._refresh_prices(eval_data, ctx)
            intents = self._check_and_build_order(slug, eval_data, ctx)
            if intents:
                all_intents.extend(intents)
        return all_intents if all_intents else None

    def _start_watching(self, slug: str, eval_data: dict) -> None:
        self._watching[slug] = eval_data

    def _stop_watching(self, slug: str) -> None:
        eval_data = self._watching.pop(slug, None)
        if slug in self._hot_slugs and eval_data:
            tids = set(eval_data.get("token_ids", ()))
            still_hot = set()
            for other_data in self._watching.values():
                still_hot.update(other_data.get("token_ids", ()))
            self._hot_tokens -= (tids - still_hot)
            self._hot_slugs.discard(slug)

    def _maybe_activate_hot_tokens(self, slug: str, eval_data: dict) -> None:
        if slug in self._hot_slugs:
            return

        end_ts = extract_market_end_ts(slug)
        if end_ts is None:
            return

        if end_ts - time.time() > BOOK_INGESTION_LEAD_S:
            return

        tids = set(eval_data.get("token_ids", ()))
        self._hot_tokens.update(tids)
        self._hot_slugs.add(slug)
        logger.info("[END_MARKET] %s activated book ingestion (<= %.0fs to expiry)", slug, BOOK_INGESTION_LEAD_S)

    def _refresh_prices(self, eval_data: dict, ctx: StrategyContext) -> None:
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
        self, slug: str, eval_data: dict, ctx: StrategyContext
    ) -> list[OrderIntent] | None:
        if slug in self._ordered_slugs:
            return None

        end_ts = extract_market_end_ts(slug)
        if end_ts is None:
            self.last_skip_reason = "cannot determine expiration time"
            return None

        tte = end_ts - time.time()
        if tte > 0:
            self.last_skip_reason = f"waiting for expiration (TTE: {tte:.1f}s)"
            return None

        best_price = eval_data["best_price"]
        best_token = eval_data["best_token_id"]
        best_outcome = eval_data["best_outcome"]
        self.last_best_price = best_price

        token_tick_size = ctx.tick_sizes.get(best_token, 0.01)
        safe_order_price = 0.999 if token_tick_size < 0.01 else 0.99

        logger.info(
            "[END_MARKET] Expiration passed for %s. Winning outcome: %s @ %.3f -> BUY %.4f x %.2f",
            slug,
            best_outcome,
            best_price,
            safe_order_price,
            END_MARKET_ORDER_SIZE,
        )

        self._ordered_slugs.add(slug)
        self._stop_watching(slug)

        return [
            OrderIntent(
                token_id=best_token,
                price=safe_order_price,
                size=END_MARKET_ORDER_SIZE,
                side=Side.BUY,
                strategy=self.name(),
                slug=slug,
                tick_size=token_tick_size,
            )
        ]

    def _get_eval(self, slug: str, ctx: StrategyContext) -> dict | None:
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
            }

        self._refresh_prices(eval_data, ctx)
        if not any(p > 0 for p in eval_data["prices"]):
            return None
        return eval_data
