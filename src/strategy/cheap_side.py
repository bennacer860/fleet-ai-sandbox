"""Pure logic for portfolio-hedged cheap-side buying on binary Up/Down markets."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Literal

Outcome = Literal["Up", "Down"]


@dataclass(frozen=True, slots=True)
class CheapSideConfig:
    max_entry_price: float = 0.50
    min_entry_price: float = 0.01
    max_tte_s: float = 300.0
    min_tte_s: float = 15.0
    max_balance_ratio: float = 1.3
    initial_capital: float = 1000.0
    min_order_notional_usd: float = 1.0
    min_shares: float = 5.0
    max_per_market_usd: float = 50.0
    fee_bps: int = 0


@dataclass(frozen=True, slots=True)
class CheapSidePick:
    outcome: Outcome
    price: float
    token_index: int  # 0 = first outcome in market_meta, 1 = second


def pick_cheap_side(yes_ask: float, no_ask: float) -> CheapSidePick | None:
    """Return the cheaper outcome if its ask is below fair value."""
    if yes_ask <= 0 or no_ask <= 0:
        return None

    if yes_ask <= no_ask:
        cheap_price, outcome, idx = yes_ask, "Up", 0
    else:
        cheap_price, outcome, idx = no_ask, "Down", 1

    if cheap_price >= 0.50:
        return None

    return CheapSidePick(outcome=outcome, price=cheap_price, token_index=idx)


def balance_ok(
    outcome: Outcome,
    portfolio_up_cost: float,
    portfolio_down_cost: float,
    max_ratio: float,
    additional_cost: float,
) -> bool:
    """True if adding cost on ``outcome`` keeps Up/Down within max_ratio."""
    if max_ratio <= 1.0:
        return True

    up = portfolio_up_cost + (additional_cost if outcome == "Up" else 0.0)
    down = portfolio_down_cost + (additional_cost if outcome == "Down" else 0.0)
    if up <= 0 and down <= 0:
        return True
    if up <= 0 or down <= 0:
        return True

    ratio = up / down
    return (1.0 / max_ratio) <= ratio <= max_ratio


def should_buy_for_outcome(
    outcome: Outcome,
    price: float,
    tte_s: float,
    cfg: CheapSideConfig,
    *,
    already_bought: bool,
    portfolio_up_cost: float,
    portfolio_down_cost: float,
    available_capital: float,
    order_cost: float,
) -> tuple[bool, str]:
    """Gate whether to place a buy (with balance check on the chosen outcome)."""
    if already_bought:
        return False, "already bought this market"

    if tte_s < cfg.min_tte_s:
        return False, f"TTE {tte_s:.0f}s < min {cfg.min_tte_s:.0f}s"
    if tte_s > cfg.max_tte_s:
        return False, f"TTE {tte_s:.0f}s > max {cfg.max_tte_s:.0f}s"

    if price > cfg.max_entry_price:
        return False, f"price {price:.3f} > max {cfg.max_entry_price:.2f}"
    if price < cfg.min_entry_price:
        return False, f"price {price:.3f} < min {cfg.min_entry_price:.2f}"

    if order_cost > available_capital + 1e-9:
        return False, f"insufficient capital (need ${order_cost:.2f}, have ${available_capital:.2f})"

    if not balance_ok(
        outcome,
        portfolio_up_cost,
        portfolio_down_cost,
        cfg.max_balance_ratio,
        order_cost,
    ):
        return False, "portfolio balance limit"

    return True, "ok"


def get_order_size(
    price: float,
    cfg: CheapSideConfig,
    *,
    available_capital: float,
    market_spent_usd: float,
    ask_liquidity: float,
) -> float:
    """Shares to buy: min notional, per-market cap, capital, and book depth."""
    if price <= 0:
        return 0.0

    room_usd = max(0.0, cfg.max_per_market_usd - market_spent_usd)
    room_usd = min(room_usd, available_capital)
    if room_usd < cfg.min_order_notional_usd:
        return 0.0

    # Smallest valid size: min shares and min notional
    size = max(cfg.min_shares, cfg.min_order_notional_usd / price)
    max_by_capital = room_usd / price
    size = min(size, max_by_capital, ask_liquidity)
    if size < cfg.min_shares - 1e-9:
        return 0.0
    if size * price < cfg.min_order_notional_usd - 1e-9:
        return 0.0
    return size


def is_btc_5min_slug(slug: str) -> bool:
    """True for BTC 5-minute Up/Down markets."""
    s = slug.lower()
    return "btc" in s and "5min" in s
