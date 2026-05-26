"""Event-driven adapter for portfolio-hedged cheap-side buying."""

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
from .cheap_side import (
    CheapSideConfig,
    CheapSidePick,
    get_order_size,
    is_btc_5min_slug,
    pick_cheap_side,
    should_buy_for_outcome,
)
from .registry import StrategySpec, register_strategy

logger = get_logger(__name__)

CHEAP_SIDE_EXECUTION_POLICY = ExecutionPolicy(
    release_dedup_on_rejection=True,
    enforce_min_notional=True,
)


@dataclass
class SlugState:
    yes_token_id: str
    no_token_id: str
    outcomes: tuple[str, ...]
    bought: bool = False
    pending_cost: float = 0.0
    filled_shares: float = 0.0
    filled_cost: float = 0.0
    outcome: str | None = None
    held_token_id: str | None = None
    avg_fill_price: float = 0.0


@dataclass
class PortfolioState:
    initial_capital: float
    available_capital: float
    portfolio_up_cost: float = 0.0
    portfolio_down_cost: float = 0.0
    markets_traded: int = 0
    wins: int = 0
    losses: int = 0
    total_pnl: float = 0.0
    order_attempts: int = 0
    order_fills: int = 0
    total_shares_bought: float = 0.0
    total_cost: float = 0.0
    recent_positions: list[dict[str, Any]] = field(default_factory=list)

    @property
    def deployed_capital(self) -> float:
        return self.total_cost - self.available_capital + self.initial_capital - (
            self.initial_capital - self.available_capital
        )

    @property
    def net_capital(self) -> float:
        return self.initial_capital + self.total_pnl

    def record_recent(self, entry: dict[str, Any], max_len: int = 8) -> None:
        self.recent_positions.insert(0, entry)
        if len(self.recent_positions) > max_len:
            self.recent_positions.pop()


