"""Aggressive post-expiry strategy — multi-phase, multi-attempt.

Phase 1 (pre-tick-change): As soon as the market expires, buys the most
expensive token at the current tick size (price capped at 0.99 with
tick_size 0.01). Retries on rejection every POLL_INTERVAL seconds.

Phase 2 (post-tick-change): When the tick size drops to 0.001, escalates
the bid to 0.999 and retries.  Keeps retrying until filled, resolved,
or MAX_RETRIES exhausted.

Uses skip_dedup=True so the OrderManager allows repeated submissions
for the same slug/token.
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field

from ..core.events import BookUpdate, MarketResolved, TickSizeChange
from ..core.models import OrderIntent, Side
from ..logging_config import get_logger
from ..markets.fifteen_min import extract_market_end_ts, extract_market_from_slug, detect_duration_from_slug
from ..config import (
    AGGRESSIVE_MAX_RETRIES,
    AGGRESSIVE_PHASE1_PRICE,
    AGGRESSIVE_PHASE2_PRICE,
    AGGRESSIVE_POLL_INTERVAL_S,
    DEFAULT_TRADE_SIZE,
    TRADE_SIZE_60M,
    POST_EXPIRY_MULTIPLIER,
)

from .base import Strategy, StrategyContext

logger = get_logger(__name__)

SWEEP_TICK_SIZE = "0.001"
FALLBACK_MIN_ORDER_SIZE = 5.0


@dataclass
class MarketState:
    """Per-market tracking for the aggressive strategy."""
    eval_data: dict
    end_ts: float
    phase: int = 1  # 1 = pre-tick-change, 2 = post-tick-change (0.001)
    attempts: int = 0
    has_live_order: bool = False
    last_attempt_time: float = 0.0
    filled: bool = False
    current_tick_size: str = "0.01"


class AggressivePostExpirySweepStrategy(Strategy):
    """Multi-phase aggressive buyer after market expiration."""

    def __init__(
        self,
        price_threshold: float = 0.95,
        hot_tokens: set[str] | None = None,
    ) -> None:
        self._price_threshold = price_threshold
        self._markets: dict[str, MarketState] = {}
        self._hot_tokens: set[str] = hot_tokens if hot_tokens is not None else set()
        self.last_skip_reason: str | None = None
        self.last_watching: bool = False
        self.last_best_price: float | None = None

    def name(self) -> str:
        return "aggressive_post_expiry"

    # ── Event handlers ────────────────────────────────────────────────────

    async def on_tick_size_change(
        self, event: TickSizeChange, ctx: StrategyContext
    ) -> list[OrderIntent] | None:
        self.last_skip_reason = None
        self.last_watching = False
        self.last_best_price = None

        eval_data = self._get_eval(event.slug, event.token_id, ctx)
        if eval_data is None:
            self.last_skip_reason = "no eval data available"
            return None

        state = self._markets.get(event.slug)

        if state is None:
            end_ts = extract_market_end_ts(event.slug)
            if end_ts is None:
                self.last_skip_reason = "cannot determine expiration time"
                return None
            state = MarketState(eval_data=eval_data, end_ts=end_ts)
            self._markets[event.slug] = state
            self._register_hot_tokens(eval_data)
            self.last_watching = True

        state.eval_data = eval_data

        if event.new_tick_size == SWEEP_TICK_SIZE and state.phase == 1:
            logger.info(
                "[AGG] %s: tick_size → 0.001, escalating to phase 2",
                event.slug,
            )
            state.phase = 2
            state.has_live_order = False
            state.attempts = 0

        tte = state.end_ts - time.time()
        if tte > 0:
            self.last_skip_reason = f"waiting for expiration (TTE: {tte:.1f}s)"
            self.last_watching = True
            return None

        return self._try_order(event.slug, state, ctx)

    async def on_book_update(
        self, event: BookUpdate, ctx: StrategyContext
    ) -> list[OrderIntent] | None:
        state = self._markets.get(event.slug)
        if state is None:
            return None

        self._refresh_prices(state, ctx)

        tte = state.end_ts - time.time()
        if tte > 0:
            return None

        return self._try_order(event.slug, state, ctx)

    async def on_market_resolved(
        self, event: MarketResolved, ctx: StrategyContext
    ) -> None:
        self._remove_market(event.slug)

    async def poll(self, ctx: StrategyContext) -> list[OrderIntent] | None:
        """Called periodically by the bot — drives retry logic."""
        now = time.time()
        all_intents: list[OrderIntent] = []

        for slug, state in list(self._markets.items()):
            if state.filled:
                continue

            tte = state.end_ts - now
            if tte > 0:
                continue

            if state.attempts >= AGGRESSIVE_MAX_RETRIES:
                continue

            if state.has_live_order:
                continue

            elapsed = now - state.last_attempt_time
            if elapsed < AGGRESSIVE_POLL_INTERVAL_S:
                continue

            self._refresh_prices(state, ctx)
            intents = self._try_order(slug, state, ctx)
            if intents:
                all_intents.extend(intents)

        return all_intents if all_intents else None

    # ── Order building ────────────────────────────────────────────────────

    def _try_order(
        self, slug: str, state: MarketState, ctx: StrategyContext
    ) -> list[OrderIntent] | None:
        if state.filled:
            return None

        if state.attempts >= AGGRESSIVE_MAX_RETRIES:
            self.last_skip_reason = f"max retries ({AGGRESSIVE_MAX_RETRIES}) exhausted"
            return None

        if state.has_live_order:
            self.last_skip_reason = "order already live"
            return None

        eval_data = state.eval_data
        best_price = eval_data.get("best_price", 0)
        best_token = eval_data.get("best_token_id")
        best_outcome = eval_data.get("best_outcome", "?")
        self.last_best_price = best_price

        if not best_token:
            self.last_skip_reason = "no best token identified"
            return None

        if best_price < self._price_threshold:
            self.last_skip_reason = (
                f"price {best_price:.3f} < {self._price_threshold:.2f} — waiting for convergence"
            )
            return None

        # Check if we already have a fill via positions
        pos = ctx.positions.get(best_token)
        if pos is not None and pos.quantity > 0:
            state.filled = True
            self.last_skip_reason = "already filled (position exists)"
            return None

        target_tick_size = ctx.tick_sizes.get(best_token, 0.01)
        if state.phase == 2:
            order_price = AGGRESSIVE_PHASE2_PRICE
        else:
            order_price = AGGRESSIVE_PHASE1_PRICE
            
        tick_size = target_tick_size
        if tick_size >= 0.01:
            order_price = min(order_price, 0.99)

        min_size = eval_data.get("min_order_size", FALLBACK_MIN_ORDER_SIZE)
        
        market_duration = detect_duration_from_slug(slug) or 15
        base_trade_size = TRADE_SIZE_60M if market_duration == 60 else DEFAULT_TRADE_SIZE
        order_size = max(base_trade_size, min_size) * POST_EXPIRY_MULTIPLIER

        state.attempts += 1
        state.last_attempt_time = time.time()
        state.has_live_order = True

        phase_label = "P2" if state.phase == 2 else "P1"
        logger.info(
            "[AGG] %s [%s] attempt %d/%d: %s @ %.3f → BUY %.4f x %.2f",
            slug, phase_label, state.attempts, AGGRESSIVE_MAX_RETRIES,
            best_outcome, best_price, order_price, order_size,
        )

        return [OrderIntent(
            token_id=best_token,
            price=order_price,
            size=order_size,
            side=Side.BUY,
            strategy=self.name(),
            slug=slug,
            tick_size=tick_size,
            skip_dedup=True,
        )]

    def notify_order_result(self, slug: str, filled: bool) -> None:
        """Called by the bot after an order resolves (filled or rejected).

        Clears has_live_order so poll() can retry on rejection.
        """
        state = self._markets.get(slug)
        if state is None:
            return
        state.has_live_order = False
        if filled:
            state.filled = True
            logger.info("[AGG] %s: filled, stopping retries", slug)

    # ── Helpers ───────────────────────────────────────────────────────────

    def _refresh_prices(self, state: MarketState, ctx: StrategyContext) -> None:
        eval_data = state.eval_data
        tids = eval_data.get("token_ids", ())
        outcomes = eval_data.get("outcomes", ())
        prices = list(eval_data.get("prices", [0.0] * len(tids)))

        for i, tid in enumerate(tids):
            rt = ctx.best_prices.get(tid, {}).get("bid")
            if rt is not None and rt > 0:
                prices[i] = rt

        if not any(p > 0 for p in prices):
            return

        best_idx = max(range(len(prices)), key=lambda i: prices[i])
        eval_data.update({
            "prices": prices,
            "best_idx": best_idx,
            "best_price": prices[best_idx],
            "best_outcome": outcomes[best_idx] if best_idx < len(outcomes) else "?",
            "best_token_id": tids[best_idx],
        })

    def _register_hot_tokens(self, eval_data: dict) -> None:
        tids = eval_data.get("token_ids", ())
        self._hot_tokens.update(tids)

    def _remove_market(self, slug: str) -> None:
        state = self._markets.pop(slug, None)
        if state:
            tids = set(state.eval_data.get("token_ids", ()))
            still_hot = set()
            for other in self._markets.values():
                still_hot.update(other.eval_data.get("token_ids", ()))
            self._hot_tokens -= (tids - still_hot)
            logger.info("[AGG] %s: removed (resolved/done)", slug)

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
