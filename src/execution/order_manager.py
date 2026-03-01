"""Order manager — full lifecycle tracking, dedup, and dry-run support.

Receives ``OrderIntent`` from strategies, validates through the risk
manager, submits via the REST client, and tracks every order from
submission through terminal state.
"""

from __future__ import annotations

import asyncio
import time
from datetime import date
from typing import Any

from ..core.event_bus import EventBus
from ..core.events import (
    OrderFill,
    OrderLive,
    OrderStatus,
    OrderSubmitted,
    OrderTerminal,
)
from ..core.models import OrderIntent, OrderState
from ..execution.risk_manager import RiskManager
from ..gateway.rest_client import AsyncRestClient
from ..logging_config import get_logger
from ..storage.persistence import AsyncPersistence

logger = get_logger(__name__)

STALE_ORDER_TIMEOUT_S = 300
RECONCILE_INTERVAL_S = 15
TERMINAL_RETENTION_S = 600


class OrderManager:
    """Manages order lifecycle: dedup -> risk check -> submit -> track."""

    def __init__(
        self,
        event_bus: EventBus,
        rest_client: AsyncRestClient,
        risk_manager: RiskManager,
        persistence: AsyncPersistence | None = None,
        dry_run: bool = False,
    ) -> None:
        self.event_bus = event_bus
        self.rest_client = rest_client
        self.risk_manager = risk_manager
        self._persistence = persistence
        self.dry_run = dry_run

        self._active_orders: dict[str, OrderState] = {}
        self._dedup: set[tuple[str, str, str]] = set()

        self._stats = {
            "submitted": 0,
            "rejected": 0,
            "failed": 0,
            "filled": 0,
            "partial": 0,
            "cancelled": 0,
            "expired": 0,
            "dedup_skips": 0,
            "risk_blocks": 0,
        }

    # ── Public API ────────────────────────────────────────────────────────

    @property
    def stats(self) -> dict[str, int]:
        return dict(self._stats)

    @property
    def active_orders(self) -> dict[str, OrderState]:
        return self._active_orders

    @property
    def pending_count(self) -> int:
        return sum(
            1 for o in self._active_orders.values() if not o.is_terminal
        )

    async def submit(self, intent: OrderIntent) -> OrderState | None:
        """Full pipeline: dedup -> risk -> submit -> track."""
        signal_ns = time.time_ns()

        if self._is_duplicate(intent):
            self._stats["dedup_skips"] += 1
            logger.info("[ORDER] Dedup skip: %s/%s", intent.slug, intent.token_id[:16])
            self._log_decision(intent, "SKIP", "DEDUP: already ordered")
            return None

        allowed, reason = self.risk_manager.check(intent)
        if not allowed:
            self._stats["risk_blocks"] += 1
            logger.warning("[ORDER] Risk blocked: %s — %s", intent.slug, reason)
            self._log_decision(intent, "SKIP", f"RISK: {reason}")
            return None

        # Record dedup BEFORE the async REST call.  This prevents the race
        # where multiple concurrent tick_size events all pass _is_duplicate()
        # before any of them has completed the await and reached this point.
        self._record_dedup(intent)

        result = await self.rest_client.place_order(intent, dry_run=self.dry_run)

        rest_ns = time.time_ns()

        if isinstance(result, OrderSubmitted):
            state = OrderState(
                order_id=result.order_id,
                intent=intent,
                status=OrderStatus.SUBMITTED,
                signal_ns=signal_ns,
                rest_response_ns=rest_ns,
                dry_run=self.dry_run,
            )
            self._active_orders[result.order_id] = state
            self.risk_manager.record_order(intent)
            self._stats["submitted"] += 1

            await self.event_bus.publish(result)
            self._persist_order(state)
            self._log_decision(intent, "TRADE", "Signal & eligibility met", result.order_id)
            return state

        if isinstance(result, OrderTerminal):
            state = OrderState(
                order_id=result.order_id or f"failed_{signal_ns}",
                intent=intent,
                status=result.status,
                signal_ns=signal_ns,
                rest_response_ns=rest_ns,
                rejection_reason=result.reason,
                resolved_at_ns=rest_ns,
                dry_run=self.dry_run,
            )

            if result.status == OrderStatus.REJECTED:
                self._stats["rejected"] += 1
            else:
                self._stats["failed"] += 1

            self._persist_order(state)
            self._log_decision(intent, "REJECTED", result.reason, result.order_id)
            return state

        return None

    def re_persist(self, state: OrderState) -> None:
        """Re-save an order state after external fields are updated."""
        self._persist_order(state)

    # ── Event handlers (subscribe via EventBus) ───────────────────────────

    async def on_order_fill(self, event: OrderFill) -> None:
        state = self._active_orders.get(event.order_id)
        if not state:
            return

        state.filled_size += event.fill_size
        if event.fill_price:
            state.fill_price = event.fill_price

        if state.filled_size >= state.intent.size * 0.99:
            state.status = OrderStatus.FILLED
            state.resolved_at_ns = time.time_ns()
            self._stats["filled"] += 1
            logger.info(
                "[ORDER] FILLED %s: %s @ %.4f x %.2f",
                event.order_id[:16], state.intent.slug,
                state.fill_price or state.intent.price, state.filled_size,
            )
        else:
            state.status = OrderStatus.PARTIAL
            self._stats["partial"] += 1

        self._persist_order(state)

    async def on_order_live(self, event: OrderLive) -> None:
        state = self._active_orders.get(event.order_id)
        if state and state.status == OrderStatus.SUBMITTED:
            state.status = OrderStatus.LIVE

    async def on_order_terminal(self, event: OrderTerminal) -> None:
        state = self._active_orders.get(event.order_id)
        if not state:
            return

        state.status = event.status
        state.rejection_reason = event.reason
        state.resolved_at_ns = time.time_ns()

        if event.status == OrderStatus.CANCELLED:
            self._stats["cancelled"] += 1
        elif event.status in (OrderStatus.EXPIRED, OrderStatus.EXPIRED_STALE):
            self._stats["expired"] += 1

        self.risk_manager.release_exposure(
            state.intent.slug, state.intent.price * state.intent.size
        )

        logger.info(
            "[ORDER] %s %s: %s",
            event.status.value, event.order_id[:16], state.intent.slug,
        )
        self._persist_order(state)

    # ── Stale order reaper ────────────────────────────────────────────────

    async def reap_stale_orders(self) -> None:
        """Background task: expire orders stuck pending > timeout and prune old terminal orders."""
        while True:
            await asyncio.sleep(60)
            now_ns = time.time_ns()

            stale: list[str] = []
            for oid, state in self._active_orders.items():
                if state.is_terminal:
                    continue
                age_s = (now_ns - state.placed_at_ns) / 1e9
                if age_s > STALE_ORDER_TIMEOUT_S:
                    stale.append(oid)

            for oid in stale:
                state = self._active_orders[oid]
                state.status = OrderStatus.EXPIRED_STALE
                state.resolved_at_ns = now_ns
                self._stats["expired"] += 1
                self.risk_manager.release_exposure(
                    state.intent.slug, state.intent.price * state.intent.size
                )
                logger.warning(
                    "[ORDER] Stale-expired %s: %s (pending %.0fs)",
                    oid[:16], state.intent.slug,
                    (now_ns - state.placed_at_ns) / 1e9,
                )
                self._persist_order(state)

            self._prune_terminal_orders(now_ns)

    # ── Terminal order pruning ────────────────────────────────────────────

    def _prune_terminal_orders(self, now_ns: int) -> None:
        """Remove terminal orders older than TERMINAL_RETENTION_S from memory."""
        to_remove: list[str] = []
        for oid, state in self._active_orders.items():
            if not state.is_terminal:
                continue
            resolved_ns = state.resolved_at_ns or state.placed_at_ns
            age_s = (now_ns - resolved_ns) / 1e9
            if age_s > TERMINAL_RETENTION_S:
                to_remove.append(oid)

        for oid in to_remove:
            del self._active_orders[oid]

        if to_remove:
            logger.info("[ORDER] Pruned %d terminal orders from memory", len(to_remove))

    # ── REST order reconciliation ─────────────────────────────────────────

    _CLOB_STATUS_MAP: dict[str, OrderStatus] = {
        "MATCHED": OrderStatus.FILLED,
        "FILLED": OrderStatus.FILLED,
        "CANCELED": OrderStatus.CANCELLED,
        "CANCELLED": OrderStatus.CANCELLED,
        "EXPIRED": OrderStatus.EXPIRED,
    }

    async def reconcile_orders(self) -> None:
        """Background task: poll the CLOB REST API to reconcile order statuses.

        Catches fills, cancellations, and expirations that the User WebSocket
        may have missed (e.g. during disconnects or restarts).
        """
        while True:
            await asyncio.sleep(RECONCILE_INTERVAL_S)

            pending = [
                (oid, state)
                for oid, state in self._active_orders.items()
                if not state.is_terminal and not state.dry_run and oid and oid != "unknown"
            ]
            if not pending:
                continue

            for oid, state in pending:
                try:
                    order_data = await self.rest_client.get_order(oid)
                except Exception:
                    logger.debug("[RECONCILE] REST fetch failed for %s", oid[:16], exc_info=True)
                    continue

                if order_data is None:
                    continue

                api_status = (
                    order_data.get("status", "")
                    or order_data.get("order_status", "")
                ).upper()

                matched_status = self._CLOB_STATUS_MAP.get(api_status)
                if matched_status is None:
                    continue

                if matched_status == OrderStatus.FILLED:
                    size_matched = 0.0
                    try:
                        size_matched = float(
                            order_data.get("size_matched")
                            or order_data.get("matched_amount")
                            or order_data.get("filled_size")
                            or state.intent.size
                        )
                    except (ValueError, TypeError):
                        size_matched = state.intent.size

                    fill_price = None
                    try:
                        raw = order_data.get("associate_trades", [])
                        if raw and isinstance(raw, list) and isinstance(raw[-1], dict):
                            fill_price = float(raw[-1].get("price", 0))
                    except (ValueError, TypeError, IndexError, AttributeError):
                        pass
                    if not fill_price:
                        try:
                            fill_price = float(order_data.get("price", 0)) or None
                        except (ValueError, TypeError):
                            pass

                    already_filled = state.filled_size
                    new_fill = size_matched - already_filled
                    if new_fill > 0.001:
                        logger.info(
                            "[RECONCILE] Fill detected for %s: +%.2f (total %.2f)",
                            oid[:16], new_fill, size_matched,
                        )
                        self.event_bus.publish_nowait(OrderFill(
                            order_id=oid,
                            fill_price=fill_price or state.intent.price,
                            fill_size=new_fill,
                            status=OrderStatus.PARTIAL,
                        ))
                else:
                    logger.info(
                        "[RECONCILE] Terminal status for %s: %s",
                        oid[:16], matched_status.value,
                    )
                    self.event_bus.publish_nowait(OrderTerminal(
                        order_id=oid,
                        status=matched_status,
                        reason=f"reconciled via REST ({api_status})",
                    ))

    # ── Dedup ─────────────────────────────────────────────────────────────

    def _is_duplicate(self, intent: OrderIntent) -> bool:
        key = (intent.slug, intent.token_id, intent.strategy)
        return key in self._dedup

    def _record_dedup(self, intent: OrderIntent) -> None:
        key = (intent.slug, intent.token_id, intent.strategy)
        self._dedup.add(key)
        if self._persistence:
            self._persistence.enqueue(
                "INSERT OR IGNORE INTO dedup (slug, token_id, strategy, session_date, created_at) VALUES (?, ?, ?, ?, ?)",
                (intent.slug, intent.token_id, intent.strategy, date.today().isoformat(), time.time()),
            )

    def load_dedup_from_db(self, conn: Any) -> None:
        """Load today's dedup set from SQLite on startup."""
        today = date.today().isoformat()
        cursor = conn.execute(
            "SELECT slug, token_id, strategy FROM dedup WHERE session_date = ?",
            (today,),
        )
        for row in cursor:
            self._dedup.add((row[0], row[1], row[2]))
        logger.info("[ORDER] Loaded %d dedup entries for %s", len(self._dedup), today)

    # ── Persistence helpers ───────────────────────────────────────────────

    def _persist_order(self, state: OrderState) -> None:
        if not self._persistence:
            return
        self._persistence.enqueue(
            "INSERT OR REPLACE INTO orders (order_id, strategy, token_id, slug, side, price, size, initial_status, final_status, rejection_reason, placed_at, resolved_at, signal_to_rest_ms, signal_to_fill_ms, tick_to_order_ms, time_to_expiry_s, dry_run) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                state.order_id,
                state.intent.strategy,
                state.intent.token_id,
                state.intent.slug,
                state.intent.side.value,
                state.intent.price,
                state.intent.size,
                OrderStatus.SUBMITTED.value,
                state.status.value,
                state.rejection_reason,
                state.placed_at_ns / 1e9,
                state.resolved_at_ns / 1e9 if state.resolved_at_ns else None,
                state.signal_to_rest_ms,
                state.signal_to_fill_ms,
                state.tick_to_order_ms,
                state.time_to_expiry_s,
                1 if state.dry_run else 0,
            ),
        )

    def _log_decision(
        self,
        intent: OrderIntent,
        decision: str,
        reason: str,
        order_id: str = "",
    ) -> None:
        if not self._persistence:
            return
        self._persistence.enqueue(
            "INSERT INTO decisions (timestamp, strategy, slug, trigger, decision, reason, order_id, dry_run) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            (
                time.time(),
                intent.strategy,
                intent.slug,
                "tick_size_change",
                decision,
                reason,
                order_id,
                1 if self.dry_run else 0,
            ),
        )
