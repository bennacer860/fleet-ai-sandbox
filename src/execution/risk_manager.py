"""Risk manager — per-strategy limits, rate limiter, and circuit breaker.

Every ``OrderIntent`` passes through ``check()`` before being submitted.
If any limit is breached the order is blocked with a reason string.
"""

from __future__ import annotations

import time
from collections import deque
from dataclasses import dataclass, field

from ..core.models import OrderIntent
from ..logging_config import get_logger

logger = get_logger(__name__)


@dataclass(slots=True)
class RiskConfig:
    max_position_per_market: float = 50.0
    max_total_exposure: float = 500.0
    max_orders_per_minute: int = 30
    max_daily_loss: float = 10.0


class RiskManager:
    """Enforces trading limits before order submission."""

    def __init__(self, config: RiskConfig | None = None) -> None:
        self.config = config or RiskConfig()

        self._daily_loss: float = 0.0
        self._daily_reset_date: str = ""
        self._circuit_breaker_tripped = False

        self._order_timestamps: deque[float] = deque()

        self._exposure_by_market: dict[str, float] = {}
        self._total_exposure: float = 0.0

    # ── Public API ────────────────────────────────────────────────────────

    def check(self, intent: OrderIntent) -> tuple[bool, str]:
        """Return ``(True, "")`` if the order is allowed, else ``(False, reason)``."""
        self._maybe_reset_daily()

        if self._circuit_breaker_tripped:
            return False, "CIRCUIT_BREAKER: daily loss limit exceeded"

        order_value = intent.price * intent.size

        market_exp = self._exposure_by_market.get(intent.slug, 0.0)
        if market_exp + order_value > self.config.max_position_per_market:
            return False, (
                f"MAX_POSITION: {intent.slug} exposure "
                f"${market_exp + order_value:.2f} > ${self.config.max_position_per_market:.2f}"
            )

        if self._total_exposure + order_value > self.config.max_total_exposure:
            return False, (
                f"MAX_EXPOSURE: total ${self._total_exposure + order_value:.2f} "
                f"> ${self.config.max_total_exposure:.2f}"
            )

        if not self._check_rate_limit():
            return False, (
                f"RATE_LIMIT: >{self.config.max_orders_per_minute} orders/min"
            )

        return True, ""

    def record_order(self, intent: OrderIntent) -> None:
        """Called after a successful order submission to update state."""
        now = time.time()
        self._order_timestamps.append(now)

        order_value = intent.price * intent.size
        self._exposure_by_market[intent.slug] = (
            self._exposure_by_market.get(intent.slug, 0.0) + order_value
        )
        self._total_exposure += order_value

    def record_fill(self, slug: str, pnl: float) -> None:
        """Called on fill/resolution to update exposure and P&L."""
        self._daily_loss -= pnl  # positive pnl reduces loss
        if self._daily_loss > self.config.max_daily_loss:
            self._circuit_breaker_tripped = True
            logger.critical(
                "[RISK] CIRCUIT BREAKER: daily loss $%.2f > limit $%.2f — halting trading",
                self._daily_loss, self.config.max_daily_loss,
            )

    def release_exposure(self, slug: str, amount: float) -> None:
        """Release exposure when a position is closed."""
        current = self._exposure_by_market.get(slug, 0.0)
        released = min(amount, current)
        self._exposure_by_market[slug] = current - released
        self._total_exposure = max(0.0, self._total_exposure - released)

    # ── Read-only state for dashboard ─────────────────────────────────────

    @property
    def daily_loss(self) -> float:
        return self._daily_loss

    @property
    def circuit_breaker_active(self) -> bool:
        return self._circuit_breaker_tripped

    @property
    def total_exposure(self) -> float:
        return self._total_exposure

    @property
    def orders_last_minute(self) -> int:
        self._prune_old_timestamps()
        return len(self._order_timestamps)

    # ── Internal ──────────────────────────────────────────────────────────

    def _check_rate_limit(self) -> bool:
        self._prune_old_timestamps()
        return len(self._order_timestamps) < self.config.max_orders_per_minute

    def _prune_old_timestamps(self) -> None:
        cutoff = time.time() - 60.0
        while self._order_timestamps and self._order_timestamps[0] < cutoff:
            self._order_timestamps.popleft()

    def _maybe_reset_daily(self) -> None:
        today = time.strftime("%Y-%m-%d")
        if today != self._daily_reset_date:
            self._daily_reset_date = today
            self._daily_loss = 0.0
            self._circuit_breaker_tripped = False
