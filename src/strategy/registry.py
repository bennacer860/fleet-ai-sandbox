"""Strategy registry for bot wiring and CLI discovery."""

from __future__ import annotations

from dataclasses import dataclass
from importlib import import_module
from typing import TYPE_CHECKING, Callable

if TYPE_CHECKING:
    from .base import Strategy
    from .proximity import ProximityCalculator

StrategyFactory = Callable[..., list["Strategy"]]


@dataclass(frozen=True, slots=True)
class StrategySpec:
    """Describes how to build and wire a strategy."""

    factory: StrategyFactory
    needs_full_book_updates: bool = False
    uses_proximity: bool = False


STRATEGY_REGISTRY: dict[str, StrategySpec] = {}


def register_strategy(name: str, spec: StrategySpec) -> None:
    """Register a strategy spec by name."""
    if name in STRATEGY_REGISTRY:
        raise ValueError(f"Strategy already registered: {name}")
    STRATEGY_REGISTRY[name] = spec


def ensure_builtin_strategies_registered() -> None:
    """Import built-in strategy modules so they self-register."""
    modules = (
        "src.strategy.sweep",
        "src.strategy.post_expiry",
        "src.strategy.aggressive_post_expiry",
        "src.strategy.gabagool_adapter",
        "src.strategy.gabagool_dual_adapter",
        "src.strategy.end_market",
        "src.strategy.cheap_side_adapter",
    )
    for module_name in modules:
        import_module(module_name)


def get_strategy_spec(name: str) -> StrategySpec:
    """Look up a strategy spec by name."""
    ensure_builtin_strategies_registered()
    try:
        return STRATEGY_REGISTRY[name]
    except KeyError as exc:
        available = ", ".join(available_strategy_names())
        raise ValueError(f"Unknown strategy '{name}'. Available: {available}") from exc


def available_strategy_names() -> list[str]:
    """Return registered strategy names in registration order."""
    ensure_builtin_strategies_registered()
    return list(STRATEGY_REGISTRY.keys())
