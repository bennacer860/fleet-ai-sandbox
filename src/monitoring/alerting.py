"""Log-based alerting framework with extensible webhook support.

Checks metric thresholds on a periodic loop and emits log lines at
WARNING or CRITICAL level.  Can optionally POST to a webhook URL
(Telegram, Discord, Slack) for push notifications.
"""

from __future__ import annotations

import asyncio
import time
from dataclasses import dataclass
from enum import Enum
from typing import Any, Callable

from ..logging_config import get_logger
from .metrics import Metrics

logger = get_logger(__name__)

CHECK_INTERVAL_S = 30
COOLDOWN_S = 300  # suppress repeat alerts for 5 minutes


class Severity(Enum):
    INFO = "INFO"
    WARNING = "WARNING"
    CRITICAL = "CRITICAL"


@dataclass(slots=True)
class AlertRule:
    name: str
    metric_name: str
    metric_type: str  # "counter", "gauge"
    threshold: float
    comparator: str  # "gt", "lt", "eq"
    severity: Severity
    message: str = ""


DEFAULT_RULES: list[AlertRule] = [
    AlertRule(
        name="event_loop_lag_high",
        metric_name="event_loop_lag_ms",
        metric_type="gauge",
        threshold=100.0,
        comparator="gt",
        severity=Severity.CRITICAL,
        message="Event loop lag exceeds 100ms",
    ),
    AlertRule(
        name="ws_market_disconnected",
        metric_name="ws_market_connected",
        metric_type="gauge",
        threshold=0.5,
        comparator="lt",
        severity=Severity.CRITICAL,
        message="Market WebSocket disconnected",
    ),
    AlertRule(
        name="no_messages",
        metric_name="ws_market_msg_age_s",
        metric_type="gauge",
        threshold=90.0,
        comparator="gt",
        severity=Severity.CRITICAL,
        message="No market data for >90 seconds",
    ),
    AlertRule(
        name="active_markets_zero",
        metric_name="active_markets",
        metric_type="gauge",
        threshold=0.5,
        comparator="lt",
        severity=Severity.CRITICAL,
        message="No active markets being tracked",
    ),
]


class AlertManager:
    """Periodic metric threshold checker with log-based alerting."""

    def __init__(
        self,
        rules: list[AlertRule] | None = None,
        webhook_fn: Callable[[str, Severity], Any] | None = None,
    ) -> None:
        self._rules = rules or list(DEFAULT_RULES)
        self._webhook_fn = webhook_fn
        self._last_fired: dict[str, float] = {}
        self._running = False

    def add_rule(self, rule: AlertRule) -> None:
        self._rules.append(rule)

    async def run(self) -> None:
        self._running = True
        try:
            while self._running:
                await asyncio.sleep(CHECK_INTERVAL_S)
                self._evaluate_rules()
        except asyncio.CancelledError:
            pass

    async def stop(self) -> None:
        self._running = False

    def _evaluate_rules(self) -> None:
        metrics = Metrics.get()
        now = time.time()

        for rule in self._rules:
            if rule.metric_type == "gauge":
                value = metrics.get_gauge(rule.metric_name)
            elif rule.metric_type == "counter":
                value = float(metrics.get_counter(rule.metric_name))
            else:
                continue

            triggered = False
            if rule.comparator == "gt" and value > rule.threshold:
                triggered = True
            elif rule.comparator == "lt" and value < rule.threshold:
                triggered = True
            elif rule.comparator == "eq" and abs(value - rule.threshold) < 1e-9:
                triggered = True

            if not triggered:
                continue

            last = self._last_fired.get(rule.name, 0.0)
            if now - last < COOLDOWN_S:
                continue

            self._last_fired[rule.name] = now
            msg = f"[ALERT:{rule.severity.value}] {rule.name}: {rule.message} (value={value:.2f})"

            if rule.severity == Severity.CRITICAL:
                logger.critical(msg)
            elif rule.severity == Severity.WARNING:
                logger.warning(msg)
            else:
                logger.info(msg)

            if self._webhook_fn:
                try:
                    self._webhook_fn(msg, rule.severity)
                except Exception:
                    logger.exception("Webhook alert failed")
