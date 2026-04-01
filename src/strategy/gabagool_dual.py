"""Gabagool dual-sided strategy primitives.

This module contains pure, testable logic for the dual-sided variant:
- work both YES and NO outcomes in the same cycle,
- pause when combined ask becomes too expensive,
- throttle or block the heavier side when imbalance is too large.
"""

from __future__ import annotations

from dataclasses import dataclass

from .gabagool import PairState


@dataclass(frozen=True)
class DualOrderPlan:
    """Result of computing dual-sided order sizes for the next cycle."""

    yes_size: float
    no_size: float
    reason: str = "ok"


def pick_dual_sizes(
    state: PairState,
    base_order_size: float,
    max_imbalance: float,
    imbalance_throttle_start: float,
    imbalance_throttle_factor: float,
) -> DualOrderPlan:
    """Compute YES/NO order sizes for a dual-sided cycle.

    Rules:
    - start from the same `base_order_size` on both sides,
    - if one side is much heavier, reduce that side's size,
    - if imbalance breaches the hard cap, block the heavier side entirely.
    """
    if base_order_size <= 0:
        return DualOrderPlan(0.0, 0.0, "base order size <= 0")

    yes_size = base_order_size
    no_size = base_order_size

    ratio = state.balance_ratio
    heavier = state.heavier_side
    if heavier is None or ratio <= 1.0:
        return DualOrderPlan(yes_size, no_size, "balanced")

    if ratio >= max_imbalance:
        if heavier == "YES":
            yes_size = 0.0
        else:
            no_size = 0.0
        return DualOrderPlan(yes_size, no_size, "hard imbalance cap")

    if ratio > imbalance_throttle_start:
        factor = max(0.0, min(1.0, imbalance_throttle_factor))
        if heavier == "YES":
            yes_size *= factor
        else:
            no_size *= factor
        return DualOrderPlan(yes_size, no_size, "throttled heavier side")

    return DualOrderPlan(yes_size, no_size, "both sides")
