"""Layer 1 — Unit tests for the Gabagool algorithm.

Tests PairState math, signal logic (should_buy, pick_side),
TrendDetector, and PhaseManager in isolation with deterministic data.
"""

import math
import pytest

from src.strategy.gabagool import (
    PairState,
    PhaseManager,
    TrendDetector,
    pick_side,
    should_buy,
)


# ═══════════════════════════════════════════════════════════════════════════════
# PairState — construction and averages
# ═══════════════════════════════════════════════════════════════════════════════


class TestPairStateAverages:
    def test_empty_state(self):
        s = PairState(slug="test")
        assert s.avg_yes == 0.0
        assert s.avg_no == 0.0
        assert s.total_cost == 0.0

    def test_yes_only(self):
        s = PairState(slug="t", qty_yes=100, cost_yes=40)
        assert s.avg_yes == pytest.approx(0.40)
        assert s.avg_no == 0.0

    def test_no_only(self):
        s = PairState(slug="t", qty_no=200, cost_no=50)
        assert s.avg_no == pytest.approx(0.25)
        assert s.avg_yes == 0.0

    def test_both_sides(self):
        s = PairState(slug="t", qty_yes=100, cost_yes=45, qty_no=100, cost_no=30)
        assert s.avg_yes == pytest.approx(0.45)
        assert s.avg_no == pytest.approx(0.30)
        assert s.total_cost == pytest.approx(75.0)


# ═══════════════════════════════════════════════════════════════════════════════
# PairState — pair cost
# ═══════════════════════════════════════════════════════════════════════════════


class TestPairCost:
    def test_both_sides_present(self):
        s = PairState(slug="t", qty_yes=100, cost_yes=45, qty_no=100, cost_no=30)
        assert s.pair_cost == pytest.approx(0.75)

    def test_profitable_pair_cost(self):
        s = PairState(slug="t", qty_yes=200, cost_yes=55, qty_no=200, cost_no=45)
        assert s.pair_cost == pytest.approx(0.50)
        assert s.pair_cost < 1.0

    def test_unprofitable_pair_cost(self):
        s = PairState(slug="t", qty_yes=100, cost_yes=55, qty_no=100, cost_no=50)
        assert s.pair_cost == pytest.approx(1.05)
        assert s.pair_cost > 1.0

    def test_yes_empty_returns_inf(self):
        s = PairState(slug="t", qty_no=100, cost_no=30)
        assert s.pair_cost == float("inf")

    def test_no_empty_returns_inf(self):
        s = PairState(slug="t", qty_yes=100, cost_yes=40)
        assert s.pair_cost == float("inf")

    def test_both_empty_returns_inf(self):
        s = PairState(slug="t")
        assert s.pair_cost == float("inf")

    def test_article_example(self):
        """Reproduce the exact numbers from the Gabagool article."""
        s = PairState(
            slug="btc-15m",
            qty_yes=1266.72,
            cost_yes=655.18,
            qty_no=1294.98,
            cost_no=581.27,
        )
        assert s.avg_yes == pytest.approx(0.517, abs=0.001)
        assert s.avg_no == pytest.approx(0.449, abs=0.001)
        assert s.pair_cost == pytest.approx(0.966, abs=0.001)
        assert s.pair_cost < 1.0


# ═══════════════════════════════════════════════════════════════════════════════
# PairState — locked profit
# ═══════════════════════════════════════════════════════════════════════════════


