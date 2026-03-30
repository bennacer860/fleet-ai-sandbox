"""Gabagool strategy adapter — bridges pure pair-arbitrage logic to the bot's event-driven Strategy interface.

Wraps the stateless functions and dataclasses from ``gabagool.py`` into
a ``Strategy`` subclass that receives ``BookUpdate`` / ``TickSizeChange``
events, maintains per-slug ``PairState`` / ``TrendDetector`` /
``PhaseManager`` instances, and emits ``OrderIntent`` objects.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from ..core.events import BookUpdate, MarketResolved, TickSizeChange
from ..core.models import OrderIntent, Side
from ..logging_config import get_logger
from .base import Strategy, StrategyContext
from .gabagool import PairState, PhaseManager, TrendDetector, pick_side

logger = get_logger(__name__)


@dataclass
class GabagoolConfig:
    max_pair_cost: float = 0.98
    max_imbalance: float = 2.0
    base_order_size: float = 10.0
    probe_size_factor: float = 0.25
    trend_min_reversals: int = 0
    trend_min_amplitude: float = 0.03
    observation_ticks: int = 5
    fee_bps: int = 0


@dataclass
class SlugState:
    """Per-market state tracked by the adapter."""

    pair: PairState
    trend: TrendDetector
    phase: PhaseManager
    yes_token_id: str
    no_token_id: str
    tick_count: int = 0
    activated: bool = False


class GabagoolStrategy(Strategy):
    """Event-driven adapter for the gabagool pair-arbitrage algorithm."""

    def __init__(
        self,
        config: GabagoolConfig | None = None,
        hot_tokens: set[str] | None = None,
    ) -> None:
        self._cfg = config or GabagoolConfig()
        self._hot_tokens: set[str] = hot_tokens if hot_tokens is not None else set()
        self._slugs: dict[str, SlugState] = {}
        # Reverse lookup: token_id -> slug (for fill notifications)
        self._token_to_slug: dict[str, str] = {}

    def name(self) -> str:
        return "gabagool"

    # ── Event handlers ────────────────────────────────────────────────────

    async def on_tick_size_change(
        self, event: TickSizeChange, ctx: StrategyContext
    ) -> list[OrderIntent] | None:
        self._ensure_slug_state(event.slug, ctx)
        return None

    async def on_book_update(
        self, event: BookUpdate, ctx: StrategyContext
    ) -> list[OrderIntent] | None:
        slug = event.slug
        state = self._ensure_slug_state(slug, ctx)
        if state is None:
            return None

        yes_ask = self._get_ask(state.yes_token_id, ctx)
        no_ask = self._get_ask(state.no_token_id, ctx)
        if yes_ask is None or no_ask is None:
            if state.tick_count == 0:
                logger.debug(
                    "[GABAGOOL] %s: missing ask (yes=%s, no=%s) — skipping",
                    slug, yes_ask, no_ask,
                )
            return None

        cfg = self._cfg
        state.tick_count += 1
        state.trend.update(yes_ask)

        if state.tick_count <= cfg.observation_ticks:
            if state.tick_count == cfg.observation_ticks:
                logger.info(
                    "[GABAGOOL] %s: observation done (%d ticks), checking activation",
                    slug, state.tick_count,
                )
            return None

        if not state.activated:
            if state.trend.should_activate(cfg.trend_min_reversals, cfg.trend_min_amplitude):
                state.activated = True
                logger.info(
                    "[GABAGOOL] Activated on %s (reversals=%d, amplitude=%.3f)",
                    slug, state.trend.reversals, state.trend.amplitude,
                )
            else:
                if state.tick_count % 20 == 0:
                    logger.info(
                        "[GABAGOOL] %s: not yet activated (ticks=%d, reversals=%d/%d, amplitude=%.3f/%.3f)",
                        slug, state.tick_count,
                        state.trend.reversals, cfg.trend_min_reversals,
                        state.trend.amplitude, cfg.trend_min_amplitude,
                    )
                return None

        if state.pair.is_profit_locked:
            state.phase.check_locked(state.pair)
            return None

        size_mult = state.phase.get_size_multiplier()
        if size_mult <= 0:
            return None
        order_size = cfg.base_order_size * size_mult

        side, price, reason = pick_side(
            state.pair,
            yes_ask=yes_ask,
            no_ask=no_ask,
            max_pair_cost=cfg.max_pair_cost,
            max_imbalance=cfg.max_imbalance,
            order_size=order_size,
            fee_bps=cfg.fee_bps,
        )

        if side is None:
            return None

        token_id = state.yes_token_id if side == "YES" else state.no_token_id
        tick_size = ctx.tick_sizes.get(token_id, 0.01)

        logger.info(
            "[GABAGOOL] %s BUY %s @ %.4f x %.2f (phase=%s, pair_cost=%.4f, ratio=%.2f)",
            slug, side, price, order_size, state.phase.phase,
            state.pair.pair_cost if state.pair.qty_yes > 0 and state.pair.qty_no > 0 else float("inf"),
            state.pair.balance_ratio,
        )

        return [OrderIntent(
            token_id=token_id,
            price=price,
            size=order_size,
            side=Side.BUY,
            strategy=self.name(),
            slug=slug,
            tick_size=tick_size,
        )]

    async def on_market_resolved(
        self, event: MarketResolved, ctx: StrategyContext
    ) -> None:
        slug = event.slug
        state = self._slugs.pop(slug, None)
        if state is not None:
            pnl = "LOCKED" if state.pair.is_profit_locked else f"pair_cost={state.pair.pair_cost:.4f}"
            logger.info("[GABAGOOL] Market %s resolved (%s)", slug, pnl)
            self._token_to_slug.pop(state.yes_token_id, None)
            self._token_to_slug.pop(state.no_token_id, None)
            self._hot_tokens.discard(state.yes_token_id)
            self._hot_tokens.discard(state.no_token_id)

    def notify_order_result(self, slug: str, filled: bool) -> None:
        """Called by the bot when an order fills or terminates.

        We don't need to do anything here — fill sync happens via
        ``on_fill_event`` which the bot calls for each ``OrderFill``.
        """

    def on_fill_event(self, token_id: str, fill_size: float, fill_price: float) -> None:
        """Sync a fill back into the gabagool pair state.

        Called by the bot's ``_on_order_fill_notify_strategy`` handler
        so the adapter keeps ``PairState`` and ``PhaseManager`` in sync
        with actual (or simulated) execution.
        """
        slug = self._token_to_slug.get(token_id)
        if slug is None:
            return
        state = self._slugs.get(slug)
        if state is None:
            return

        if token_id == state.yes_token_id:
            side = "YES"
        elif token_id == state.no_token_id:
            side = "NO"
        else:
            return

        state.pair.apply_fill(side, fill_size, fill_price, fee_bps=self._cfg.fee_bps)
        state.phase.record_fill(side)
        state.phase.check_locked(state.pair)
        logger.info(
            "[GABAGOOL] Fill applied: %s %s %.2f @ %.4f → pair_cost=%.4f, locked=%s",
            slug, side, fill_size, fill_price,
            state.pair.pair_cost if state.pair.qty_yes > 0 and state.pair.qty_no > 0 else float("inf"),
            state.pair.is_profit_locked,
        )

    # ── Internal helpers ──────────────────────────────────────────────────

    def _ensure_slug_state(self, slug: str, ctx: StrategyContext) -> SlugState | None:
        """Lazily create per-slug state from market metadata."""
        if slug in self._slugs:
            return self._slugs[slug]

        meta = ctx.market_meta.get(slug)
        if not meta:
            return None

        token_ids: tuple[str, ...] = meta.get("token_ids", ())
        outcomes: tuple[str, ...] = meta.get("outcomes", ())
        if len(token_ids) < 2 or len(outcomes) < 2:
            return None

        # Map outcomes to YES/NO tokens. Polymarket binary markets
        # typically have outcomes ("Up", "Down") or ("Yes", "No").
        # First token is the "YES" equivalent.
        yes_tid = token_ids[0]
        no_tid = token_ids[1]

        state = SlugState(
            pair=PairState(slug=slug),
            trend=TrendDetector(),
            phase=PhaseManager(probe_size_factor=self._cfg.probe_size_factor),
            yes_token_id=yes_tid,
            no_token_id=no_tid,
        )
        self._slugs[slug] = state
        self._token_to_slug[yes_tid] = slug
        self._token_to_slug[no_tid] = slug
        self._hot_tokens.update(token_ids)

        logger.info(
            "[GABAGOOL] Tracking %s (YES=%s… NO=%s…)",
            slug, yes_tid[:16], no_tid[:16],
        )
        return state

    @staticmethod
    def _get_ask(token_id: str, ctx: StrategyContext) -> float | None:
        """Extract best ask price from context for a token."""
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

    def get_slug_state(self, slug: str) -> SlugState | None:
        """Read-only access for testing / monitoring."""
        return self._slugs.get(slug)

    def token_to_slug(self, token_id: str) -> str | None:
        """Resolve a token_id back to its slug."""
        return self._token_to_slug.get(token_id)
