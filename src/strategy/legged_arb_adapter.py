"""Event-driven adapter for 3-phase legged arbitrage."""

from __future__ import annotations

import os
import time
from dataclasses import dataclass, field
from typing import Any

from ..core.events import BookUpdate, MarketResolved, TickSizeChange
from ..core.models import ExecutionPolicy, OrderIntent, Side
from ..logging_config import get_logger
from ..markets.fifteen_min import extract_market_end_ts
from .base import Strategy, StrategyContext
from .legged_arb import (
    ArbState,
    LeggedArbConfig,
    MarketArbState,
    Outcome,
    build_market_book,
    count_active_arbs,
    is_eligible_slug,
    should_buy_phase3,
    should_enter_phase1,
    should_sell_phase2,
)
from .registry import StrategySpec, register_strategy

logger = get_logger(__name__)

LEGGED_ARB_EXECUTION_POLICY = ExecutionPolicy(
    release_dedup_on_rejection=True,
    release_dedup_on_partial_terminal=True,
    release_dedup_on_fill=True,
    enforce_min_notional=True,
)


def _parse_csv_env(name: str, default: str) -> tuple[str, ...]:
    raw = os.getenv(name, default)
    return tuple(part.strip().upper() for part in raw.split(",") if part.strip())


def _parse_int_csv_env(name: str, default: str) -> tuple[int, ...]:
    raw = os.getenv(name, default)
    return tuple(int(part.strip()) for part in raw.split(",") if part.strip())


def legged_arb_config_from_env() -> LeggedArbConfig:
    """Load legged-arb config from environment variables."""
    return LeggedArbConfig(
        markets=_parse_csv_env("LEGGED_ARB_MARKETS", "BTC"),
        durations=_parse_int_csv_env("LEGGED_ARB_DURATIONS", "15"),
        phase1_price_min=float(os.getenv("LEGGED_ARB_PHASE1_PRICE_MIN", "0.70")),
        phase1_price_max=float(os.getenv("LEGGED_ARB_PHASE1_PRICE_MAX", "0.95")),
        phase1_tte_min_s=float(os.getenv("LEGGED_ARB_PHASE1_TTE_MIN", "300")),
        phase1_tte_max_s=float(os.getenv("LEGGED_ARB_PHASE1_TTE_MAX", "840")),
        max_spread=float(os.getenv("LEGGED_ARB_MAX_SPREAD", "0.02")),
        min_ask_depth=float(os.getenv("LEGGED_ARB_MIN_ASK_DEPTH", "1000")),
        max_concurrent=int(os.getenv("LEGGED_ARB_MAX_CONCURRENT", "10")),
        clip_size=float(os.getenv("LEGGED_ARB_CLIP_SIZE", "150")),
        phase2_uplift=float(os.getenv("LEGGED_ARB_PHASE2_UPLIFT", "0.14")),
        phase2_abs_bid=float(os.getenv("LEGGED_ARB_PHASE2_ABS", "0.90")),
        phase2_min_tte_s=float(os.getenv("LEGGED_ARB_PHASE2_MIN_TTE", "60")),
        phase3_max_price=float(os.getenv("LEGGED_ARB_PHASE3_MAX_PRICE", "0.05")),
        phase3_max_tte_s=float(os.getenv("LEGGED_ARB_PHASE3_MAX_TTE", "180")),
        phase3_min_tte_s=float(os.getenv("LEGGED_ARB_PHASE3_MIN_TTE", "30")),
        min_order_notional_usd=float(os.getenv("LEGGED_ARB_MIN_NOTIONAL_USD", "1.0")),
        min_shares=float(os.getenv("LEGGED_ARB_MIN_SHARES", "5")),
    )


@dataclass
class LeggedArbStats:
    phase1_attempts: int = 0
    phase1_fills: int = 0
    phase2_attempts: int = 0
    phase2_fills: int = 0
    phase3_attempts: int = 0
    phase3_fills: int = 0
    completed_arbs: int = 0
    markets_seen: int = 0


