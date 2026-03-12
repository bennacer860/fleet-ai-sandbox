"""Gabagool strategy — market-neutral binary arbitrage algorithm.

Buys YES when cheap, NO when cheap, targeting:
    avg_YES + avg_NO < 1.00
to lock in risk-free profit regardless of market outcome.

This module contains only pure logic — no I/O, no side effects.
It is fully testable with synthetic data.
"""

from __future__ import annotations

import math
from collections import deque
from dataclasses import dataclass


# ── Pair State ──────────────────────────────────────────────────────────────────


@dataclass
class PairState:
    """Tracks accumulated positions on both sides of a binary market."""

    slug: str
    qty_yes: float = 0.0
    qty_no: float = 0.0
    cost_yes: float = 0.0
    cost_no: float = 0.0

    @property
    def avg_yes(self) -> float:
        return self.cost_yes / self.qty_yes if self.qty_yes > 0 else 0.0

    @property
    def avg_no(self) -> float:
        return self.cost_no / self.qty_no if self.qty_no > 0 else 0.0

    @property
    def pair_cost(self) -> float:
        if self.qty_yes <= 0 or self.qty_no <= 0:
            return float("inf")
        return self.avg_yes + self.avg_no

    @property
    def locked_profit(self) -> float:
        return min(self.qty_yes, self.qty_no) - (self.cost_yes + self.cost_no)

    @property
    def is_profit_locked(self) -> bool:
        return self.locked_profit > 0

    @property
    def balance_ratio(self) -> float:
        lo = min(self.qty_yes, self.qty_no)
        hi = max(self.qty_yes, self.qty_no)
        if lo <= 0:
            return float("inf") if hi > 0 else 1.0
        return hi / lo

    @property
    def heavier_side(self) -> str | None:
        if self.qty_yes > self.qty_no:
            return "YES"
        elif self.qty_no > self.qty_yes:
            return "NO"
        return None

    @property
    def total_cost(self) -> float:
        return self.cost_yes + self.cost_no

    def simulate_buy(
        self, side: str, qty: float, price: float, fee_bps: int = 0
    ) -> PairState:
        """Return a new PairState reflecting a hypothetical buy.

        Does NOT mutate self.
        """
        s = PairState(
            slug=self.slug,
            qty_yes=self.qty_yes,
            qty_no=self.qty_no,
            cost_yes=self.cost_yes,
            cost_no=self.cost_no,
        )
        effective_price = price * (1 + fee_bps / 10_000)
        if side == "YES":
            s.qty_yes += qty
            s.cost_yes += qty * effective_price
        else:
            s.qty_no += qty
            s.cost_no += qty * effective_price
        return s

    def apply_fill(
        self, side: str, qty: float, price: float, fee_bps: int = 0
    ) -> None:
        """Mutate state to reflect an actual fill."""
        effective_price = price * (1 + fee_bps / 10_000)
        if side == "YES":
            self.qty_yes += qty
            self.cost_yes += qty * effective_price
        else:
            self.qty_no += qty
            self.cost_no += qty * effective_price

    def pnl_if_resolves(self, winner: str) -> float:
        """Calculate P&L if the given side wins."""
        payout = self.qty_yes if winner == "YES" else self.qty_no
        return payout - self.total_cost


# ── Signal Logic ────────────────────────────────────────────────────────────────


def should_buy(
    state: PairState,
    side: str,
    qty: float,
    price: float,
    max_pair_cost: float,
    max_imbalance: float,
    fee_bps: int = 0,
) -> tuple[bool, str]:
    """Check if a proposed buy is acceptable.

    Returns (allowed, reason_string).
    """
    if state.is_profit_locked:
        return False, "profit already locked"

    simulated = state.simulate_buy(side, qty, price, fee_bps)

    # One-sided exposure guard: when one side has shares and the other
    # is empty, only allow buying the EMPTY side.  This prevents
    # unbounded accumulation on one side before the hedge is established.
    one_side_filled = (state.qty_yes > 0) != (state.qty_no > 0)
    if one_side_filled:
        adding_to_filled = (
            (side == "YES" and state.qty_yes > 0)
            or (side == "NO" and state.qty_no > 0)
        )
        if adding_to_filled:
            return False, "must establish other side first"

    # Pair cost guard — only meaningful when both sides have shares
    if simulated.qty_yes > 0 and simulated.qty_no > 0:
        if simulated.pair_cost > max_pair_cost:
            return (
                False,
                f"pair_cost {simulated.pair_cost:.4f} > {max_pair_cost}",
            )

    # Balance guard — only block when buying the HEAVIER side would
    # worsen the imbalance beyond the threshold.  Buying the lighter
    # (or empty) side always improves balance and is never blocked here.
    if min(simulated.qty_yes, simulated.qty_no) > 0:
        buying_heavier = (
            (side == "YES" and state.qty_yes >= state.qty_no)
            or (side == "NO" and state.qty_no >= state.qty_yes)
        )
        if buying_heavier and simulated.balance_ratio > max_imbalance:
            return (
                False,
                f"balance_ratio {simulated.balance_ratio:.2f} > {max_imbalance}",
            )

    return True, ""


