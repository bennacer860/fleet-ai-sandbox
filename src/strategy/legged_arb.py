"""Pure decision logic for 3-phase legged arbitrage on binary Up/Down markets."""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import Literal

from ..markets.fifteen_min import detect_duration_from_slug, extract_market_from_slug

Outcome = Literal["Up", "Down"]


class ArbState(str, Enum):
    IDLE = "idle"
    PHASE1_PENDING = "phase1_pending"
    PHASE1_FILLED = "phase1_filled"
    PHASE2_PENDING = "phase2_pending"
    PHASE2_SOLD = "phase2_sold"
    PHASE3_PENDING = "phase3_pending"
    PHASE3_FILLED = "phase3_filled"
    DONE = "done"


@dataclass(frozen=True, slots=True)
class LeggedArbConfig:
    """Tunable parameters for legged arb (defaults from research + dry-run plan)."""

    markets: tuple[str, ...] = ("BTC",)
    durations: tuple[int, ...] = (15,)
    phase1_price_min: float = 0.70
    phase1_price_max: float = 0.95
    phase1_tte_min_s: float = 300.0
    phase1_tte_max_s: float = 840.0
    max_spread: float = 0.02
    min_ask_depth: float = 1000.0
    max_concurrent: int = 10
    clip_size: float = 150.0
    phase2_uplift: float = 0.14
    phase2_abs_bid: float = 0.90
    phase2_min_tte_s: float = 60.0
    phase3_max_price: float = 0.05
    phase3_max_tte_s: float = 180.0
    phase3_min_tte_s: float = 30.0
    min_order_notional_usd: float = 1.0
    min_shares: float = 5.0
    keep_shares_after_sell: float = 1.0
    stop_loss_drop: float = 0.10
    stop_loss_min_tte_s: float = 120.0


@dataclass(frozen=True, slots=True)
class BookSide:
    best_bid: float
    best_ask: float
    best_ask_size: float
    ask_depth: float
    bid_depth: float

    @property
    def mid(self) -> float:
        if self.best_bid > 0 and self.best_ask > 0:
            return (self.best_bid + self.best_ask) / 2.0
        if self.best_ask > 0:
            return self.best_ask
        if self.best_bid > 0:
            return self.best_bid
        return 0.0

    @property
    def spread(self) -> float:
        if self.best_bid > 0 and self.best_ask > 0:
            return max(self.best_ask - self.best_bid, 0.0)
        return float("inf")

    @property
    def imbalance(self) -> float:
        if self.ask_depth <= 0:
            return 0.0
        return self.bid_depth / self.ask_depth


@dataclass(frozen=True, slots=True)
class MarketBook:
    up: BookSide
    down: BookSide

    @property
    def favorite(self) -> Outcome:
        if self.up.mid >= self.down.mid:
            return "Up"
        return "Down"

    def side(self, outcome: Outcome) -> BookSide:
        return self.up if outcome == "Up" else self.down

    @property
    def favorite_side(self) -> BookSide:
        return self.side(self.favorite)

    @property
    def opposite(self) -> Outcome:
        return "Down" if self.favorite == "Up" else "Up"


@dataclass(frozen=True, slots=True)
class Phase1Decision:
    enter: bool
    side: Outcome | None = None
    price: float = 0.0
    size: float = 0.0
    reason: str = ""


@dataclass(frozen=True, slots=True)
class Phase2Decision:
    sell: bool
    side: Outcome | None = None
    price: float = 0.0
    size: float = 0.0
    reason: str = ""


@dataclass(frozen=True, slots=True)
class Phase3Decision:
    buy: bool
    side: Outcome | None = None
    price: float = 0.0
    size: float = 0.0
    reason: str = ""


@dataclass
class MarketArbState:
    slug: str
    yes_token_id: str
    no_token_id: str
    outcomes: tuple[str, str] = ("Up", "Down")
    phase: ArbState = ArbState.IDLE
    phase1_side: Outcome | None = None
    phase1_entry_price: float = 0.0
    phase1_target_size: float = 0.0
    phase1_filled_size: float = 0.0
    phase1_filled_cost: float = 0.0
    phase2_target_size: float = 0.0
    phase2_sold_size: float = 0.0
    phase3_target_size: float = 0.0
    phase3_filled_size: float = 0.0
    phase3_filled_cost: float = 0.0
    last_skip_reason: str = ""
    exit_is_stop_loss: bool = False
    stats: dict[str, int] = field(default_factory=dict)


