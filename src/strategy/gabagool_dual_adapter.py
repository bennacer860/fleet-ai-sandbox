"""Gabagool dual-sided adapter.

Bridges dual-sided pure logic to the event-driven Strategy interface.
Unlike classic gabagool, this strategy can emit up to two BUY intents
per cycle (YES and NO) while applying cooldown/imbalance/notional guards.
"""

from __future__ import annotations

import math
import os
from dataclasses import dataclass, field

from ..core.events import BookUpdate, MarketResolved, TickSizeChange
from ..core.models import OrderIntent, Side
from ..logging_config import get_logger
from .base import Strategy, StrategyContext
from .gabagool import PairState, TrendDetector
from .gabagool_dual import pick_dual_sizes

logger = get_logger(__name__)


@dataclass
class GabagoolDualConfig:
    max_pair_cost: float = field(default_factory=lambda: float(os.getenv("P1_GABAGOOL_DUAL_MAX_PAIR_COST", "0.98")))
    cooldown_pair_cost: float = field(default_factory=lambda: float(os.getenv("P1_GABAGOOL_DUAL_COOLDOWN_PAIR_COST", "0.995")))
    resume_pair_cost: float = field(default_factory=lambda: float(os.getenv("P1_GABAGOOL_DUAL_RESUME_PAIR_COST", "0.985")))
    max_imbalance: float = field(default_factory=lambda: float(os.getenv("P1_GABAGOOL_DUAL_MAX_IMBALANCE", "3.0")))
    imbalance_throttle_start: float = field(
        default_factory=lambda: float(os.getenv("P1_GABAGOOL_DUAL_IMBALANCE_THROTTLE_START", "1.5"))
    )
    imbalance_throttle_factor: float = field(
        default_factory=lambda: float(os.getenv("P1_GABAGOOL_DUAL_IMBALANCE_THROTTLE_FACTOR", "0.35"))
    )
    base_order_size: float = field(
        default_factory=lambda: float(
            os.getenv("P1_GABAGOOL_DUAL_BASE_ORDER_SIZE", os.getenv("P1_DEFAULT_TRADE_SIZE", "5.0"))
        )
    )
    max_notional_per_slug: float = field(
        default_factory=lambda: float(os.getenv("P1_GABAGOOL_DUAL_MAX_NOTIONAL_PER_SLUG", "250.0"))
    )
    trend_min_reversals: int = field(default_factory=lambda: int(os.getenv("P1_GABAGOOL_DUAL_TREND_MIN_REVERSALS", "0")))
    trend_min_amplitude: float = field(
        default_factory=lambda: float(os.getenv("P1_GABAGOOL_DUAL_TREND_MIN_AMPLITUDE", "0.03"))
    )
    observation_ticks: int = field(default_factory=lambda: int(os.getenv("P1_GABAGOOL_DUAL_OBSERVATION_TICKS", "5")))
    fee_bps: int = field(default_factory=lambda: int(os.getenv("P1_GABAGOOL_DUAL_FEE_BPS", "100")))
    min_order_notional_usd: float = field(
        default_factory=lambda: float(os.getenv("P1_GABAGOOL_DUAL_MIN_ORDER_NOTIONAL_USD", "1.0"))
    )


@dataclass
class DualSlugState:
    pair: PairState
    trend: TrendDetector
    yes_token_id: str
    no_token_id: str
    tick_count: int = 0
    activated: bool = False
    in_cooldown: bool = False