class TestLockedProfit:
    def test_profit_locked(self):
        s = PairState(slug="t", qty_yes=100, cost_yes=30, qty_no=100, cost_no=30)
        assert s.locked_profit == pytest.approx(40.0)
        assert s.is_profit_locked is True

    def test_profit_not_locked(self):
        s = PairState(slug="t", qty_yes=100, cost_yes=55, qty_no=100, cost_no=50)
        assert s.locked_profit == pytest.approx(-5.0)
        assert s.is_profit_locked is False

    def test_profit_exactly_zero(self):
        s = PairState(slug="t", qty_yes=100, cost_yes=50, qty_no=100, cost_no=50)
        assert s.locked_profit == pytest.approx(0.0)
        assert s.is_profit_locked is False

    def test_profit_locked_unbalanced_quantities(self):
        """min(qty) determines payout — excess shares on one side don't help."""
        s = PairState(slug="t", qty_yes=50, cost_yes=15, qty_no=200, cost_no=30)
        assert s.locked_profit == pytest.approx(50 - 45)
        assert s.is_profit_locked is True

    def test_article_example(self):
        s = PairState(
            slug="btc-15m",
            qty_yes=1266.72,
            cost_yes=655.18,
            qty_no=1294.98,
            cost_no=581.27,
        )
        expected = min(1266.72, 1294.98) - (655.18 + 581.27)
        assert s.locked_profit == pytest.approx(expected, abs=0.01)
        assert s.is_profit_locked is True

    def test_one_side_empty(self):
        s = PairState(slug="t", qty_yes=100, cost_yes=40)
        assert s.locked_profit == pytest.approx(-40.0)
        assert s.is_profit_locked is False


# ═══════════════════════════════════════════════════════════════════════════════
# PairState — balance ratio
# ═══════════════════════════════════════════════════════════════════════════════


class TestBalanceRatio:
    def test_perfectly_balanced(self):
        s = PairState(slug="t", qty_yes=100, qty_no=100)
        assert s.balance_ratio == pytest.approx(1.0)

    def test_yes_heavy(self):
        s = PairState(slug="t", qty_yes=150, qty_no=100)
        assert s.balance_ratio == pytest.approx(1.5)

    def test_no_heavy(self):
        s = PairState(slug="t", qty_yes=80, qty_no=160)
        assert s.balance_ratio == pytest.approx(2.0)

    def test_one_side_empty_other_filled(self):
        s = PairState(slug="t", qty_yes=100, qty_no=0)
        assert s.balance_ratio == float("inf")

    def test_both_empty(self):
        s = PairState(slug="t")
        assert s.balance_ratio == 1.0

    def test_heavier_side_yes(self):
        s = PairState(slug="t", qty_yes=200, qty_no=100)
        assert s.heavier_side == "YES"

    def test_heavier_side_no(self):
        s = PairState(slug="t", qty_yes=100, qty_no=200)
        assert s.heavier_side == "NO"

    def test_heavier_side_balanced(self):
        s = PairState(slug="t", qty_yes=100, qty_no=100)
        assert s.heavier_side is None


# ═══════════════════════════════════════════════════════════════════════════════
# PairState — simulate_buy (immutable)
# ═══════════════════════════════════════════════════════════════════════════════


class TestSimulateBuy:
    def test_does_not_mutate_original(self):
        s = PairState(slug="t", qty_yes=100, cost_yes=40, qty_no=100, cost_no=30)
        new = s.simulate_buy("YES", 50, 0.35)
        assert s.qty_yes == 100
        assert s.cost_yes == 40.0
        assert new.qty_yes == 150

    def test_buy_yes(self):
        s = PairState(slug="t", qty_yes=100, cost_yes=40, qty_no=100, cost_no=30)
        new = s.simulate_buy("YES", 50, 0.30)
        assert new.qty_yes == pytest.approx(150)
        assert new.cost_yes == pytest.approx(55.0)
        assert new.qty_no == 100
        assert new.cost_no == 30.0

    def test_buy_no(self):
        s = PairState(slug="t", qty_yes=100, cost_yes=40, qty_no=100, cost_no=30)
        new = s.simulate_buy("NO", 80, 0.25)
        assert new.qty_no == pytest.approx(180)
        assert new.cost_no == pytest.approx(50.0)

    def test_buy_with_fees(self):
        s = PairState(slug="t")
        new = s.simulate_buy("YES", 100, 0.50, fee_bps=200)
        # effective price = 0.50 * 1.02 = 0.51
        assert new.cost_yes == pytest.approx(51.0)
        assert new.qty_yes == 100

    def test_first_buy_on_empty(self):
        s = PairState(slug="t")
        new = s.simulate_buy("NO", 100, 0.25)
        assert new.qty_no == 100
        assert new.cost_no == pytest.approx(25.0)
        assert new.pair_cost == float("inf")

    def test_pair_cost_changes_after_buy(self):
        s = PairState(slug="t", qty_yes=100, cost_yes=40, qty_no=100, cost_no=30)
        assert s.pair_cost == pytest.approx(0.70)
        new = s.simulate_buy("YES", 100, 0.60)
        assert new.pair_cost == pytest.approx(0.50 + 0.30)