def is_eligible_slug(slug: str, cfg: LeggedArbConfig) -> bool:
    """True when slug matches configured markets and durations."""
    market = extract_market_from_slug(slug)
    if market not in cfg.markets:
        return False
    duration = detect_duration_from_slug(slug)
    if duration is None or duration not in cfg.durations:
        return False
    return True


def count_active_arbs(states: dict[str, MarketArbState]) -> int:
    """Count markets with an open arb workflow (excluding idle/done)."""
    active_phases = {
        ArbState.PHASE1_PENDING,
        ArbState.PHASE1_FILLED,
        ArbState.PHASE2_PENDING,
        ArbState.PHASE2_SOLD,
        ArbState.PHASE3_PENDING,
        ArbState.PHASE3_FILLED,
    }
    return sum(1 for s in states.values() if s.phase in active_phases)


def _size_for_min_notional(size: float, price: float, cfg: LeggedArbConfig) -> float:
    if size <= 0 or price <= 0:
        return 0.0
    required = max(cfg.min_shares, cfg.min_order_notional_usd / price)
    return max(size, required)


def should_enter_phase1(
    book: MarketBook,
    tte_s: float,
    cfg: LeggedArbConfig,
    *,
    active_arb_count: int,
) -> Phase1Decision:
    """Decide whether to enter Phase 1 on the favorite side."""
    if active_arb_count >= cfg.max_concurrent:
        return Phase1Decision(False, reason="max concurrent arbs")

    if tte_s < cfg.phase1_tte_min_s:
        return Phase1Decision(False, reason="tte too low")
    if tte_s > cfg.phase1_tte_max_s:
        return Phase1Decision(False, reason="tte too high")

    fav = book.favorite
    fav_book = book.favorite_side
    fav_ask = fav_book.best_ask
    if fav_ask <= 0:
        return Phase1Decision(False, reason="missing favorite ask")

    if fav_ask < cfg.phase1_price_min:
        return Phase1Decision(False, reason="favorite too cheap")
    if fav_ask > cfg.phase1_price_max:
        return Phase1Decision(False, reason="favorite too expensive")

    if fav_book.spread > cfg.max_spread + 1e-9:
        return Phase1Decision(False, reason="spread too wide")

    if fav_book.ask_depth < cfg.min_ask_depth:
        return Phase1Decision(False, reason="insufficient ask depth")

    size = _size_for_min_notional(cfg.clip_size, fav_ask, cfg)
    size = min(size, fav_book.best_ask_size if fav_book.best_ask_size > 0 else size)
    size = min(size, fav_book.ask_depth)
    if size <= 0:
        return Phase1Decision(False, reason="size zero")

    if size * fav_ask < cfg.min_order_notional_usd - 1e-9:
        return Phase1Decision(False, reason="below min notional")

    return Phase1Decision(
        enter=True,
        side=fav,
        price=fav_ask,
        size=size,
        reason="phase1 entry",
    )


def should_sell_phase2(
    state: MarketArbState,
    book: MarketBook,
    tte_s: float,
    cfg: LeggedArbConfig,
) -> Phase2Decision:
    """Decide whether to sell the Phase-1 leg."""
    if state.phase != ArbState.PHASE1_FILLED:
        return Phase2Decision(False, reason="not in phase1 filled")
    if state.phase1_side is None or state.phase1_filled_size <= 0:
        return Phase2Decision(False, reason="no phase1 position")

    if tte_s < cfg.phase2_min_tte_s:
        return Phase2Decision(False, reason="phase2 tte cutoff")

    side_book = book.side(state.phase1_side)
    bid = side_book.best_bid
    if bid <= 0:
        return Phase2Decision(False, reason="missing bid")

    uplift = bid - state.phase1_entry_price
    triggered = uplift >= cfg.phase2_uplift - 1e-9 or bid >= cfg.phase2_abs_bid - 1e-9
    if not triggered:
        return Phase2Decision(False, reason="sell trigger not met")

    sell_size = max(state.phase1_filled_size - cfg.keep_shares_after_sell, 0.0)
    if sell_size <= 0:
        return Phase2Decision(False, reason="nothing to sell")

    return Phase2Decision(
        sell=True,
        side=state.phase1_side,
        price=bid,
        size=sell_size,
        reason=f"phase2 sell uplift={uplift:.3f}",
    )