class GabagoolDualStrategy(Strategy):
    """Dual-sided continuous accumulation strategy."""

    def __init__(
        self,
        config: GabagoolDualConfig | None = None,
        hot_tokens: set[str] | None = None,
    ) -> None:
        self._cfg = config or GabagoolDualConfig()
        self._hot_tokens: set[str] = hot_tokens if hot_tokens is not None else set()
        self._slugs: dict[str, DualSlugState] = {}
        self._token_to_slug: dict[str, str] = {}

    def name(self) -> str:
        return "gabagool_dual"

    async def on_tick_size_change(
        self, event: TickSizeChange, ctx: StrategyContext
    ) -> list[OrderIntent] | None:
        self._ensure_slug_state(event.slug, ctx)
        return None

    async def on_book_update(
        self, event: BookUpdate, ctx: StrategyContext
    ) -> list[OrderIntent] | None:
        state = self._ensure_slug_state(event.slug, ctx)
        if state is None:
            return None

        yes_ask = self._get_ask(state.yes_token_id, ctx)
        no_ask = self._get_ask(state.no_token_id, ctx)
        if yes_ask is None or no_ask is None:
            return None

        cfg = self._cfg
        state.tick_count += 1
        state.trend.update(yes_ask)
        combined = yes_ask + no_ask

        if state.tick_count <= cfg.observation_ticks:
            return None

        if not state.activated:
            if state.trend.should_activate(cfg.trend_min_reversals, cfg.trend_min_amplitude):
                state.activated = True
                logger.info(
                    "[GABAGOOL_DUAL] Activated on %s (reversals=%d, amplitude=%.3f)",
                    event.slug, state.trend.reversals, state.trend.amplitude,
                )
            else:
                return None

        # Pause accumulation when spread is too tight; resume if it widens.
        if state.in_cooldown:
            if combined <= cfg.resume_pair_cost:
                state.in_cooldown = False
                logger.info(
                    "[GABAGOOL_DUAL] %s resume (combined=%.4f <= %.4f)",
                    event.slug,
                    combined,
                    cfg.resume_pair_cost,
                )
            else:
                return None
        elif combined >= cfg.cooldown_pair_cost:
            state.in_cooldown = True
            return None

        if combined > cfg.max_pair_cost:
            return None

        notional_left = cfg.max_notional_per_slug - state.pair.total_cost
        if notional_left <= 0:
            return None

        plan = pick_dual_sizes(
            state=state.pair,
            base_order_size=cfg.base_order_size,
            max_imbalance=cfg.max_imbalance,
            imbalance_throttle_start=cfg.imbalance_throttle_start,
            imbalance_throttle_factor=cfg.imbalance_throttle_factor,
        )

        intents: list[OrderIntent] = []
        intents.extend(
            self._build_side_intent(
                slug=event.slug,
                token_id=state.yes_token_id,
                price=yes_ask,
                requested_size=plan.yes_size,
                tick_size=ctx.tick_sizes.get(state.yes_token_id, 0.01),
                notional_left=notional_left,
            )
        )
        # Recompute remaining budget after the YES intent reservation.
        if intents:
            notional_left -= intents[0].price * intents[0].size

        intents.extend(
            self._build_side_intent(
                slug=event.slug,
                token_id=state.no_token_id,
                price=no_ask,
                requested_size=plan.no_size,
                tick_size=ctx.tick_sizes.get(state.no_token_id, 0.01),
                notional_left=notional_left,
            )
        )

        if not intents:
            return None

        logger.info(
            "[GABAGOOL_DUAL] %s intents=%d combined=%.4f ratio=%.2f reason=%s",
            event.slug,
            len(intents),
            combined,
            state.pair.balance_ratio,
            plan.reason,
        )
        return intents

    async def on_market_resolved(
        self, event: MarketResolved, ctx: StrategyContext
    ) -> None:
        state = self._slugs.pop(event.slug, None)
        if state is None:
            return
        self._token_to_slug.pop(state.yes_token_id, None)
        self._token_to_slug.pop(state.no_token_id, None)
        self._hot_tokens.discard(state.yes_token_id)
        self._hot_tokens.discard(state.no_token_id)

    def notify_order_result(self, slug: str, filled: bool) -> None:
        # Fill state synchronization is handled by on_fill_event.
        return

    def on_fill_event(self, token_id: str, fill_size: float, fill_price: float) -> None:
        slug = self._token_to_slug.get(token_id)
        if slug is None:
            return
        state = self._slugs.get(slug)
        if state is None:
            return
        if token_id == state.yes_token_id:
            state.pair.apply_fill("YES", fill_size, fill_price, fee_bps=self._cfg.fee_bps)
        elif token_id == state.no_token_id:
            state.pair.apply_fill("NO", fill_size, fill_price, fee_bps=self._cfg.fee_bps)

    def get_slug_state(self, slug: str) -> DualSlugState | None:
        return self._slugs.get(slug)

    def token_to_slug(self, token_id: str) -> str | None:
        return self._token_to_slug.get(token_id)

    def _ensure_slug_state(self, slug: str, ctx: StrategyContext) -> DualSlugState | None:
        if slug in self._slugs:
            return self._slugs[slug]

        meta = ctx.market_meta.get(slug)
        if not meta:
            return None
        token_ids: tuple[str, ...] = meta.get("token_ids", ())
        outcomes: tuple[str, ...] = meta.get("outcomes", ())
        if len(token_ids) < 2 or len(outcomes) < 2:
            return None

        yes_tid = token_ids[0]
        no_tid = token_ids[1]
        state = DualSlugState(
            pair=PairState(slug=slug),
            trend=TrendDetector(),
            yes_token_id=yes_tid,
            no_token_id=no_tid,
        )
        self._slugs[slug] = state
        self._token_to_slug[yes_tid] = slug
        self._token_to_slug[no_tid] = slug
        self._hot_tokens.update(token_ids)
        return state

    @staticmethod
    def _get_ask(token_id: str, ctx: StrategyContext) -> float | None:
        bp = ctx.best_prices.get(token_id)
        if bp is None:
            return None
        ask = bp.get("ask") or bp.get("best_ask")
        if ask is not None and ask > 0:
            return ask
        bid = bp.get("bid")
        if bid is not None and bid > 0:
            return bid
        return None

    @staticmethod
    def _size_for_min_notional(size: float, price: float, min_notional_usd: float) -> float:
        if size <= 0 or price <= 0 or min_notional_usd <= 0:
            return size
        required = min_notional_usd / price
        adjusted = max(size, required)
        return math.ceil(adjusted * 1_000_000) / 1_000_000

    def _build_side_intent(
        self,
        slug: str,
        token_id: str,
        price: float,
        requested_size: float,
        tick_size: float,
        notional_left: float,
    ) -> list[OrderIntent]:
        if requested_size <= 0 or price <= 0 or notional_left <= 0:
            return []

        size = self._size_for_min_notional(
            requested_size,
            price,
            self._cfg.min_order_notional_usd,
        )
        # Respect per-slug notional budget; skip if we cannot clear min-notional.
        max_affordable_size = max(0.0, notional_left / price)
        if max_affordable_size <= 0:
            return []
        if size > max_affordable_size:
            size = max_affordable_size
            min_required = self._cfg.min_order_notional_usd / price if self._cfg.min_order_notional_usd > 0 else 0.0
            if size + 1e-9 < min_required:
                return []
            size = math.floor(size * 1_000_000) / 1_000_000
            if size <= 0:
                return []

        return [
            OrderIntent(
                token_id=token_id,
                price=price,
                size=size,
                side=Side.BUY,
                strategy=self.name(),
                slug=slug,
                tick_size=tick_size,
                skip_dedup=True,
            )
        ]