def pick_side(
    state: PairState,
    yes_ask: float,
    no_ask: float,
    max_pair_cost: float,
    max_imbalance: float,
    order_size: float,
    fee_bps: int = 0,
) -> tuple[str | None, float, str]:
    """Decide which side to buy, if any.

    Prefers the underweight side to maintain balance.
    Returns (side, price, reason).  side is None if no trade.
    """
    candidates: list[tuple[str, float, float]] = []

    for side, ask in [("YES", yes_ask), ("NO", no_ask)]:
        allowed, reason = should_buy(
            state, side, order_size, ask, max_pair_cost, max_imbalance, fee_bps
        )
        if allowed:
            qty_this = state.qty_yes if side == "YES" else state.qty_no
            qty_other = state.qty_no if side == "YES" else state.qty_yes
            deficit = qty_other - qty_this
            candidates.append((side, ask, deficit))

    if not candidates:
        return None, 0.0, "no valid side"

    # Prefer largest deficit (most underweight side), break ties by cheaper
    candidates.sort(key=lambda c: (-c[2], c[1]))
    side, price, _ = candidates[0]
    return side, price, "ok"


# ── Trend Detection ────────────────────────────────────────────────────────────


class TrendDetector:
    """Tracks price history to detect oscillation vs directional trend."""

    def __init__(self, lookback: int = 20, min_move: float = 0.001) -> None:
        self._prices: deque[float] = deque(maxlen=lookback)
        self._reversal_count: int = 0
        self._last_direction: int = 0
        self._min_move = min_move

    def update(self, yes_price: float) -> None:
        self._prices.append(yes_price)
        if len(self._prices) >= 2:
            diff = self._prices[-1] - self._prices[-2]
            direction = (
                1 if diff > self._min_move else (-1 if diff < -self._min_move else 0)
            )
            if (
                direction != 0
                and self._last_direction != 0
                and direction != self._last_direction
            ):
                self._reversal_count += 1
            if direction != 0:
                self._last_direction = direction

    @property
    def reversals(self) -> int:
        return self._reversal_count

    @property
    def has_reversed(self) -> bool:
        return self._reversal_count >= 1

    @property
    def amplitude(self) -> float:
        if len(self._prices) < 3:
            return 0.0
        return max(self._prices) - min(self._prices)

    def should_activate(
        self, min_reversals: int = 1, min_amplitude: float = 0.15
    ) -> bool:
        """Should the strategy start trading in this market?"""
        return self.reversals >= min_reversals and self.amplitude >= min_amplitude

    def reset(self) -> None:
        self._prices.clear()
        self._reversal_count = 0
        self._last_direction = 0


# ── Phase Logic ─────────────────────────────────────────────────────────────────


class PhaseManager:
    """Manages the probe -> build -> locked lifecycle."""

    PROBE = "probe"
    BUILD = "build"
    LOCKED = "locked"

    def __init__(self, probe_size_factor: float = 0.25) -> None:
        self._phase = self.PROBE
        self._probe_size_factor = probe_size_factor
        self._has_yes_fill = False
        self._has_no_fill = False

    @property
    def phase(self) -> str:
        return self._phase

    def record_fill(self, side: str) -> None:
        if side == "YES":
            self._has_yes_fill = True
        else:
            self._has_no_fill = True

        if (
            self._has_yes_fill
            and self._has_no_fill
            and self._phase == self.PROBE
        ):
            self._phase = self.BUILD

    def check_locked(self, state: PairState) -> None:
        if state.is_profit_locked:
            self._phase = self.LOCKED

    def get_size_multiplier(self) -> float:
        if self._phase == self.PROBE:
            return self._probe_size_factor
        elif self._phase == self.BUILD:
            return 1.0
        return 0.0

    def reset(self) -> None:
        self._phase = self.PROBE
        self._has_yes_fill = False
        self._has_no_fill = False
