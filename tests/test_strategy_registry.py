from src.strategy.base import Strategy
from src.strategy.proximity import NoOpProximityCalculator
from src.strategy.registry import available_strategy_names, get_strategy_spec


def test_strategy_registry_contains_expected_strategies() -> None:
    assert set(available_strategy_names()) == {
        "sweep",
        "post_expiry",
        "aggressive_post_expiry",
        "gabagool",
        "gabagool_dual",
        "end_market",
        "post_expiry_temperature",
    }


def test_strategy_registry_factories_build_valid_strategies() -> None:
    for name in available_strategy_names():
        spec = get_strategy_spec(name)
        built = spec.factory(
            hot_tokens=set(),
            price_threshold=0.95,
            early_tick_threshold=0.995,
            proximity_calculator=NoOpProximityCalculator(),
        )
        assert built
        assert all(isinstance(strategy, Strategy) for strategy in built)
        assert all(strategy.name() == name for strategy in built)
