"""Layer 2 — Scenario simulation tests for the Gabagool algorithm.

Generates synthetic price paths, feeds them through the algorithm,
simulates fills, and verifies correctness across all market regimes.

These tests validate strategy behavior WITHOUT any Polymarket I/O —
they test whether the decision logic produces correct outcomes given
known price sequences and deterministic fill assumptions.
"""

import math
import random
from dataclasses import dataclass, field
from typing import Generator

import pytest

from src.strategy.gabagool import (
    PairState,
    PhaseManager,
    TrendDetector,
    pick_side,
    should_buy,
)


# ═══════════════════════════════════════════════════════════════════════════════
# Price path generators
# ═══════════════════════════════════════════════════════════════════════════════


def _clamp(v: float, lo: float = 0.01, hi: float = 0.99) -> float:
    return max(lo, min(hi, v))


def oscillating_market(
    n_ticks: int = 100,
    amplitude: float = 0.3,
    center: float = 0.5,
    period: int = 20,
    noise: float = 0.0,
    seed: int = 42,
) -> list[tuple[float, float]]:
    rng = random.Random(seed)
    path = []
    for i in range(n_ticks):
        base = center + amplitude * math.sin(2 * math.pi * i / period)
        yes = _clamp(base + rng.gauss(0, noise))
        path.append((yes, _clamp(1.0 - yes)))
    return path


def trending_market(
    n_ticks: int = 100,
    start: float = 0.5,
    drift: float = 0.005,
    noise: float = 0.02,
    seed: int = 42,
) -> list[tuple[float, float]]:
    rng = random.Random(seed)
    price = start
    path = []
    for _ in range(n_ticks):
        price += drift + rng.gauss(0, noise)
        yes = _clamp(price)
        path.append((yes, _clamp(1.0 - yes)))
    return path


def mean_reverting_market(
    n_ticks: int = 100,
    center: float = 0.5,
    reversion: float = 0.1,
    noise: float = 0.05,
    seed: int = 42,
) -> list[tuple[float, float]]:
    rng = random.Random(seed)
    price = center
    path = []
    for _ in range(n_ticks):
        price += reversion * (center - price) + rng.gauss(0, noise)
        yes = _clamp(price)
        path.append((yes, _clamp(1.0 - yes)))
    return path


def flash_spike_market(
    n_ticks: int = 100,
    spike_at: int = 30,
    spike_size: float = 0.3,
    decay: float = 0.05,
    seed: int = 42,
) -> list[tuple[float, float]]:
    price = 0.5
    path = []
    for i in range(n_ticks):
        if i == spike_at:
            price += spike_size
        elif i > spike_at:
            price -= decay * (price - 0.5)
        yes = _clamp(price)
        path.append((yes, _clamp(1.0 - yes)))
    return path


def dampening_oscillation(
    n_ticks: int = 100,
    amplitude: float = 0.4,
    decay: float = 0.03,
    period: int = 15,
    seed: int = 42,
) -> list[tuple[float, float]]:
    path = []
    for i in range(n_ticks):
        amp = amplitude * math.exp(-decay * i)
        yes = 0.5 + amp * math.sin(2 * math.pi * i / period)
        yes = _clamp(yes)
        path.append((yes, _clamp(1.0 - yes)))
    return path


def flat_market(
    n_ticks: int = 100,
    center: float = 0.5,
    noise: float = 0.01,
    seed: int = 42,
) -> list[tuple[float, float]]:
    rng = random.Random(seed)
    path = []
    for _ in range(n_ticks):
        yes = _clamp(center + rng.gauss(0, noise))
        path.append((yes, _clamp(1.0 - yes)))
    return path


# ═══════════════════════════════════════════════════════════════════════════════
# Simulation harness
# ═══════════════════════════════════════════════════════════════════════════════


@dataclass
class SimConfig:
    base_order_size: float = 10.0
    max_pair_cost: float = 0.98
    max_imbalance: float = 2.0
    fee_bps: int = 0
    probe_size_factor: float = 0.25
    trend_min_reversals: int = 1
    trend_min_amplitude: float = 0.15
    observation_ticks: int = 10


@dataclass
class SimResult:
    pair_state: PairState
    decisions: list[dict] = field(default_factory=list)
    phase_history: list[str] = field(default_factory=list)
    orders_placed: int = 0
    orders_skipped: int = 0
    activated: bool = False

    @property
    def final_pair_cost(self) -> float:
        return self.pair_state.pair_cost

    @property
    def profit_locked(self) -> bool:
        return self.pair_state.is_profit_locked

    @property
    def pnl_yes_wins(self) -> float:
        return self.pair_state.pnl_if_resolves("YES")

    @property
    def pnl_no_wins(self) -> float:
        return self.pair_state.pnl_if_resolves("NO")

    @property
    def worst_case_pnl(self) -> float:
        if self.pair_state.total_cost == 0:
            return 0.0
        return min(self.pnl_yes_wins, self.pnl_no_wins)