class CheapSideStrategy(Strategy):
    """Buy the cheap side of BTC 5-min markets with portfolio-level balance."""

    def __init__(
        self,
        config: CheapSideConfig | None = None,
        hot_tokens: set[str] | None = None,
    ) -> None:
        self._cfg = config or CheapSideConfig(
            initial_capital=float(os.getenv("CHEAP_SIDE_INITIAL_CAPITAL", "1000")),
            max_per_market_usd=float(os.getenv("CHEAP_SIDE_MAX_PER_MARKET_USD", "50")),
            min_order_notional_usd=float(os.getenv("CHEAP_SIDE_MIN_NOTIONAL_USD", "1")),
            max_balance_ratio=float(os.getenv("CHEAP_SIDE_MAX_BALANCE_RATIO", "1.3")),
        )
        self._hot_tokens: set[str] = hot_tokens if hot_tokens is not None else set()
        self._slugs: dict[str, SlugState] = {}
        self._token_to_slug: dict[str, str] = {}
        self._portfolio = PortfolioState(
            initial_capital=self._cfg.initial_capital,
            available_capital=self._cfg.initial_capital,
        )
        self.last_skip_reason: str = ""
        self.last_best_price: float | None = None

    def name(self) -> str:
        return "cheap_side"

    @property
    def portfolio(self) -> PortfolioState:
        return self._portfolio

    def dashboard_snapshot(self) -> dict[str, Any]:
        p = self._portfolio
        up_m = sum(1 for s in self._slugs.values() if s.outcome == "Up" and s.filled_shares > 0)
        dn_m = sum(1 for s in self._slugs.values() if s.outcome == "Down" and s.filled_shares > 0)
        total_m = up_m + dn_m
        up_pct = (up_m / total_m * 100) if total_m else 0.0
        dn_pct = (dn_m / total_m * 100) if total_m else 0.0
        wr = (p.wins / (p.wins + p.losses) * 100) if (p.wins + p.losses) else 0.0
        avg_price = p.total_cost / p.total_shares_bought if p.total_shares_bought > 0 else 0.0
        fill_rate = (p.order_fills / p.order_attempts * 100) if p.order_attempts else 0.0
        roi = (p.total_pnl / p.initial_capital * 100) if p.initial_capital else 0.0
        open_deployed = sum(
            s.filled_cost for s in self._slugs.values() if s.filled_shares > 0
        )
        return {
            "initial_capital": p.initial_capital,
            "available_capital": p.available_capital,
            "deployed_capital": open_deployed,
            "total_pnl": p.total_pnl,
            "roi_pct": roi,
            "markets_traded": p.markets_traded,
            "wins": p.wins,
            "losses": p.losses,
            "win_rate_pct": wr,
            "up_markets": up_m,
            "down_markets": dn_m,
            "up_pct": up_pct,
            "down_pct": dn_pct,
            "avg_entry_price": avg_price,
            "order_attempts": p.order_attempts,
            "order_fills": p.order_fills,
            "fill_rate_pct": fill_rate,
            "recent_positions": list(p.recent_positions),
            "active_slugs": len(self._slugs),
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
        if not is_btc_5min_slug(slug):
            return None

        state = self._ensure_slug_state(slug, ctx)
        if state is None or state.bought:
            return None

        yes_ask = self._get_ask(state.yes_token_id, ctx)
        no_ask = self._get_ask(state.no_token_id, ctx)
        if yes_ask is None or no_ask is None:
            self.last_skip_reason = "missing ask prices"
            return None

        pick = pick_cheap_side(yes_ask, no_ask)
        if pick is None:
            self.last_skip_reason = "no cheap side"
            return None

        self.last_best_price = pick.price

        end_ts = extract_market_end_ts(slug)
        if end_ts is None:
            self.last_skip_reason = "unknown expiry"
            return None
        tte_s = end_ts - time.time()

        outcome_label = (
            state.outcomes[pick.token_index]
            if pick.token_index < len(state.outcomes)
            else pick.outcome
        )
        token_id = (
            state.yes_token_id if pick.token_index == 0 else state.no_token_id
        )
        ask_liquidity = self._ask_liquidity_at_price(event, pick.price)

        size = get_order_size(
            pick.price,
            self._cfg,
            available_capital=self._portfolio.available_capital,
            market_spent_usd=state.filled_cost,
            ask_liquidity=ask_liquidity,
        )
        if size <= 0:
            self.last_skip_reason = "size zero (capital/liquidity/min)"
            return None

        order_cost = size * pick.price
        ok, reason = should_buy_for_outcome(
            outcome_label if outcome_label in ("Up", "Down") else pick.outcome,
            pick.price,
            tte_s,
            self._cfg,
            already_bought=state.bought,
            portfolio_up_cost=self._portfolio.portfolio_up_cost,
            portfolio_down_cost=self._portfolio.portfolio_down_cost,
            available_capital=self._portfolio.available_capital,
            order_cost=order_cost,
        )
        if not ok:
            self.last_skip_reason = reason
            return None

        self.last_skip_reason = ""
        tick_size = ctx.tick_sizes.get(token_id, 0.01)
        self._portfolio.order_attempts += 1
        state.pending_cost += order_cost
        state.outcome = pick.outcome

        logger.info(
            "[CHEAP_SIDE] %s BUY %s @ %.4f x %.2f (tte=%.0fs, avail=$%.2f)",
            slug,
            outcome_label,
            pick.price,
            size,
            tte_s,
            self._portfolio.available_capital,
        )

        return [
            OrderIntent(
                token_id=token_id,
                price=pick.price,
                size=size,
                side=Side.BUY,
                strategy=self.name(),
                slug=slug,
                tick_size=tick_size,
                execution_policy=CHEAP_SIDE_EXECUTION_POLICY,
            )
        ]

    async def on_market_resolved(
        self, event: MarketResolved, ctx: StrategyContext
    ) -> None:
        slug = event.slug
        state = self._slugs.pop(slug, None)
        if state is None:
            return

        self._token_to_slug.pop(state.yes_token_id, None)
        self._token_to_slug.pop(state.no_token_id, None)
        self._hot_tokens.discard(state.yes_token_id)
        self._hot_tokens.discard(state.no_token_id)

        if state.filled_shares <= 0:
            return

        held_token = state.held_token_id or state.yes_token_id
        won = event.winning_token_id == held_token
        if won:
            self._portfolio.wins += 1
        else:
            self._portfolio.losses += 1

        payout = state.filled_shares * 1.0 if won else 0.0
        pnl = payout - state.filled_cost
        self._portfolio.total_pnl += pnl
        self._portfolio.available_capital += payout

        self._portfolio.record_recent(
            {
                "slug": slug,
                "outcome": state.outcome,
                "shares": state.filled_shares,
                "avg_price": state.avg_fill_price,
                "cost": state.filled_cost,
                "pnl": pnl,
                "won": won,
            }
        )

        logger.info(
            "[CHEAP_SIDE] %s resolved %s: %s shares @ avg %.4f, pnl=%+.2f, capital=$%.2f",
            slug,
            "WIN" if won else "LOSS",
            state.filled_shares,
            state.avg_fill_price,
            pnl,
            self._portfolio.available_capital,
        )

    def on_fill_event(self, token_id: str, fill_size: float, fill_price: float) -> None:
        slug = self._token_to_slug.get(token_id)
        if slug is None:
            return
        state = self._slugs.get(slug)
        if state is None:
            return

        cost = fill_size * fill_price
        self._portfolio.available_capital -= cost
        self._portfolio.order_fills += 1
        self._portfolio.total_shares_bought += fill_size
        self._portfolio.total_cost += cost

        prev_shares = state.filled_shares
        state.filled_shares += fill_size
        state.filled_cost += cost
        if state.filled_shares > 0:
            state.avg_fill_price = state.filled_cost / state.filled_shares

        if token_id == state.yes_token_id:
            state.outcome = state.outcome or (
                state.outcomes[0] if state.outcomes else "Up"
            )
        elif token_id == state.no_token_id:
            state.outcome = state.outcome or (
                state.outcomes[1] if len(state.outcomes) > 1 else "Down"
            )
        state.held_token_id = token_id

        if state.outcome == "Up":
            self._portfolio.portfolio_up_cost += cost
        else:
            self._portfolio.portfolio_down_cost += cost

        if prev_shares <= 0 and state.filled_shares > 0:
            state.bought = True
            self._portfolio.markets_traded += 1

        logger.info(
            "[CHEAP_SIDE] Fill %s: %.2f @ %.4f ($%.2f), avail=$%.2f",
            slug,
            fill_size,
            fill_price,
            cost,
            self._portfolio.available_capital,
        )

    def _ensure_slug_state(self, slug: str, ctx: StrategyContext) -> SlugState | None:
        if slug in self._slugs:
            return self._slugs[slug]

        if not is_btc_5min_slug(slug):
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

        state = SlugState(
            yes_token_id=yes_id,
            no_token_id=no_id,
            outcomes=out_tuple,
        )
        self._slugs[slug] = state
        self._token_to_slug[yes_id] = slug
        self._token_to_slug[no_id] = slug
        self._hot_tokens.add(yes_id)
        self._hot_tokens.add(no_id)
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
    def _ask_liquidity_at_price(event: BookUpdate, limit_price: float) -> float:
        total = 0.0
        for ask_price, ask_size in event.asks:
            if ask_price <= limit_price + 1e-9:
                total += ask_size
        if total <= 0 and event.best_ask > 0 and event.best_ask <= limit_price + 1e-9:
            return 100.0
        return max(total, 0.0)


register_strategy(
    "cheap_side",
    StrategySpec(
        factory=lambda hot_tokens, **_: [CheapSideStrategy(hot_tokens=hot_tokens)],
        needs_full_book_updates=True,
    ),
)