# ═══════════════════════════════════════════════════════════════════════════════
# PairState — apply_fill (mutable)
# ═══════════════════════════════════════════════════════════════════════════════


class TestApplyFill:
    def test_apply_yes_fill(self):
        s = PairState(slug="t")
        s.apply_fill("YES", 100, 0.30)
        assert s.qty_yes == 100
        assert s.cost_yes == pytest.approx(30.0)

    def test_apply_no_fill(self):
        s = PairState(slug="t")
        s.apply_fill("NO", 50, 0.40)
        assert s.qty_no == 50
        assert s.cost_no == pytest.approx(20.0)

    def test_apply_with_fees(self):
        s = PairState(slug="t")
        s.apply_fill("YES", 100, 0.50, fee_bps=200)
        assert s.cost_yes == pytest.approx(51.0)

    def test_multiple_fills_update_average(self):
        s = PairState(slug="t")
        s.apply_fill("NO", 100, 0.25)
        s.apply_fill("NO", 100, 0.35)
        assert s.qty_no == 200
        assert s.cost_no == pytest.approx(60.0)
        assert s.avg_no == pytest.approx(0.30)

    def test_fill_sequence_matches_article(self):
        """Build up the article's position step by step."""
        s = PairState(slug="btc-15m")
        s.apply_fill("NO", 1294.98, 581.27 / 1294.98)
        s.apply_fill("YES", 1266.72, 655.18 / 1266.72)
        assert s.pair_cost == pytest.approx(0.966, abs=0.001)
        assert s.is_profit_locked is True


# ═══════════════════════════════════════════════════════════════════════════════
# PairState — pnl_if_resolves
# ═══════════════════════════════════════════════════════════════════════════════


class TestPnlIfResolves:
    def test_yes_wins_profitable(self):
        s = PairState(slug="t", qty_yes=100, cost_yes=30, qty_no=100, cost_no=30)
        assert s.pnl_if_resolves("YES") == pytest.approx(40.0)

    def test_no_wins_profitable(self):
        s = PairState(slug="t", qty_yes=100, cost_yes=30, qty_no=100, cost_no=30)
        assert s.pnl_if_resolves("NO") == pytest.approx(40.0)

    def test_yes_wins_unhedged_loss(self):
        s = PairState(slug="t", qty_yes=0, qty_no=100, cost_no=40)
        assert s.pnl_if_resolves("YES") == pytest.approx(-40.0)

    def test_no_wins_unhedged_loss(self):
        s = PairState(slug="t", qty_yes=100, cost_yes=50, qty_no=0)
        assert s.pnl_if_resolves("NO") == pytest.approx(-50.0)

    def test_balanced_with_locked_profit(self):
        s = PairState(slug="t", qty_yes=200, cost_yes=55, qty_no=200, cost_no=45)
        assert s.pnl_if_resolves("YES") == pytest.approx(100.0)
        assert s.pnl_if_resolves("NO") == pytest.approx(100.0)

    def test_unbalanced_different_outcomes(self):
        s = PairState(slug="t", qty_yes=100, cost_yes=30, qty_no=200, cost_no=50)
        pnl_yes = s.pnl_if_resolves("YES")
        pnl_no = s.pnl_if_resolves("NO")
        assert pnl_yes == pytest.approx(100 - 80)
        assert pnl_no == pytest.approx(200 - 80)
        assert pnl_yes < pnl_no