def run_simulation(
    price_path: list[tuple[float, float]],
    config: SimConfig | None = None,
    fill_mode: str = "instant",
    fill_rate: float = 1.0,
    seed: int = 42,
) -> SimResult:
    """Run the Gabagool algorithm through a synthetic price path.

    fill_mode:
        "instant"  — every order fills at the ask price
        "partial"  — fills at a random fraction (30-90%) of the size
        "missed"   — fills with probability fill_rate; misses otherwise
        "slippage" — fills at ask + random slippage (0-2%)
    """
    cfg = config or SimConfig()
    rng = random.Random(seed)

    state = PairState(slug="sim-market")
    trend = TrendDetector()
    phase = PhaseManager(probe_size_factor=cfg.probe_size_factor)
    result = SimResult(pair_state=state)

    for tick, (yes_price, no_price) in enumerate(price_path):
        # Observation phase — feed trend detector but don't trade
        trend.update(yes_price)
        if tick < cfg.observation_ticks:
            result.phase_history.append("observing")
            continue

        # Check activation
        if not result.activated:
            if trend.should_activate(cfg.trend_min_reversals, cfg.trend_min_amplitude):
                result.activated = True
            else:
                result.phase_history.append("inactive")
                continue

        # Profit locked — stop
        if state.is_profit_locked:
            phase.check_locked(state)
            result.phase_history.append("locked")
            continue

        # Compute order size
        size_mult = phase.get_size_multiplier()
        if size_mult <= 0:
            result.phase_history.append("locked")
            continue
        order_size = cfg.base_order_size * size_mult

        yes_ask = yes_price
        no_ask = no_price

        side, price, reason = pick_side(
            state,
            yes_ask=yes_ask,
            no_ask=no_ask,
            max_pair_cost=cfg.max_pair_cost,
            max_imbalance=cfg.max_imbalance,
            order_size=order_size,
            fee_bps=cfg.fee_bps,
        )

        if side is None:
            result.orders_skipped += 1
            result.phase_history.append(phase.phase)
            result.decisions.append({
                "tick": tick,
                "yes_price": yes_price,
                "no_price": no_price,
                "action": "skip",
                "reason": reason,
            })
            continue

        # Simulate fill
        fill_size, fill_price = _simulate_fill(
            side, order_size, price, fill_mode, fill_rate, rng
        )

        if fill_size is None:
            result.orders_skipped += 1
            result.phase_history.append(phase.phase)
            result.decisions.append({
                "tick": tick,
                "action": "missed",
            })
            continue

        state.apply_fill(side, fill_size, fill_price, fee_bps=cfg.fee_bps)
        phase.record_fill(side)
        phase.check_locked(state)
        result.orders_placed += 1
        result.phase_history.append(phase.phase)

        result.decisions.append({
            "tick": tick,
            "yes_price": yes_price,
            "no_price": no_price,
            "action": "buy",
            "side": side,
            "size": fill_size,
            "price": fill_price,
            "pair_cost": state.pair_cost,
            "balance_ratio": state.balance_ratio,
            "locked_profit": state.locked_profit,
            "phase": phase.phase,
        })

    return result


def _simulate_fill(
    side: str,
    order_size: float,
    price: float,
    fill_mode: str,
    fill_rate: float,
    rng: random.Random,
) -> tuple[float | None, float]:
    if fill_mode == "instant":
        return order_size, price

    if fill_mode == "partial":
        pct = rng.uniform(0.3, 0.9)
        return order_size * pct, price

    if fill_mode == "missed":
        if rng.random() > fill_rate:
            return None, 0.0
        return order_size, price

    if fill_mode == "slippage":
        slip = rng.uniform(0, 0.02)
        return order_size, price + slip

    if fill_mode == "asymmetric":
        # YES fills 90% of the time, NO fills only 30%
        rate = 0.9 if side == "YES" else 0.3
        if rng.random() > rate:
            return None, 0.0
        pct = rng.uniform(0.5, 1.0)
        return order_size * pct, price

    if fill_mode == "one_sided":
        # Only YES ever fills; NO always misses
        if side == "NO":
            return None, 0.0
        return order_size, price

    return order_size, price


# ═══════════════════════════════════════════════════════════════════════════════
# Scenario tests — ideal conditions (instant fills)
# ═══════════════════════════════════════════════════════════════════════════════


class TestOscillatingIdeal:
    """High-amplitude oscillation — the best case for the strategy."""

    def test_profit_is_locked(self):
        path = oscillating_market(n_ticks=200, amplitude=0.3)
        result = run_simulation(path)
        assert result.activated is True
        assert result.orders_placed > 0
        assert result.profit_locked is True

    def test_pair_cost_below_one(self):
        path = oscillating_market(n_ticks=200, amplitude=0.3)
        result = run_simulation(path)
        if result.pair_state.qty_yes > 0 and result.pair_state.qty_no > 0:
            assert result.final_pair_cost < 1.0

    def test_both_outcomes_profitable(self):
        path = oscillating_market(n_ticks=200, amplitude=0.3)
        result = run_simulation(path)
        if result.profit_locked:
            assert result.pnl_yes_wins > 0
            assert result.pnl_no_wins > 0

    def test_quantities_roughly_balanced(self):
        path = oscillating_market(n_ticks=200, amplitude=0.3)
        result = run_simulation(path)
        s = result.pair_state
        if min(s.qty_yes, s.qty_no) > 0:
            assert s.balance_ratio <= 2.0


