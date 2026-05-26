"""Unit tests for cheap_side pure logic and adapter helpers."""

from src.strategy.cheap_side import (
    CheapSideConfig,
    balance_ok,
    get_order_size,
    is_btc_5min_slug,
    pick_cheap_side,
    should_buy_for_outcome,
)
from src.strategy.cheap_side_adapter import CheapSideStrategy


def test_pick_cheap_side_prefers_lower_ask() -> None:
    pick = pick_cheap_side(0.35, 0.12)
    assert pick is not None
    assert pick.outcome == "Down"
    assert pick.price == 0.12


def test_pick_cheap_side_rejects_above_fair() -> None:
    assert pick_cheap_side(0.55, 0.60) is None


def test_balance_ok_within_ratio() -> None:
    cfg = CheapSideConfig(max_balance_ratio=1.3)
    assert balance_ok("Up", 100, 100, cfg.max_balance_ratio, 10)
    assert not balance_ok("Up", 200, 50, cfg.max_balance_ratio, 50)


def test_should_buy_for_outcome_gates_tte_and_capital() -> None:
    cfg = CheapSideConfig(initial_capital=1000, min_tte_s=15, max_tte_s=300)
    ok, _ = should_buy_for_outcome(
        "Up",
        0.20,
        100,
        cfg,
        already_bought=False,
        portfolio_up_cost=0,
        portfolio_down_cost=0,
        available_capital=500,
        order_cost=20,
    )
    assert ok
    ok2, reason = should_buy_for_outcome(
        "Up",
        0.20,
        5,
        cfg,
        already_bought=False,
        portfolio_up_cost=0,
        portfolio_down_cost=0,
        available_capital=500,
        order_cost=20,
    )
    assert not ok2
    assert "TTE" in reason


def test_get_order_size_respects_liquidity_and_min_notional() -> None:
    cfg = CheapSideConfig(
        min_shares=5,
        min_order_notional_usd=1.0,
        max_per_market_usd=50,
    )
    size = get_order_size(
        0.20,
        cfg,
        available_capital=100,
        market_spent_usd=0,
        ask_liquidity=3,
    )
    assert size == 0.0

    size2 = get_order_size(
        0.20,
        cfg,
        available_capital=100,
        market_spent_usd=0,
        ask_liquidity=20,
    )
    assert size2 == 5.0


def test_is_btc_5min_slug() -> None:
    assert is_btc_5min_slug("btc-5min-up-or-down-2026-05-06-00:05")
    assert not is_btc_5min_slug("eth-15min-up-or-down-2026-05-06-00:05")


def test_dashboard_snapshot_initial() -> None:
    s = CheapSideStrategy()
    snap = s.dashboard_snapshot()
    assert snap["initial_capital"] == 1000
    assert snap["available_capital"] == 1000
    assert snap["markets_traded"] == 0


def test_strategy_registry_includes_cheap_side() -> None:
    from src.strategy.registry import available_strategy_names

    assert "cheap_side" in available_strategy_names()