# ═══════════════════════════════════════════════════════════════════════════════
# should_buy — signal gating
# ═══════════════════════════════════════════════════════════════════════════════


class TestShouldBuy:
    def setup_method(self):
        # Costs are high enough that profit is NOT locked:
        # locked_profit = min(100,80) - (45+40) = 80 - 85 = -5
        self.state = PairState(
            slug="t", qty_yes=100, cost_yes=45, qty_no=80, cost_no=40
        )

    def test_allowed_when_within_limits(self):
        ok, _ = should_buy(self.state, "YES", 10, 0.30, max_pair_cost=0.98, max_imbalance=2.0)
        assert ok is True

    def test_blocked_by_pair_cost(self):
        ok, reason = should_buy(
            self.state, "YES", 50, 0.80, max_pair_cost=0.90, max_imbalance=5.0
        )
        assert ok is False
        assert "pair_cost" in reason

    def test_blocked_by_imbalance(self):
        # YES is already heavier (100 > 80), buying 200 more YES makes ratio huge
        ok, reason = should_buy(
            self.state, "YES", 200, 0.30, max_pair_cost=0.99, max_imbalance=1.5
        )
        assert ok is False
        assert "balance_ratio" in reason

    def test_blocked_when_profit_locked(self):
        locked = PairState(slug="t", qty_yes=100, cost_yes=20, qty_no=100, cost_no=20)
        assert locked.is_profit_locked is True
        ok, reason = should_buy(locked, "YES", 50, 0.30, max_pair_cost=0.98, max_imbalance=2.0)
        assert ok is False
        assert "profit already locked" in reason

    def test_first_buy_allowed_on_empty(self):
        empty = PairState(slug="t")
        ok, _ = should_buy(empty, "YES", 100, 0.30, max_pair_cost=0.98, max_imbalance=1.5)
        assert ok is True

    def test_second_side_first_buy_allowed(self):
        one_side = PairState(slug="t", qty_yes=100, cost_yes=40)
        ok, _ = should_buy(one_side, "NO", 100, 0.30, max_pair_cost=0.98, max_imbalance=1.5)
        assert ok is True

    def test_buying_lighter_side_always_allowed(self):
        """Buying the underweight side improves balance — never blocked by ratio."""
        s = PairState(slug="t", qty_yes=200, cost_yes=80, qty_no=50, cost_no=20)
        ok, _ = should_buy(s, "NO", 50, 0.30, max_pair_cost=0.99, max_imbalance=1.5)
        assert ok is True

    def test_pair_cost_with_fees(self):
        s = PairState(slug="t", qty_yes=100, cost_yes=48, qty_no=100, cost_no=48)
        sim_no_fee = s.simulate_buy("YES", 100, 0.50, fee_bps=0)
        sim_fee = s.simulate_buy("YES", 100, 0.50, fee_bps=200)
        assert sim_fee.pair_cost > sim_no_fee.pair_cost

    def test_buying_heavier_side_blocked_by_ratio(self):
        # YES is heavier (150 > 100). Costs set so profit is not locked:
        # locked_profit = min(150,100) - (80+55) = 100 - 135 = -35
        s = PairState(slug="t", qty_yes=150, cost_yes=80, qty_no=100, cost_no=55)
        ok, reason = should_buy(s, "YES", 100, 0.30, max_pair_cost=0.99, max_imbalance=1.5)
        assert ok is False
        assert "balance_ratio" in reason


# ═══════════════════════════════════════════════════════════════════════════════
# pick_side — side selection
# ═══════════════════════════════════════════════════════════════════════════════