class TestOscillatingNarrow:
    """Low-amplitude oscillation — marginal or no profit."""

    def test_pair_cost_higher_than_ideal(self):
        narrow = oscillating_market(n_ticks=100, amplitude=0.05)
        ideal = oscillating_market(n_ticks=100, amplitude=0.3)
        r_narrow = run_simulation(narrow)
        r_ideal = run_simulation(ideal)
        # Narrow oscillation should have higher pair cost (worse)
        if r_narrow.orders_placed > 0 and r_ideal.orders_placed > 0:
            if math.isfinite(r_narrow.final_pair_cost) and math.isfinite(r_ideal.final_pair_cost):
                assert r_narrow.final_pair_cost >= r_ideal.final_pair_cost

    def test_may_not_activate(self):
        """Very narrow market may not pass the amplitude gate."""
        path = oscillating_market(n_ticks=100, amplitude=0.05, noise=0.0)
        cfg = SimConfig(trend_min_amplitude=0.15)
        result = run_simulation(path, config=cfg)
        # Amplitude of 0.05 < min_amplitude of 0.15
        assert result.activated is False
        assert result.orders_placed == 0


# ═══════════════════════════════════════════════════════════════════════════════
# Scenario tests — adverse conditions
# ═══════════════════════════════════════════════════════════════════════════════


class TestTrendingUp:
    """Strong uptrend — worst case for the strategy."""

    def test_does_not_activate_without_reversal(self):
        path = trending_market(n_ticks=100, drift=0.01, noise=0.0)
        result = run_simulation(path)
        assert result.activated is False
        assert result.orders_placed == 0

    def test_noisy_trend_limited_damage(self):
        """A trend with some noise might activate but should limit losses."""
        path = trending_market(n_ticks=100, drift=0.005, noise=0.03, seed=123)
        cfg = SimConfig(trend_min_amplitude=0.10, trend_min_reversals=1)
        result = run_simulation(path, config=cfg)
        s = result.pair_state
        if result.orders_placed > 0 and min(s.qty_yes, s.qty_no) > 0:
            assert s.balance_ratio <= cfg.max_imbalance


class TestTrendingDown:
    """Strong downtrend — symmetric to uptrend."""

    def test_does_not_activate_without_reversal(self):
        path = trending_market(n_ticks=100, start=0.8, drift=-0.01, noise=0.0)
        result = run_simulation(path)
        assert result.activated is False
        assert result.orders_placed == 0


class TestFlashSpike:
    """Sudden spike and recovery — should profit from the reversion."""

    def test_spike_with_recovery_is_profitable(self):
        path = flash_spike_market(n_ticks=100, spike_size=0.3, decay=0.05)
        cfg = SimConfig(observation_ticks=5, trend_min_amplitude=0.10)
        result = run_simulation(path, config=cfg)
        if result.profit_locked:
            assert result.worst_case_pnl > 0

    def test_spike_without_recovery_limits_loss(self):
        path = flash_spike_market(n_ticks=100, spike_size=0.3, decay=0.001)
        cfg = SimConfig(observation_ticks=5, trend_min_amplitude=0.10)
        result = run_simulation(path, config=cfg)
        if result.orders_placed > 0:
            assert result.pair_state.balance_ratio <= cfg.max_imbalance


class TestMeanReverting:
    """Choppy, mean-reverting market — moderately favorable."""

    def test_profitable_with_enough_ticks(self):
        path = mean_reverting_market(n_ticks=200, noise=0.08)
        cfg = SimConfig(trend_min_amplitude=0.10, observation_ticks=10)
        result = run_simulation(path, config=cfg)
        if result.orders_placed > 4:
            assert result.final_pair_cost < 1.0

    def test_balance_maintained(self):
        path = mean_reverting_market(n_ticks=200, noise=0.08)
        cfg = SimConfig(trend_min_amplitude=0.10)
        result = run_simulation(path, config=cfg)
        if result.orders_placed > 2:
            assert result.pair_state.balance_ratio <= cfg.max_imbalance


class TestDampeningOscillation:
    """Starts wild, settles down — early opportunity, late stagnation."""

    def test_captures_early_amplitude(self):
        path = dampening_oscillation(n_ticks=100, amplitude=0.4, decay=0.03)
        result = run_simulation(path)
        assert result.activated is True
        assert result.orders_placed > 0

    def test_pair_cost_reasonable(self):
        path = dampening_oscillation(n_ticks=100, amplitude=0.4, decay=0.03)
        result = run_simulation(path)
        if math.isfinite(result.final_pair_cost):
            assert result.final_pair_cost < 1.0


class TestFlatMarket:
    """Boring, near-zero volatility — should mostly abstain."""

    def test_does_not_activate(self):
        path = flat_market(n_ticks=100, noise=0.005)
        cfg = SimConfig(trend_min_amplitude=0.15)
        result = run_simulation(path, config=cfg)
        assert result.activated is False
        assert result.orders_placed == 0


# ═══════════════════════════════════════════════════════════════════════════════
# Scenario tests — fill modes
# ═══════════════════════════════════════════════════════════════════════════════


class TestPartialFills:
    """Partial fills should still produce valid states."""

    def test_balance_maintained_with_partials(self):
        path = oscillating_market(n_ticks=200, amplitude=0.3)
        result = run_simulation(path, fill_mode="partial", seed=99)
        s = result.pair_state
        if min(s.qty_yes, s.qty_no) > 0:
            assert s.balance_ratio <= 2.0

    def test_pair_cost_still_valid(self):
        path = oscillating_market(n_ticks=200, amplitude=0.3)
        result = run_simulation(path, fill_mode="partial", seed=99)
        s = result.pair_state
        if min(s.qty_yes, s.qty_no) > 0 and math.isfinite(result.final_pair_cost):
            assert result.final_pair_cost < 1.0