def should_stop_loss(
    state: MarketArbState,
    book: MarketBook,
    tte_s: float,
    cfg: LeggedArbConfig,
) -> Phase2Decision:
    """Exit Phase-1 early when bid drops too far below entry (cap hold-to-expiry loss)."""
    if state.phase != ArbState.PHASE1_FILLED:
        return Phase2Decision(False, reason="not in phase1 filled")
    if state.phase1_side is None or state.phase1_filled_size <= 0:
        return Phase2Decision(False, reason="no phase1 position")
    if cfg.stop_loss_drop <= 0:
        return Phase2Decision(False, reason="stop loss disabled")

    if tte_s < cfg.stop_loss_min_tte_s:
        return Phase2Decision(False, reason="stop loss tte cutoff")

    side_book = book.side(state.phase1_side)
    bid = side_book.best_bid
    if bid <= 0:
        return Phase2Decision(False, reason="missing bid")

    drop = state.phase1_entry_price - bid
    if drop < cfg.stop_loss_drop - 1e-9:
        return Phase2Decision(False, reason="stop loss not triggered")

    sell_size = state.phase1_filled_size
    if sell_size <= 0:
        return Phase2Decision(False, reason="nothing to sell")

    return Phase2Decision(
        sell=True,
        side=state.phase1_side,
        price=bid,
        size=sell_size,
        reason=f"stop loss drop={drop:.3f}",
    )


def should_buy_phase3(
    state: MarketArbState,
    book: MarketBook,
    tte_s: float,
    cfg: LeggedArbConfig,
) -> Phase3Decision:
    """Decide whether to buy the cheap opposite leg."""
    if state.phase != ArbState.PHASE2_SOLD:
        return Phase3Decision(False, reason="phase2 not complete")
    if state.phase1_side is None:
        return Phase3Decision(False, reason="missing phase1 side")

    if tte_s > cfg.phase3_max_tte_s:
        return Phase3Decision(False, reason="phase3 tte too high")
    if tte_s < cfg.phase3_min_tte_s:
        return Phase3Decision(False, reason="phase3 tte too low")

    opp: Outcome = "Down" if state.phase1_side == "Up" else "Up"
    opp_book = book.side(opp)
    ask = opp_book.best_ask
    if ask <= 0:
        return Phase3Decision(False, reason="missing opposite ask")
    if ask > cfg.phase3_max_price + 1e-9:
        return Phase3Decision(False, reason="opposite too expensive")

    target = state.phase3_target_size or max(state.phase1_filled_size, cfg.clip_size)
    size = _size_for_min_notional(target, ask, cfg)
    size = min(size, opp_book.best_ask_size if opp_book.best_ask_size > 0 else size)
    size = min(size, opp_book.ask_depth if opp_book.ask_depth > 0 else size)
    if size <= 0:
        return Phase3Decision(False, reason="size zero")

    if size * ask < cfg.min_order_notional_usd - 1e-9:
        return Phase3Decision(False, reason="below min notional")

    return Phase3Decision(
        buy=True,
        side=opp,
        price=ask,
        size=size,
        reason="phase3 cheap leg",
    )


def build_market_book(
    *,
    up_bid: float,
    up_ask: float,
    up_ask_size: float,
    up_bid_depth: float,
    up_ask_depth: float,
    down_bid: float,
    down_ask: float,
    down_ask_size: float,
    down_bid_depth: float,
    down_ask_depth: float,
) -> MarketBook:
    """Construct a ``MarketBook`` from top-of-book metrics."""
    return MarketBook(
        up=BookSide(
            best_bid=up_bid,
            best_ask=up_ask,
            best_ask_size=up_ask_size,
            ask_depth=up_ask_depth,
            bid_depth=up_bid_depth,
        ),
        down=BookSide(
            best_bid=down_bid,
            best_ask=down_ask,
            best_ask_size=down_ask_size,
            ask_depth=down_ask_depth,
            bid_depth=down_bid_depth,
        ),
    )