class TestPickSide:
    def test_prefers_underweight_side(self):
        s = PairState(slug="t", qty_yes=100, cost_yes=40, qty_no=50, cost_no=15)
        side, price, reason = pick_side(
            s, yes_ask=0.40, no_ask=0.30,
            max_pair_cost=0.98, max_imbalance=3.0, order_size=10,
        )
        assert side == "NO"
        assert reason == "ok"

    def test_both_empty_picks_cheaper(self):
        s = PairState(slug="t")
        side, price, reason = pick_side(
            s, yes_ask=0.55, no_ask=0.35,
            max_pair_cost=0.98, max_imbalance=3.0, order_size=10,
        )
        assert side == "NO"
        assert price == pytest.approx(0.35)

    def test_both_empty_picks_cheaper_yes(self):
        s = PairState(slug="t")
        side, price, _ = pick_side(
            s, yes_ask=0.30, no_ask=0.60,
            max_pair_cost=0.98, max_imbalance=3.0, order_size=10,
        )
        assert side == "YES"
        assert price == pytest.approx(0.30)

    def test_one_side_blocked_picks_other(self):
        # Costs high enough that profit is not locked:
        # locked_profit = min(100,100) - (52+52) = 100 - 104 = -4
        s = PairState(slug="t", qty_yes=100, cost_yes=52, qty_no=100, cost_no=52)
        # YES at 0.50 would push pair_cost above 1.0, NO at 0.01 is cheap enough
        side, price, reason = pick_side(
            s, yes_ask=0.50, no_ask=0.01,
            max_pair_cost=1.0, max_imbalance=2.0, order_size=10,
        )
        assert side == "NO"

    def test_both_blocked_returns_none(self):
        s = PairState(slug="t", qty_yes=100, cost_yes=50, qty_no=100, cost_no=50)
        side, _, reason = pick_side(
            s, yes_ask=0.80, no_ask=0.80,
            max_pair_cost=0.90, max_imbalance=2.0, order_size=50,
        )
        assert side is None
        assert "no valid side" in reason

    def test_profit_locked_returns_none(self):
        s = PairState(slug="t", qty_yes=100, cost_yes=20, qty_no=100, cost_no=20)
        side, _, reason = pick_side(
            s, yes_ask=0.30, no_ask=0.30,
            max_pair_cost=0.98, max_imbalance=2.0, order_size=10,
        )
        assert side is None

    def test_with_fees(self):
        s = PairState(slug="t", qty_yes=100, cost_yes=48, qty_no=100, cost_no=48)
        side_no_fee, _, _ = pick_side(
            s, yes_ask=0.50, no_ask=0.50,
            max_pair_cost=0.99, max_imbalance=2.0, order_size=10, fee_bps=0,
        )
        side_fee, _, _ = pick_side(
            s, yes_ask=0.50, no_ask=0.50,
            max_pair_cost=0.99, max_imbalance=2.0, order_size=10, fee_bps=500,
        )
        # High fees may block both sides
        if side_no_fee is not None:
            pass  # at least without fees it should be ok
        # With 5% fee, effective price = 0.525, pair_cost ~= 0.48 + 0.525 > 0.99
        assert side_fee is None


# ═══════════════════════════════════════════════════════════════════════════════
# TrendDetector
# ═══════════════════════════════════════════════════════════════════════════════