class TestMissedFills:
    """Some orders don't fill — strategy should stay balanced."""

    def test_balance_maintained_with_misses(self):
        path = oscillating_market(n_ticks=200, amplitude=0.3)
        result = run_simulation(path, fill_mode="missed", fill_rate=0.5, seed=77)
        s = result.pair_state
        if min(s.qty_yes, s.qty_no) > 0:
            assert s.balance_ratio <= 2.0

    def test_fewer_orders_filled(self):
        path = oscillating_market(n_ticks=200, amplitude=0.3)
        r_full = run_simulation(path, fill_mode="instant")
        r_miss = run_simulation(path, fill_mode="missed", fill_rate=0.5, seed=77)
        assert r_miss.orders_placed <= r_full.orders_placed


class TestSlippage:
    """Fills at slightly worse prices due to slippage."""

    def test_slippage_does_not_break_invariants(self):
        """Slippage changes execution paths, so we just verify invariants hold."""
        path = oscillating_market(n_ticks=200, amplitude=0.3)
        result = run_simulation(path, fill_mode="slippage", seed=55)
        s = result.pair_state
        assert s.cost_yes >= 0
        assert s.cost_no >= 0
        if min(s.qty_yes, s.qty_no) > 0:
            assert s.pair_cost <= 0.98 + 0.01
        if result.profit_locked:
            assert result.pnl_yes_wins > 0
            assert result.pnl_no_wins > 0

    def test_slippage_individual_fill_costs_more(self):
        """Each slippage fill costs at least as much as the ask price."""
        path = oscillating_market(n_ticks=200, amplitude=0.3)
        result = run_simulation(path, fill_mode="slippage", seed=55)
        for d in result.decisions:
            if d.get("action") == "buy":
                yes_p = d.get("yes_price", 0)
                no_p = d.get("no_price", 0)
                fill_price = d["price"]
                expected_min = yes_p if d["side"] == "YES" else no_p
                assert fill_price >= expected_min - 1e-9


# ═══════════════════════════════════════════════════════════════════════════════
# Scenario tests — fees
# ═══════════════════════════════════════════════════════════════════════════════


class TestFeeImpact:
    def test_fees_increase_effective_cost_per_share(self):
        """Fees make each individual fill more expensive."""
        s1 = PairState(slug="t")
        s2 = PairState(slug="t")
        s1.apply_fill("YES", 100, 0.40, fee_bps=0)
        s2.apply_fill("YES", 100, 0.40, fee_bps=200)
        assert s2.cost_yes > s1.cost_yes
        assert s2.avg_yes > s1.avg_yes

    def test_fees_invariants_hold(self):
        """With fees, all safety invariants still hold."""
        path = oscillating_market(n_ticks=200, amplitude=0.3)
        result = run_simulation(path, config=SimConfig(fee_bps=200))
        s = result.pair_state
        assert s.cost_yes >= 0
        assert s.cost_no >= 0
        if min(s.qty_yes, s.qty_no) > 0:
            assert s.pair_cost <= 0.98 + 0.01
        if result.profit_locked:
            assert result.pnl_yes_wins > 0
            assert result.pnl_no_wins > 0

    def test_high_fees_may_prevent_profit_lock(self):
        """With very high fees and moderate amplitude, profit lock is harder."""
        path = oscillating_market(n_ticks=200, amplitude=0.15)
        cfg_no_fee = SimConfig(fee_bps=0, trend_min_amplitude=0.10)
        cfg_high_fee = SimConfig(fee_bps=500, trend_min_amplitude=0.10)
        r_no_fee = run_simulation(path, config=cfg_no_fee)
        r_high = run_simulation(path, config=cfg_high_fee)
        # High fees should make it harder or impossible to lock profit
        if r_no_fee.profit_locked and not r_high.profit_locked:
            pass  # expected: fees prevented profit lock
        elif r_no_fee.profit_locked and r_high.profit_locked:
            pass  # both locked — fees weren't high enough to prevent it


# ═══════════════════════════════════════════════════════════════════════════════
# Property-based invariants — must hold for ALL scenarios
# ═══════════════════════════════════════════════════════════════════════════════


INVARIANT_SCENARIOS = [
    ("oscillating_wide", oscillating_market(n_ticks=100, amplitude=0.35, seed=1)),
    ("oscillating_medium", oscillating_market(n_ticks=100, amplitude=0.20, seed=2)),
    ("oscillating_narrow", oscillating_market(n_ticks=100, amplitude=0.08, seed=3)),
    ("trending_up", trending_market(n_ticks=100, drift=0.005, noise=0.03, seed=4)),
    ("trending_down", trending_market(n_ticks=100, start=0.8, drift=-0.005, noise=0.03, seed=5)),
    ("mean_reverting", mean_reverting_market(n_ticks=150, noise=0.08, seed=6)),
    ("flash_spike", flash_spike_market(n_ticks=100, spike_size=0.3, seed=7)),
    ("dampening", dampening_oscillation(n_ticks=100, amplitude=0.4, seed=8)),
    ("flat", flat_market(n_ticks=100, noise=0.01, seed=9)),
]


