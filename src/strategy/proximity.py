"""Pluggable proximity calculation for trading strategies.

Each strategy can receive its own ``ProximityCalculator`` to control how
spot-vs-strike distance is computed and whether trades should be blocked.
"""

from __future__ import annotations

import time
from abc import ABC, abstractmethod
from dataclasses import dataclass
from typing import Any, Protocol

from ..logging_config import get_logger
from ..markets.fifteen_min import extract_market_from_slug

logger = get_logger(__name__)


def _fetch_strike_price(slug: str) -> float | None:
    """Lazy import to avoid pulling in clob_client at module level."""
    from ..utils.market_data import fetch_strike_price
    return fetch_strike_price(slug)


@dataclass(frozen=True, slots=True)
class ProximityResult:
    """Outcome of a proximity check — passed to the strategy for logging."""

    proximity: float | None
    spot: float | None
    strike: float | None
    price_age_ms: float | None
    blocked: bool
    reason: str | None = None


class ProximityCalculator(ABC):
    """Interface for proximity calculation.

    Strategies call ``check()`` with the slug, eval data, and context.
    The calculator returns a ``ProximityResult`` that tells the strategy
    whether the trade should be blocked and why.
    """

    @abstractmethod
    def check(
        self,
        slug: str,
        eval_data: dict[str, Any],
        crypto_prices: dict[str, float],
        crypto_price_ts: dict[str, float],
    ) -> ProximityResult:
        """Evaluate proximity and return whether to block.

        Parameters
        ----------
        slug:
            Market slug (used to derive asset and strike).
        eval_data:
            Cached evaluation data (may contain ``price_to_beat``).
        crypto_prices:
            Current spot prices keyed by asset ticker.
        crypto_price_ts:
            Monotonic timestamps of when each spot price was received.
        """
        ...

    @property
    @abstractmethod
    def enabled(self) -> bool:
        """Whether this calculator is active (False = never blocks)."""
        ...

    @property
    @abstractmethod
    def min_distance(self) -> float:
        """Minimum distance threshold for display/logging."""
        ...


class NoOpProximityCalculator(ProximityCalculator):
    """Never blocks — used when proximity filtering is disabled."""

    def check(
        self,
        slug: str,
        eval_data: dict[str, Any],
        crypto_prices: dict[str, float],
        crypto_price_ts: dict[str, float],
    ) -> ProximityResult:
        asset = extract_market_from_slug(slug)
        spot = crypto_prices.get(asset)
        strike = eval_data.get("price_to_beat")
        proximity = (
            abs(spot - strike) / strike
            if spot is not None and strike is not None and strike > 0
            else None
        )
        ts = crypto_price_ts.get(asset) if asset else None
        age = (time.monotonic() - ts) * 1000 if ts is not None else None
        return ProximityResult(
            proximity=proximity, spot=spot, strike=strike,
            price_age_ms=age, blocked=False,
        )

    @property
    def enabled(self) -> bool:
        return False

    @property
    def min_distance(self) -> float:
        return 0.0


class SimpleProximityCalculator(ProximityCalculator):
    """Standard ``|spot - strike| / strike`` proximity with configurable threshold.

    This is the original proximity logic extracted from SweepStrategy and
    PostExpirySweepStrategy, now parameterised per-strategy.
    """

    def __init__(
        self,
        min_distance: float = 0.001,
        stale_threshold_ms: float = 10_000,
        block_on_missing_strike: bool = False,
        block_on_missing_spot: bool = False,
    ) -> None:
        self._min_distance = min_distance
        self._stale_threshold_ms = stale_threshold_ms
        self._block_on_missing_strike = block_on_missing_strike
        self._block_on_missing_spot = block_on_missing_spot

    @property
    def enabled(self) -> bool:
        return True

    @property
    def min_distance(self) -> float:
        return self._min_distance

    def check(
        self,
        slug: str,
        eval_data: dict[str, Any],
        crypto_prices: dict[str, float],
        crypto_price_ts: dict[str, float],
    ) -> ProximityResult:
        asset = extract_market_from_slug(slug)

        strike = eval_data.get("price_to_beat")
        if strike is None:
            strike = _fetch_strike_price(slug)
            if strike is not None:
                eval_data["price_to_beat"] = strike
                logger.info("Lazy strike fetch succeeded for %s: $%.6f", slug, strike)
            elif self._block_on_missing_strike:
                return ProximityResult(
                    proximity=None, spot=None, strike=None,
                    price_age_ms=None, blocked=True,
                    reason="proximity guard: strike unavailable",
                )
            else:
                logger.warning(
                    "Strike price unavailable for %s — proximity filter skipped", slug,
                )

        spot = crypto_prices.get(asset) if asset else None
        ts = crypto_price_ts.get(asset) if asset else None
        price_age_ms: float | None = None
        stale = False

        if ts is not None:
            price_age_ms = (time.monotonic() - ts) * 1000
            stale = price_age_ms > self._stale_threshold_ms

        if spot is None and self._block_on_missing_spot:
            return ProximityResult(
                proximity=None, spot=None, strike=strike,
                price_age_ms=price_age_ms, blocked=True,
                reason="proximity guard: spot unavailable",
            )

        if stale:
            return ProximityResult(
                proximity=None, spot=spot, strike=strike,
                price_age_ms=price_age_ms, blocked=True,
                reason=f"spot price stale ({price_age_ms:.0f}ms old > {self._stale_threshold_ms:.0f}ms)",
            )

        if strike is not None and strike <= 0:
            return ProximityResult(
                proximity=None, spot=spot, strike=strike,
                price_age_ms=price_age_ms, blocked=True,
                reason="proximity guard: invalid strike",
            )

        proximity: float | None = None
        if spot is not None and strike is not None and strike > 0:
            proximity = abs(spot - strike) / strike

        if proximity is not None and proximity < self._min_distance:
            return ProximityResult(
                proximity=proximity, spot=spot, strike=strike,
                price_age_ms=price_age_ms, blocked=True,
                reason=f"proximity {proximity:.4%} < {self._min_distance:.4%}",
            )

        return ProximityResult(
            proximity=proximity, spot=spot, strike=strike,
            price_age_ms=price_age_ms, blocked=False,
        )