class TestTrendDetector:
    def test_no_data(self):
        td = TrendDetector()
        assert td.reversals == 0
        assert td.has_reversed is False
        assert td.amplitude == 0.0
        assert td.should_activate() is False

    def test_steady_uptrend_no_reversal(self):
        td = TrendDetector()
        for p in [0.40, 0.45, 0.50, 0.55, 0.60, 0.65, 0.70]:
            td.update(p)
        assert td.reversals == 0
        assert td.has_reversed is False
        assert td.amplitude == pytest.approx(0.30)

    def test_steady_downtrend_no_reversal(self):
        td = TrendDetector()
        for p in [0.70, 0.65, 0.60, 0.55, 0.50]:
            td.update(p)
        assert td.reversals == 0
        assert td.has_reversed is False

    def test_single_reversal(self):
        td = TrendDetector()
        for p in [0.40, 0.50, 0.60, 0.50, 0.40]:
            td.update(p)
        assert td.reversals == 1
        assert td.has_reversed is True

    def test_multiple_reversals(self):
        td = TrendDetector()
        prices = [0.30, 0.50, 0.70, 0.50, 0.30, 0.50, 0.70]
        for p in prices:
            td.update(p)
        assert td.reversals >= 2

    def test_amplitude(self):
        td = TrendDetector()
        for p in [0.30, 0.70, 0.50]:
            td.update(p)
        assert td.amplitude == pytest.approx(0.40)

    def test_small_moves_below_threshold_not_counted(self):
        td = TrendDetector(min_move=0.01)
        for p in [0.500, 0.505, 0.500, 0.505]:
            td.update(p)
        assert td.reversals == 0

    def test_should_activate_requires_both(self):
        td = TrendDetector()
        # One reversal but low amplitude
        for p in [0.48, 0.52, 0.48]:
            td.update(p)
        assert td.has_reversed is True
        assert td.amplitude == pytest.approx(0.04)
        assert td.should_activate(min_reversals=1, min_amplitude=0.15) is False

    def test_should_activate_passes(self):
        td = TrendDetector()
        for p in [0.30, 0.60, 0.30, 0.60]:
            td.update(p)
        assert td.should_activate(min_reversals=1, min_amplitude=0.15) is True

    def test_reset(self):
        td = TrendDetector()
        for p in [0.30, 0.60, 0.30]:
            td.update(p)
        assert td.reversals >= 1
        td.reset()
        assert td.reversals == 0
        assert td.amplitude == 0.0


# ═══════════════════════════════════════════════════════════════════════════════
# PhaseManager
# ═══════════════════════════════════════════════════════════════════════════════


class TestPhaseManager:
    def test_starts_in_probe(self):
        pm = PhaseManager()
        assert pm.phase == "probe"
        assert pm.get_size_multiplier() == pytest.approx(0.25)

    def test_custom_probe_factor(self):
        pm = PhaseManager(probe_size_factor=0.10)
        assert pm.get_size_multiplier() == pytest.approx(0.10)

    def test_one_side_fill_stays_probe(self):
        pm = PhaseManager()
        pm.record_fill("YES")
        assert pm.phase == "probe"

    def test_both_sides_fill_transitions_to_build(self):
        pm = PhaseManager()
        pm.record_fill("YES")
        pm.record_fill("NO")
        assert pm.phase == "build"
        assert pm.get_size_multiplier() == pytest.approx(1.0)

    def test_reverse_order_also_transitions(self):
        pm = PhaseManager()
        pm.record_fill("NO")
        pm.record_fill("YES")
        assert pm.phase == "build"

    def test_duplicate_fills_dont_re_transition(self):
        pm = PhaseManager()
        pm.record_fill("YES")
        pm.record_fill("YES")
        assert pm.phase == "probe"

    def test_locked_phase(self):
        pm = PhaseManager()
        pm.record_fill("YES")
        pm.record_fill("NO")
        assert pm.phase == "build"

        locked_state = PairState(slug="t", qty_yes=100, cost_yes=20, qty_no=100, cost_no=20)
        pm.check_locked(locked_state)
        assert pm.phase == "locked"
        assert pm.get_size_multiplier() == pytest.approx(0.0)

    def test_not_locked_stays_build(self):
        pm = PhaseManager()
        pm.record_fill("YES")
        pm.record_fill("NO")

        not_locked = PairState(slug="t", qty_yes=100, cost_yes=55, qty_no=100, cost_no=50)
        pm.check_locked(not_locked)
        assert pm.phase == "build"

    def test_reset(self):
        pm = PhaseManager()
        pm.record_fill("YES")
        pm.record_fill("NO")
        assert pm.phase == "build"
        pm.reset()
        assert pm.phase == "probe"