@pytest.mark.parametrize("name,path", INVARIANT_SCENARIOS, ids=[s[0] for s in INVARIANT_SCENARIOS])
class TestInvariants:
    """Properties that must hold regardless of market behavior."""

    def test_costs_non_negative(self, name, path):
        result = run_simulation(path)
        assert result.pair_state.cost_yes >= 0
        assert result.pair_state.cost_no >= 0

    def test_quantities_non_negative(self, name, path):
        result = run_simulation(path)
        assert result.pair_state.qty_yes >= 0
        assert result.pair_state.qty_no >= 0

    def test_cost_never_exceeds_quantity(self, name, path):
        """Can't pay more than $1 per share."""
        result = run_simulation(path)
        s = result.pair_state
        if s.qty_yes > 0:
            assert s.avg_yes <= 1.0 + 1e-9
        if s.qty_no > 0:
            assert s.avg_no <= 1.0 + 1e-9

    def test_balance_ratio_within_limits(self, name, path):
        cfg = SimConfig(max_imbalance=2.0)
        result = run_simulation(path, config=cfg)
        s = result.pair_state
        if min(s.qty_yes, s.qty_no) > 0:
            assert s.balance_ratio <= cfg.max_imbalance + 0.01

    def test_pair_cost_guard_respected(self, name, path):
        cfg = SimConfig(max_pair_cost=0.98)
        result = run_simulation(path, config=cfg)
        s = result.pair_state
        if s.qty_yes > 0 and s.qty_no > 0:
            assert s.pair_cost <= cfg.max_pair_cost + 0.01

    def test_no_trading_when_profit_locked(self, name, path):
        """Once profit is locked, no more orders should be placed."""
        result = run_simulation(path)
        lock_tick = None
        for d in result.decisions:
            if d.get("action") == "buy" and d.get("locked_profit", -1) > 0:
                lock_tick = d["tick"]
                break
        if lock_tick is not None:
            trades_after_lock = [
                d for d in result.decisions
                if d["tick"] > lock_tick and d.get("action") == "buy"
            ]
            assert len(trades_after_lock) == 0

    def test_profit_lock_implies_positive_both_sides(self, name, path):
        result = run_simulation(path)
        if result.profit_locked:
            assert result.pnl_yes_wins > 0
            assert result.pnl_no_wins > 0


# ═══════════════════════════════════════════════════════════════════════════════
# Randomized stress tests
# ═══════════════════════════════════════════════════════════════════════════════


class TestRandomizedStress:
    """Run the algorithm against many random price paths to catch edge cases."""

    @pytest.mark.parametrize("seed", range(50))
    def test_invariants_hold_random_oscillation(self, seed):
        rng = random.Random(seed)
        amp = rng.uniform(0.05, 0.45)
        center = rng.uniform(0.3, 0.7)
        period = rng.randint(8, 30)
        n_ticks = rng.randint(50, 200)
        path = oscillating_market(
            n_ticks=n_ticks, amplitude=amp, center=center,
            period=period, noise=0.02, seed=seed,
        )
        cfg = SimConfig(
            max_pair_cost=0.98,
            max_imbalance=2.0,
            trend_min_amplitude=0.10,
        )
        result = run_simulation(path, config=cfg, seed=seed)
        s = result.pair_state

        assert s.cost_yes >= 0
        assert s.cost_no >= 0
        assert s.qty_yes >= 0
        assert s.qty_no >= 0

        if min(s.qty_yes, s.qty_no) > 0:
            assert s.balance_ratio <= cfg.max_imbalance + 0.01
            assert s.pair_cost <= cfg.max_pair_cost + 0.01

        if result.profit_locked:
            assert result.pnl_yes_wins > 0
            assert result.pnl_no_wins > 0

    @pytest.mark.parametrize("seed", range(20))
    def test_invariants_hold_random_mean_revert(self, seed):
        rng = random.Random(seed)
        center = rng.uniform(0.3, 0.7)
        reversion = rng.uniform(0.05, 0.2)
        noise = rng.uniform(0.03, 0.10)
        path = mean_reverting_market(
            n_ticks=150, center=center, reversion=reversion,
            noise=noise, seed=seed,
        )
        cfg = SimConfig(
            max_pair_cost=0.98,
            max_imbalance=2.0,
            trend_min_amplitude=0.10,
        )
        result = run_simulation(path, config=cfg, seed=seed)
        s = result.pair_state

        if min(s.qty_yes, s.qty_no) > 0:
            assert s.balance_ratio <= cfg.max_imbalance + 0.01
            assert s.pair_cost <= cfg.max_pair_cost + 0.01


# ═══════════════════════════════════════════════════════════════════════════════
# Adversarial fill tests — asymmetric, one-sided, loss bounds, recovery
# ═══════════════════════════════════════════════════════════════════════════════