class LeggedArbStrategy(Strategy):
    """3-phase legged arbitrage on configured crypto Up/Down markets."""

    def __init__(
        self,
        config: LeggedArbConfig | None = None,
        hot_tokens: set[str] | None = None,
    ) -> None:
        self._cfg = config or legged_arb_config_from_env()
        self._hot_tokens: set[str] = hot_tokens if hot_tokens is not None else set()
        self._states: dict[str, MarketArbState] = {}
        self._token_to_slug: dict[str, str] = {}
        self._stats = LeggedArbStats()
        self.last_skip_reason: str = ""

    def name(self) -> str:
        return "legged_arb"

    @property
    def stats(self) -> LeggedArbStats:
        return self._stats

    def dashboard_snapshot(self) -> dict[str, Any]:
        active = count_active_arbs(self._states)
        by_phase: dict[str, int] = {}
        for state in self._states.values():
            by_phase[state.phase.value] = by_phase.get(state.phase.value, 0) + 1
        return {
            "phase1_attempts": self._stats.phase1_attempts,
            "phase1_fills": self._stats.phase1_fills,
            "phase2_attempts": self._stats.phase2_attempts,
            "phase2_fills": self._stats.phase2_fills,
            "phase3_attempts": self._stats.phase3_attempts,
            "phase3_fills": self._stats.phase3_fills,
            "completed_arbs": self._stats.completed_arbs,
            "active_arbs": active,
            "tracked_markets": len(self._states),
            "by_phase": by_phase,
            "last_skip_reason": self.last_skip_reason,
            "config": {
                "markets": list(self._cfg.markets),
                "durations": list(self._cfg.durations),
                "phase1_price_min": self._cfg.phase1_price_min,
                "phase1_price_max": self._cfg.phase1_price_max,
                "clip_size": self._cfg.clip_size,
            },
        }

    async def on_tick_size_change(
        self, event: TickSizeChange, ctx: StrategyContext
    ) -> list[OrderIntent] | None:
        self._ensure_slug_state(event.slug, ctx)
        return None

    async def on_book_update(
        self, event: BookUpdate, ctx: StrategyContext
    ) -> list[OrderIntent] | None:
        slug = event.slug
        if not is_eligible_slug(slug, self._cfg):
            return None

        state = self._ensure_slug_state(slug, ctx)
        if state is None:
            return None

        end_ts = extract_market_end_ts(slug)
        if end_ts is None:
            self.last_skip_reason = "unknown expiry"
            return None
        tte_s = end_ts - time.time()

        book = self._build_book(state, ctx)
        if book is None:
            self.last_skip_reason = "missing book"
            return None

        if state.phase == ArbState.IDLE:
            decision = should_enter_phase1(
                book,
                tte_s,
                self._cfg,
                active_arb_count=count_active_arbs(self._states),
            )
            if not decision.enter or decision.side is None:
                self.last_skip_reason = decision.reason
                state.last_skip_reason = decision.reason
                return None

            token_id = self._token_for_outcome(state, decision.side)
            tick_size = ctx.tick_sizes.get(token_id, 0.01)
            state.phase = ArbState.PHASE1_PENDING
            state.phase1_side = decision.side
            state.phase1_entry_price = decision.price
            state.phase1_target_size = decision.size
            state.phase3_target_size = decision.size
            self._stats.phase1_attempts += 1
            self.last_skip_reason = ""
            logger.info(
                "[LEGGED_ARB] %s PHASE1 BUY %s @ %.4f x %.2f (tte=%.0fs)",
                slug,
                decision.side,
                decision.price,
                decision.size,
                tte_s,
            )
            return [
                OrderIntent(
                    token_id=token_id,
                    price=decision.price,
                    size=decision.size,
                    side=Side.BUY,
                    strategy=self.name(),
                    slug=slug,
                    tick_size=tick_size,
                    execution_policy=LEGGED_ARB_EXECUTION_POLICY,
                )
            ]

        if state.phase == ArbState.PHASE1_FILLED:
            decision = should_sell_phase2(state, book, tte_s, self._cfg)
            if not decision.sell or decision.side is None:
                self.last_skip_reason = decision.reason
                state.last_skip_reason = decision.reason
                return None

            token_id = self._token_for_outcome(state, decision.side)
            tick_size = ctx.tick_sizes.get(token_id, 0.01)
            state.phase = ArbState.PHASE2_PENDING
            state.phase2_target_size = decision.size
            self._stats.phase2_attempts += 1
            self.last_skip_reason = ""
            logger.info(
                "[LEGGED_ARB] %s PHASE2 SELL %s @ %.4f x %.2f (tte=%.0fs)",
                slug,
                decision.side,
                decision.price,
                decision.size,
                tte_s,
            )
            return [
                OrderIntent(
                    token_id=token_id,
                    price=decision.price,
                    size=decision.size,
                    side=Side.SELL,
                    strategy=self.name(),
                    slug=slug,
                    tick_size=tick_size,
                    execution_policy=LEGGED_ARB_EXECUTION_POLICY,
                )
            ]

        if state.phase == ArbState.PHASE2_SOLD:
            decision = should_buy_phase3(state, book, tte_s, self._cfg)
            if not decision.buy or decision.side is None:
                self.last_skip_reason = decision.reason
                state.last_skip_reason = decision.reason
                return None

            token_id = self._token_for_outcome(state, decision.side)
            tick_size = ctx.tick_sizes.get(token_id, 0.01)
            state.phase = ArbState.PHASE3_PENDING
            state.phase3_target_size = decision.size
            self._stats.phase3_attempts += 1
            self.last_skip_reason = ""
            logger.info(
                "[LEGGED_ARB] %s PHASE3 BUY %s @ %.4f x %.2f (tte=%.0fs)",
                slug,
                decision.side,
                decision.price,
                decision.size,
                tte_s,
            )
            return [
                OrderIntent(
                    token_id=token_id,
                    price=decision.price,
                    size=decision.size,
                    side=Side.BUY,
                    strategy=self.name(),
                    slug=slug,
                    tick_size=tick_size,
                    execution_policy=LEGGED_ARB_EXECUTION_POLICY,
                )
            ]

        return None

    async def on_market_resolved(
        self, event: MarketResolved, ctx: StrategyContext
    ) -> None:
        slug = event.slug
        state = self._states.pop(slug, None)
        if state is None:
            return

        self._token_to_slug.pop(state.yes_token_id, None)
        self._token_to_slug.pop(state.no_token_id, None)
        self._hot_tokens.discard(state.yes_token_id)
        self._hot_tokens.discard(state.no_token_id)

        if state.phase == ArbState.PHASE3_FILLED:
            self._stats.completed_arbs += 1

        logger.info(
            "[LEGGED_ARB] %s resolved phase=%s p1=%.2f@%.4f p3=%.2f",
            slug,
            state.phase.value,
            state.phase1_filled_size,
            state.phase1_entry_price,
            state.phase3_filled_size,
        )

    def notify_order_result(self, slug: str, filled: bool) -> None:
        state = self._states.get(slug)
        if state is None or filled:
            return

        if state.phase == ArbState.PHASE1_PENDING:
            state.phase = ArbState.IDLE
            state.phase1_side = None
            state.phase1_entry_price = 0.0
            state.phase1_target_size = 0.0
        elif state.phase == ArbState.PHASE2_PENDING:
            state.phase = ArbState.PHASE1_FILLED
            state.phase2_target_size = 0.0
        elif state.phase == ArbState.PHASE3_PENDING:
            state.phase = ArbState.PHASE2_SOLD

    def on_fill_event(self, token_id: str, fill_size: float, fill_price: float) -> None:
        slug = self._token_to_slug.get(token_id)
        if slug is None:
            return
        state = self._states.get(slug)
        if state is None:
            return

        outcome = self._outcome_for_token(state, token_id)
        if outcome is None:
            return

        if state.phase == ArbState.PHASE1_PENDING and outcome == state.phase1_side:
            prev = state.phase1_filled_size
            state.phase1_filled_size += fill_size
            state.phase1_filled_cost += fill_size * fill_price
            if prev <= 0 and state.phase1_filled_size > 0:
                state.phase1_entry_price = fill_price
            elif state.phase1_filled_size > 0:
                state.phase1_entry_price = state.phase1_filled_cost / state.phase1_filled_size
            if state.phase1_filled_size + 1e-9 >= state.phase1_target_size:
                state.phase = ArbState.PHASE1_FILLED
                self._stats.phase1_fills += 1
            logger.info(
                "[LEGGED_ARB] Phase1 fill %s: %.2f @ %.4f (held=%.2f)",
                slug,
                fill_size,
                fill_price,
                state.phase1_filled_size,
            )
            return

        if state.phase == ArbState.PHASE2_PENDING and outcome == state.phase1_side:
            state.phase2_sold_size += fill_size
            state.phase1_filled_size = max(state.phase1_filled_size - fill_size, 0.0)
            if state.phase2_sold_size + 1e-9 >= state.phase2_target_size:
                state.phase = ArbState.PHASE2_SOLD
                self._stats.phase2_fills += 1
            logger.info(
                "[LEGGED_ARB] Phase2 fill %s: sold %.2f @ %.4f (remaining=%.2f)",
                slug,
                fill_size,
                fill_price,
                state.phase1_filled_size,
            )
            return

        if state.phase == ArbState.PHASE3_PENDING and outcome != state.phase1_side:
            state.phase3_filled_size += fill_size
            state.phase3_filled_cost += fill_size * fill_price
            if state.phase3_filled_size + 1e-9 >= state.phase3_target_size:
                state.phase = ArbState.PHASE3_FILLED
                self._stats.phase3_fills += 1
            logger.info(
                "[LEGGED_ARB] Phase3 fill %s: %.2f @ %.4f (held=%.2f)",
                slug,
                fill_size,
                fill_price,
                state.phase3_filled_size,
            )

    def get_slug_state(self, slug: str) -> MarketArbState | None:
        return self._states.get(slug)

    def _ensure_slug_state(self, slug: str, ctx: StrategyContext) -> MarketArbState | None:
        if slug in self._states:
            return self._states[slug]

        if not is_eligible_slug(slug, self._cfg):
            return None

        meta = ctx.market_meta.get(slug)
        if not meta:
            return None

        token_ids = meta.get("token_ids")
        outcomes = meta.get("outcomes")
        if not token_ids or len(token_ids) < 2:
            return None

        yes_id, no_id = token_ids[0], token_ids[1]
        out_tuple = tuple(outcomes) if outcomes else ("Up", "Down")
        state = MarketArbState(
            slug=slug,
            yes_token_id=yes_id,
            no_token_id=no_id,
            outcomes=(out_tuple[0], out_tuple[1]),
        )
        self._states[slug] = state
        self._token_to_slug[yes_id] = slug
        self._token_to_slug[no_id] = slug
        self._hot_tokens.add(yes_id)
        self._hot_tokens.add(no_id)
        self._stats.markets_seen += 1
        logger.info("[LEGGED_ARB] Tracking %s", slug)
        return state

    def _build_book(self, state: MarketArbState, ctx: StrategyContext) -> Any | None:
        up = self._side_metrics(state.yes_token_id, ctx)
        down = self._side_metrics(state.no_token_id, ctx)
        if up is None or down is None:
            return None
        return build_market_book(
            up_bid=up["bid"],
            up_ask=up["ask"],
            up_ask_size=up["ask_size"],
            up_bid_depth=up["bid_depth"],
            up_ask_depth=up["ask_depth"],
            down_bid=down["bid"],
            down_ask=down["ask"],
            down_ask_size=down["ask_size"],
            down_bid_depth=down["bid_depth"],
            down_ask_depth=down["ask_depth"],
        )

    @staticmethod
    def _side_metrics(token_id: str, ctx: StrategyContext) -> dict[str, float] | None:
        bp = ctx.best_prices.get(token_id)
        if bp is None:
            return None
        bid = float(bp.get("bid") or bp.get("best_bid") or 0.0)
        ask = float(bp.get("ask") or bp.get("best_ask") or 0.0)
        if bid <= 0 and ask <= 0:
            return None
        return {
            "bid": bid,
            "ask": ask,
            "ask_size": float(bp.get("ask_size") or bp.get("best_ask_size") or 100.0),
            "bid_depth": float(bp.get("bid_depth") or 1000.0),
            "ask_depth": float(bp.get("ask_depth") or 1000.0),
        }

    @staticmethod
    def _token_for_outcome(state: MarketArbState, outcome: Outcome) -> str:
        if outcome == "Up":
            return state.yes_token_id
        return state.no_token_id

    @staticmethod
    def _outcome_for_token(state: MarketArbState, token_id: str) -> Outcome | None:
        if token_id == state.yes_token_id:
            return "Up"
        if token_id == state.no_token_id:
            return "Down"
        return None


register_strategy(
    "legged_arb",
    StrategySpec(
        factory=lambda hot_tokens, **_: [LeggedArbStrategy(hot_tokens=hot_tokens)],
        needs_full_book_updates=True,
    ),
)
