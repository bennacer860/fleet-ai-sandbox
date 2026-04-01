"""Scenario simulation tests for the gabagool_dual strategy model."""

from __future__ import annotations

import math
import random
from dataclasses import dataclass

import pytest

from src.strategy.gabagool import PairState, TrendDetector
from src.strategy.gabagool_dual import pick_dual_sizes


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
    _ = seed
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
    _ = seed
    path = []
    for i in range(n_ticks):
        amp = amplitude * math.exp(-decay * i)
        yes = _clamp(0.5 + amp * math.sin(2 * math.pi * i / period))
        path.append((yes, _clamp(1.0 - yes)))
    return path


@dataclass
class DualConfig:
    base_order_size: float = 10.0
    max_pair_cost: float = 0.98
    cooldown_pair_cost: float = 0.995
    resume_pair_cost: float = 0.985
    max_imbalance: float = 3.0
    imbalance_throttle_start: float = 1.5
    imbalance_throttle_factor: float = 0.35
    fee_bps: int = 0
    observation_ticks: int = 5
    trend_min_reversals: int = 0
    trend_min_amplitude: float = 0.03
    max_notional_per_slug: float = 250.0


@dataclass
class DualResult:
    pair_state: PairState
    orders_placed: int = 0

    @property
    def pnl_yes_wins(self) -> float:
        return self.pair_state.pnl_if_resolves("YES")

    @property
    def pnl_no_wins(self) -> float:
        return self.pair_state.pnl_if_resolves("NO")

    @property
    def worst_case_pnl(self) -> float:
        return min(self.pnl_yes_wins, self.pnl_no_wins)


def _simulate_fill(
    side: str,
    order_size: float,
    price: float,
    fill_mode: str,
    rng: random.Random,
) -> tuple[float | None, float]:
    if fill_mode == "instant":
        return order_size, price
    if fill_mode == "partial":
        return order_size * rng.uniform(0.3, 0.9), price
    if fill_mode == "missed":
        if rng.random() > 0.6:
            return None, 0.0
        return order_size, price
    if fill_mode == "slippage":
        return order_size, price + rng.uniform(0, 0.02)
    if fill_mode == "asymmetric":
        rate = 0.9 if side == "YES" else 0.3
        if rng.random() > rate:
            return None, 0.0
        return order_size * rng.uniform(0.5, 1.0), price
    if fill_mode == "one_sided":
        if side == "NO":
            return None, 0.0
        return order_size, price
    return order_size, price


def run_dual_simulation(
    price_path: list[tuple[float, float]],
    cfg: DualConfig | None = None,
    fill_mode: str = "instant",
    seed: int = 42,
) -> DualResult:
    cfg = cfg or DualConfig()
    rng = random.Random(seed)
    state = PairState(slug="dual-sim")
    trend = TrendDetector()
    in_cooldown = False
    tick_count = 0

    for yes_ask, no_ask in price_path:
        tick_count += 1
        trend.update(yes_ask)
        if tick_count <= cfg.observation_ticks:
            continue
        if not trend.should_activate(cfg.trend_min_reversals, cfg.trend_min_amplitude):
            continue

        combined = yes_ask + no_ask
        if in_cooldown:
            if combined <= cfg.resume_pair_cost:
                in_cooldown = False
            else:
                continue
        elif combined >= cfg.cooldown_pair_cost:
            in_cooldown = True
            continue

        if combined > cfg.max_pair_cost:
            continue

        if state.total_cost >= cfg.max_notional_per_slug:
            continue

        plan = pick_dual_sizes(
            state=state,
            base_order_size=cfg.base_order_size,
            max_imbalance=cfg.max_imbalance,
            imbalance_throttle_start=cfg.imbalance_throttle_start,
            imbalance_throttle_factor=cfg.imbalance_throttle_factor,
        )

        for side, price, size in (("YES", yes_ask, plan.yes_size), ("NO", no_ask, plan.no_size)):
            if size <= 0:
                continue
            if state.total_cost + (size * price) > cfg.max_notional_per_slug:
                continue
            fill_size, fill_price = _simulate_fill(side, size, price, fill_mode, rng)
            if fill_size is None:
                continue
            state.apply_fill(side, fill_size, fill_price, fee_bps=cfg.fee_bps)

    return DualResult(pair_state=state)