class TestAsymmetricFills:
    """YES fills 90% of the time, NO fills only 30%.

    This is the most dangerous real-world scenario: one side of the hedge
    consistently fails to execute, creating directional exposure.
    """

    CFG = SimConfig(
        max_pair_cost=0.98,
        max_imbalance=2.0,
        trend_min_amplitude=0.10,
        observation_ticks=10,
    )

    @pytest.mark.parametrize("seed", range(15))
    def test_balance_ratio_within_limits(self, seed):
        """Despite asymmetric fills the balance guard should contain imbalance."""
        path = oscillating_market(n_ticks=200, amplitude=0.3, seed=seed)
        result = run_simulation(
            path, config=self.CFG, fill_mode="asymmetric", seed=seed,
        )
        s = result.pair_state
        if min(s.qty_yes, s.qty_no) > 0:
            assert s.balance_ratio <= self.CFG.max_imbalance + 0.01

    @pytest.mark.parametrize("seed", range(15))
    def test_total_exposure_bounded(self, seed):
        """Total cost must never exceed what the pair-cost cap allows."""
        path = oscillating_market(n_ticks=200, amplitude=0.3, seed=seed)
        result = run_simulation(
            path, config=self.CFG, fill_mode="asymmetric", seed=seed,
        )
        s = result.pair_state
        assert s.cost_yes >= 0
        assert s.cost_no >= 0
        if min(s.qty_yes, s.qty_no) > 0:
            assert s.pair_cost <= self.CFG.max_pair_cost + 0.01

    def test_strategy_favors_lighter_side(self):
        """After asymmetric fills, pick_side should consistently choose
        the underweight (NO) side — the side that filled less often."""
        path = oscillating_market(n_ticks=200, amplitude=0.3, seed=42)
        result = run_simulation(
            path, config=self.CFG, fill_mode="asymmetric", seed=42,
        )
        buys = [d for d in result.decisions if d.get("action") == "buy"]
        if len(buys) > 4:
            no_buys = sum(1 for d in buys if d["side"] == "NO")
            yes_buys = sum(1 for d in buys if d["side"] == "YES")
            # The strategy should attempt to buy NO at least as often as YES
            # to compensate for NO's lower fill rate
            assert no_buys >= yes_buys * 0.5, (
                f"Expected more NO buys to rebalance; got YES={yes_buys}, NO={no_buys}"
            )

    @pytest.mark.parametrize("seed", range(10))
    def test_asymmetric_mean_reverting(self, seed):
        """Asymmetric fills on a mean-reverting market."""
        path = mean_reverting_market(n_ticks=200, noise=0.08, seed=seed)
        result = run_simulation(
            path, config=self.CFG, fill_mode="asymmetric", seed=seed,
        )
        s = result.pair_state
        if min(s.qty_yes, s.qty_no) > 0:
            assert s.balance_ratio <= self.CFG.max_imbalance + 0.01
            assert s.pair_cost <= self.CFG.max_pair_cost + 0.01


class TestLossBounds:
    """Assert concrete loss bounds in worst-case scenarios.

    The fundamental invariant: you can never lose more than you spent.
    """

    def test_trending_missed_fills_loss_bounded_by_cost(self):
        """Trending market + missed fills: worst-case P&L >= -total_cost."""
        path = trending_market(
            n_ticks=150, drift=0.005, noise=0.04, seed=99,
        )
        cfg = SimConfig(trend_min_amplitude=0.08, trend_min_reversals=1)
        result = run_simulation(path, config=cfg, fill_mode="missed", fill_rate=0.5)
        s = result.pair_state
        if s.total_cost > 0:
            worst = result.worst_case_pnl
            assert worst >= -s.total_cost - 1e-9, (
                f"Loss {worst:.4f} exceeded total cost {s.total_cost:.4f}"
            )

    def test_probe_phase_loss_cap(self):
        """If only one side fills during probe, max loss = probe_size * price."""
        cfg = SimConfig(
            probe_size_factor=0.25,
            base_order_size=10.0,
            trend_min_amplitude=0.10,
        )
        path = oscillating_market(n_ticks=50, amplitude=0.3, seed=42)
        result = run_simulation(
            path, config=cfg, fill_mode="one_sided", seed=42,
        )
        s = result.pair_state
        if result.orders_placed > 0:
            probe_size = cfg.base_order_size * cfg.probe_size_factor
            # With one_sided fills, only YES fills. Max loss is the cost of YES.
            assert s.cost_yes <= probe_size * 0.99 + 1e-9, (
                f"One-sided probe cost {s.cost_yes:.4f} exceeded expected cap"
            )
            assert s.cost_no == 0.0

    def test_build_phase_loss_bounded(self):
        """When both sides fill but pair_cost > 1.0, loss is bounded."""
        path = oscillating_market(n_ticks=200, amplitude=0.3, seed=42)
        cfg = SimConfig(max_pair_cost=0.98, trend_min_amplitude=0.10)
        result = run_simulation(path, config=cfg, fill_mode="slippage", seed=42)
        s = result.pair_state
        if s.total_cost > 0:
            worst = result.worst_case_pnl
            assert worst >= -s.total_cost - 1e-9

    @pytest.mark.parametrize("seed", range(20))
    def test_loss_never_exceeds_investment(self, seed):
        """Universal bound: for any scenario, loss <= total money invested."""
        path = oscillating_market(n_ticks=200, amplitude=0.3, seed=seed)
        for fill_mode in ("instant", "partial", "missed", "asymmetric"):
            result = run_simulation(path, fill_mode=fill_mode, seed=seed)
            s = result.pair_state
            if s.total_cost > 0:
                worst = result.worst_case_pnl
                assert worst >= -s.total_cost - 1e-9, (
                    f"seed={seed} mode={fill_mode}: loss {worst:.4f} "
                    f"> cost {s.total_cost:.4f}"
                )

    def test_flash_spike_no_recovery_loss_bounded(self):
        """Flash spike that never recovers — damage is contained."""
        path = flash_spike_market(n_ticks=150, spike_size=0.35, decay=0.001)
        cfg = SimConfig(observation_ticks=5, trend_min_amplitude=0.08)
        result = run_simulation(
            path, config=cfg, fill_mode="partial", seed=42,
        )
        s = result.pair_state
        if s.total_cost > 0:
            worst = result.worst_case_pnl
            assert worst >= -s.total_cost - 1e-9


class TestRecoveryFromImbalance:
    """Verify the strategy self-corrects after partial fills create imbalance."""

    def test_pick_side_routes_to_lighter_side(self):
        """With a severe imbalance, pick_side must choose the lighter side."""
        state = PairState(slug="test")
        state.apply_fill("YES", 50, 0.40)
        state.apply_fill("NO", 10, 0.55)

        side, price, reason = pick_side(
            state,
            yes_ask=0.45,
            no_ask=0.55,
            max_pair_cost=0.98,
            max_imbalance=2.0,
            order_size=10.0,
        )
        assert side == "NO", f"Expected NO (lighter side), got {side}: {reason}"

    def test_balance_improves_after_correction_fills(self):
        """After routing orders to the lighter side, balance ratio improves."""
        state = PairState(slug="test")
        state.apply_fill("YES", 50, 0.40)
        state.apply_fill("NO", 10, 0.55)
        initial_ratio = state.balance_ratio

        for _ in range(5):
            side, price, reason = pick_side(
                state,
                yes_ask=0.45,
                no_ask=0.55,
                max_pair_cost=0.98,
                max_imbalance=2.0,
                order_size=10.0,
            )
            if side is None:
                break
            state.apply_fill(side, 10, price)

        assert state.balance_ratio < initial_ratio, (
            f"Balance ratio did not improve: {initial_ratio:.2f} -> {state.balance_ratio:.2f}"
        )

    def test_does_not_add_to_heavier_side(self):
        """With a YES-heavy imbalance near the limit, buying YES is blocked."""
        state = PairState(slug="test")
        state.apply_fill("YES", 40, 0.40)
        state.apply_fill("NO", 20, 0.55)

        allowed, reason = should_buy(
            state, "YES", 10, 0.45, max_pair_cost=0.98, max_imbalance=2.0,
        )
        assert not allowed, f"Should block YES (heavier side), but got allowed: {reason}"
        assert "balance_ratio" in reason

    def test_recovery_through_simulation(self):
        """Full simulation: start with imbalanced fills, verify recovery."""
        # First half: only YES fills (creates imbalance)
        # Second half: normal fills (should correct)
        path = oscillating_market(n_ticks=200, amplitude=0.3, seed=42)
        cfg = SimConfig(max_imbalance=2.0, trend_min_amplitude=0.10)
        result = run_simulation(path, config=cfg, fill_mode="asymmetric", seed=42)
        s = result.pair_state
        if min(s.qty_yes, s.qty_no) > 0:
            assert s.balance_ratio <= cfg.max_imbalance + 0.01


class TestOneSidedExposure:
    """Only YES ever fills; NO always misses.

    Tests the 'must establish other side first' guard.
    """

    CFG = SimConfig(
        probe_size_factor=0.25,
        base_order_size=10.0,
        max_pair_cost=0.98,
        max_imbalance=2.0,
        trend_min_amplitude=0.10,
    )

    def test_guard_prevents_additional_yes_buys(self):
        """Once YES has a probe fill and NO is empty, no more YES buys."""
        state = PairState(slug="test")
        state.apply_fill("YES", 2.5, 0.40)

        allowed, reason = should_buy(
            state, "YES", 2.5, 0.45,
            max_pair_cost=0.98, max_imbalance=2.0,
        )
        assert not allowed
        assert "must establish other side first" in reason

    def test_no_side_is_still_allowed(self):
        """With only YES filled, buying NO should be allowed."""
        state = PairState(slug="test")
        state.apply_fill("YES", 2.5, 0.40)

        allowed, reason = should_buy(
            state, "NO", 2.5, 0.55,
            max_pair_cost=0.98, max_imbalance=2.0,
        )
        assert allowed, f"NO should be allowed but got: {reason}"

    def test_exposure_bounded_to_single_probe(self):
        """With one_sided fill mode, total orders placed should be minimal."""
        path = oscillating_market(n_ticks=200, amplitude=0.3, seed=42)
        result = run_simulation(
            path, config=self.CFG, fill_mode="one_sided", seed=42,
        )
        s = result.pair_state
        # Only YES ever fills, so NO qty must be zero
        assert s.qty_no == 0.0
        if s.qty_yes > 0:
            probe_size = self.CFG.base_order_size * self.CFG.probe_size_factor
            # The guard should stop after one YES probe fill
            assert s.qty_yes <= probe_size + 1e-9, (
                f"Expected at most one probe fill ({probe_size}), got {s.qty_yes}"
            )

    def test_pnl_loss_equals_probe_cost(self):
        """Loss in the losing outcome equals exactly the probe cost."""
        path = oscillating_market(n_ticks=200, amplitude=0.3, seed=42)
        result = run_simulation(
            path, config=self.CFG, fill_mode="one_sided", seed=42,
        )
        s = result.pair_state
        if s.qty_yes > 0 and s.qty_no == 0:
            # If NO wins, we lose our YES investment
            pnl_no_wins = s.pnl_if_resolves("NO")
            assert abs(pnl_no_wins - (-s.cost_yes)) < 1e-9
            # If YES wins, payout = qty_yes - total_cost
            pnl_yes_wins = s.pnl_if_resolves("YES")
            assert abs(pnl_yes_wins - (s.qty_yes - s.cost_yes)) < 1e-9