class TestDualSidedImbalance:
    @pytest.mark.parametrize("seed", range(10))
    def test_asymmetric_fills_keep_ratio_bounded(self, seed: int) -> None:
        path = oscillating_market(n_ticks=220, amplitude=0.3, seed=seed)
        cfg = DualConfig(max_imbalance=3.0)
        result = run_dual_simulation(path, cfg=cfg, fill_mode="asymmetric", seed=seed)
        s = result.pair_state
        if min(s.qty_yes, s.qty_no) > 0:
            assert s.balance_ratio <= cfg.max_imbalance * 1.25


class TestDualSidedCooldown:
    def test_cooldown_pauses_and_resumes(self) -> None:
        path = [(0.55, 0.45)] * 20 + [(0.54, 0.43)] * 20
        cfg = DualConfig(
            observation_ticks=0,
            trend_min_reversals=0,
            trend_min_amplitude=0.0,
            cooldown_pair_cost=0.995,
            resume_pair_cost=0.985,
            max_pair_cost=0.99,
        )
        result = run_dual_simulation(path, cfg=cfg, fill_mode="instant")
        assert result.pair_state.total_cost > 0


class TestDualSidedOrphanLeg:
    def test_one_sided_fills_create_bounded_exposure(self) -> None:
        path = oscillating_market(n_ticks=200, amplitude=0.3, seed=7)
        cfg = DualConfig(max_notional_per_slug=25.0)
        result = run_dual_simulation(path, cfg=cfg, fill_mode="one_sided", seed=7)
        s = result.pair_state
        assert s.qty_no == 0.0
        assert s.total_cost <= cfg.max_notional_per_slug + 1e-6


class TestDualSidedFeeErosion:
    def test_fees_reduce_net_outcome(self) -> None:
        path = oscillating_market(n_ticks=220, amplitude=0.3, seed=9)
        no_fee = run_dual_simulation(path, cfg=DualConfig(fee_bps=0), fill_mode="instant", seed=9)
        with_fee = run_dual_simulation(path, cfg=DualConfig(fee_bps=200), fill_mode="instant", seed=9)
        assert with_fee.pair_state.total_cost >= no_fee.pair_state.total_cost
        assert with_fee.worst_case_pnl <= no_fee.worst_case_pnl + 1e-9


class TestDualSidedAdversePath:
    def test_flash_spike_loss_bound(self) -> None:
        path = flash_spike_market(n_ticks=200, spike_size=0.35, decay=0.001, seed=12)
        result = run_dual_simulation(path, cfg=DualConfig(), fill_mode="partial", seed=12)
        s = result.pair_state
        if s.total_cost > 0:
            assert result.worst_case_pnl >= -s.total_cost - 1e-9


WORST_CASE_MARKETS = [
    ("oscillating", oscillating_market(n_ticks=160, amplitude=0.30, seed=1)),
    ("trending", trending_market(n_ticks=160, drift=0.005, noise=0.04, seed=2)),
    ("mean_reverting", mean_reverting_market(n_ticks=180, noise=0.08, seed=3)),
    ("flash_spike", flash_spike_market(n_ticks=160, spike_size=0.3, seed=4)),
    ("dampening", dampening_oscillation(n_ticks=160, amplitude=0.4, seed=5)),
]
FILL_MODES = ["instant", "partial", "missed", "slippage", "asymmetric"]


@pytest.mark.parametrize("market_name,path", WORST_CASE_MARKETS, ids=[m[0] for m in WORST_CASE_MARKETS])
@pytest.mark.parametrize("fill_mode", FILL_MODES)
class TestDualWorstCasePnlMatrix:
    def test_loss_never_exceeds_total_cost(self, market_name: str, path: list[tuple[float, float]], fill_mode: str) -> None:
        result = run_dual_simulation(path, cfg=DualConfig(), fill_mode=fill_mode, seed=42)
        s = result.pair_state
        if s.total_cost > 0:
            assert result.worst_case_pnl >= -s.total_cost - 1e-9