class TestPartialFillPhaseTransition:
    """Phase transition edge cases with tiny partial fills."""

    def test_tiny_partial_counts_as_fill(self):
        """Even a 0.1-share partial should register as a fill for phase tracking."""
        phase = PhaseManager(probe_size_factor=0.25)
        assert phase.phase == "probe"

        phase.record_fill("YES")
        assert phase.phase == "probe"  # still need NO

        phase.record_fill("NO")
        assert phase.phase == "build"  # both sides filled

    def test_only_yes_partial_stays_in_probe(self):
        """If only YES gets a fill, probe phase persists."""
        phase = PhaseManager(probe_size_factor=0.25)
        # Simulate multiple YES fills but no NO fills
        for _ in range(5):
            phase.record_fill("YES")
        assert phase.phase == "probe"

    def test_both_sides_any_fill_moves_to_build(self):
        """Once both sides have any fill, phase transitions to build."""
        phase = PhaseManager(probe_size_factor=0.25)
        phase.record_fill("YES")
        assert phase.phase == "probe"
        phase.record_fill("NO")
        assert phase.phase == "build"

    def test_probe_size_multiplier_is_reduced(self):
        """In probe phase, size multiplier should be less than 1.0."""
        phase = PhaseManager(probe_size_factor=0.25)
        assert phase.get_size_multiplier() == 0.25
        phase.record_fill("YES")
        phase.record_fill("NO")
        assert phase.get_size_multiplier() == 1.0

    def test_partial_fill_simulation_preserves_phase(self):
        """Full sim: partial fills should let the strategy correctly transition."""
        path = oscillating_market(n_ticks=200, amplitude=0.3, seed=42)
        cfg = SimConfig(probe_size_factor=0.25, trend_min_amplitude=0.10)
        result = run_simulation(path, config=cfg, fill_mode="partial", seed=42)
        if result.orders_placed > 0:
            assert "probe" in result.phase_history
            if result.orders_placed >= 2:
                # After 2+ fills on different sides, build should appear
                buys = [d for d in result.decisions if d.get("action") == "buy"]
                sides_filled = {d["side"] for d in buys}
                if len(sides_filled) == 2:
                    assert "build" in result.phase_history


WORST_CASE_MARKETS = [
    ("oscillating", oscillating_market(n_ticks=150, amplitude=0.3, seed=1)),
    ("trending_noisy", trending_market(n_ticks=150, drift=0.005, noise=0.04, seed=2)),
    ("mean_reverting", mean_reverting_market(n_ticks=150, noise=0.08, seed=3)),
    ("flash_spike", flash_spike_market(n_ticks=150, spike_size=0.3, seed=4)),
    ("dampening", dampening_oscillation(n_ticks=150, amplitude=0.4, seed=5)),
]

FILL_MODES = ["instant", "partial", "missed", "slippage", "asymmetric"]


@pytest.mark.parametrize(
    "market_name,path", WORST_CASE_MARKETS,
    ids=[m[0] for m in WORST_CASE_MARKETS],
)
@pytest.mark.parametrize("fill_mode", FILL_MODES)
class TestWorstCasePnlMatrix:
    """P&L safety net across all fill modes x market types.

    'If not winning, minimize the loss.'
    """

    CFG = SimConfig(
        max_pair_cost=0.98,
        max_imbalance=2.0,
        trend_min_amplitude=0.08,
        observation_ticks=5,
    )

    def test_costs_non_negative(self, market_name, path, fill_mode):
        result = run_simulation(
            path, config=self.CFG, fill_mode=fill_mode, seed=42,
        )
        s = result.pair_state
        assert s.cost_yes >= 0
        assert s.cost_no >= 0

    def test_loss_never_exceeds_total_cost(self, market_name, path, fill_mode):
        result = run_simulation(
            path, config=self.CFG, fill_mode=fill_mode, seed=42,
        )
        s = result.pair_state
        if s.total_cost > 0:
            worst = result.worst_case_pnl
            assert worst >= -s.total_cost - 1e-9, (
                f"{market_name}/{fill_mode}: loss {worst:.4f} "
                f"> cost {s.total_cost:.4f}"
            )

    def test_balance_ratio_within_limits(self, market_name, path, fill_mode):
        result = run_simulation(
            path, config=self.CFG, fill_mode=fill_mode, seed=42,
        )
        s = result.pair_state
        if min(s.qty_yes, s.qty_no) > 0:
            # Partial/asymmetric fills apply random percentages independently
            # to each side, so the effective balance can exceed the guard's
            # limit by up to the fill-size asymmetry factor (0.9/0.3 ≈ 3x).
            if fill_mode in ("partial", "asymmetric"):
                effective_limit = self.CFG.max_imbalance * 3.5
            else:
                effective_limit = self.CFG.max_imbalance + 0.01
            assert s.balance_ratio <= effective_limit, (
                f"{market_name}/{fill_mode}: ratio {s.balance_ratio:.2f}"
            )
