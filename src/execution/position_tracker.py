"""Live position and P&L tracking.

Subscribes to order fill and market resolution events to maintain
an up-to-date picture of open positions, unrealised P&L, and
cumulative realised P&L.
"""

from __future__ import annotations

import sqlite3
import time
from typing import Any

from ..core.events import BookUpdate, MarketResolved, OrderFill, OrderStatus
from ..core.models import Position, Side, TradeRecord
from ..logging_config import get_logger
from ..storage.persistence import AsyncPersistence

logger = get_logger(__name__)


class PositionTracker:
    """Tracks live positions and computes P&L from events."""

    def __init__(self, persistence: AsyncPersistence | None = None) -> None:
        self._positions: dict[str, Position] = {}
        self._persistence = persistence

        self._order_meta: dict[str, dict[str, Any]] = {}
        self._best_prices: dict[str, float] = {}

        self.total_realized_pnl: float = 0.0
        self.session_pnl: float = 0.0
        self.wins: int = 0
        self.losses: int = 0
        self.trades_closed: int = 0

    # ── Startup ────────────────────────────────────────────────────────────

    def load_positions_from_db(self, conn: sqlite3.Connection) -> None:
        """Restore open positions from SQLite on startup.

        Only loads positions with quantity > 0 (still open).
        """
        cursor = conn.execute(
            "SELECT token_id, strategy, slug, quantity, avg_entry_price, realized_pnl "
            "FROM positions WHERE quantity > 0"
        )
        loaded = 0
        for row in cursor:
            token_id, strategy, slug, quantity, avg_entry_price, realized_pnl = row
            pos = Position(
                token_id=token_id,
                slug=slug,
                strategy=strategy,
                quantity=quantity,
                avg_entry_price=avg_entry_price,
                realized_pnl=realized_pnl,
            )
            self._positions[token_id] = pos
            loaded += 1

        if loaded:
            self.total_realized_pnl = sum(p.realized_pnl for p in self._positions.values())
            logger.info(
                "[POSITION] Restored %d open positions from database (realized_pnl=$%.4f)",
                loaded, self.total_realized_pnl,
            )
        else:
            logger.info("[POSITION] No open positions found in database")

    def reset_session_metrics(self) -> None:
        """Reset per-process session metrics while keeping open positions intact."""
        self.session_pnl = 0.0
        self.wins = 0
        self.losses = 0
        self.trades_closed = 0

    def clear_positions_for_slug(self, slug: str) -> int:
        """Drop open positions for a market slug from memory and DB."""
        to_clear = [tid for tid, pos in self._positions.items() if pos.slug == slug and pos.quantity > 0]
        if not to_clear:
            return 0

        for tid in to_clear:
            pos = self._positions.pop(tid, None)
            if pos and self._persistence:
                self._persistence.enqueue(
                    "DELETE FROM positions WHERE token_id = ? AND strategy = ?",
                    (tid, pos.strategy),
                )
        return len(to_clear)

    # ── Public API ────────────────────────────────────────────────────────

    @property
    def positions(self) -> dict[str, Position]:
        return self._positions

    def get_exposure(self, strategy: str | None = None) -> float:
        total = 0.0
        for pos in self._positions.values():
            if strategy and pos.strategy != strategy:
                continue
            total += pos.cost_basis
        return total

    def get_total_unrealized_pnl(self) -> float:
        total = 0.0
        for pos in self._positions.values():
            price = self._best_prices.get(pos.token_id, pos.avg_entry_price)
            total += pos.unrealized_pnl(price)
        return total

    def get_total_position_value(self) -> float:
        """Return mark-to-market value of all open positions."""
        total = 0.0
        for pos in self._positions.values():
            if pos.quantity <= 0:
                continue
            price = self._best_prices.get(pos.token_id, pos.avg_entry_price)
            total += pos.quantity * price
        return total

    def register_order(
        self,
        order_id: str,
        token_id: str,
        slug: str,
        strategy: str,
        side: str,
        price: float,
        size: float,
        spot_price: float | None = None,
    ) -> None:
        """Register an order for later fill tracking."""
        self._order_meta[order_id] = {
            "token_id": token_id,
            "slug": slug,
            "strategy": strategy,
            "side": side,
            "price": price,
            "size": size,
            "placed_at": time.time(),
            "spot_price": spot_price,
        }

    # ── Event handlers ────────────────────────────────────────────────────

    async def on_book_update(self, event: BookUpdate) -> None:
        self._best_prices[event.token_id] = event.best_bid

    async def on_fill(self, event: OrderFill) -> None:
        meta = self._order_meta.get(event.order_id)
        if not meta:
            return

        token_id = meta["token_id"]
        slug = meta["slug"]
        strategy = meta["strategy"]
        side = Side.BUY if meta["side"] == "BUY" else Side.SELL

        pos = self._positions.get(token_id)
        if pos is None:
            pos = Position(
                token_id=token_id,
                slug=slug,
                strategy=strategy,
                spot_price=meta.get("spot_price"),
            )
            self._positions[token_id] = pos

        fill_price = event.fill_price if event.fill_price else meta["price"]
        pos.apply_fill(event.fill_size, fill_price, side)

        if self._persistence:
            self._persistence.enqueue(
                "INSERT OR REPLACE INTO positions (token_id, strategy, slug, quantity, avg_entry_price, realized_pnl, updated_at) VALUES (?, ?, ?, ?, ?, ?, ?)",
                (token_id, strategy, slug, pos.quantity, pos.avg_entry_price, pos.realized_pnl, time.time()),
            )

    async def on_market_resolved(self, event: MarketResolved) -> None:
        """Close all positions for the resolved market."""
        to_close: list[str] = []
        for tid, pos in self._positions.items():
            if pos.slug != event.slug or pos.quantity <= 0:
                continue

            exit_price = 1.0 if tid == event.winning_token_id else 0.0
            pnl = pos.quantity * (exit_price - pos.avg_entry_price)

            self.total_realized_pnl += pnl
            self.session_pnl += pnl
            self.trades_closed += 1
            if pnl >= 0:
                self.wins += 1
            else:
                self.losses += 1

            logger.info(
                "[POSITION] Closed %s/%s: %.2f shares @ entry=%.4f exit=%.4f pnl=$%.4f",
                pos.slug, pos.strategy, pos.quantity, pos.avg_entry_price, exit_price, pnl,
            )

            if self._persistence:
                self._persistence.enqueue(
                    "INSERT INTO trades (trade_id, strategy, slug, token_id, side, entry_price, exit_price, size, gross_pnl, net_pnl, timestamp_entry, timestamp_exit, spot_price, dry_run) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                    (
                        f"{tid}_{int(time.time())}",
                        pos.strategy, pos.slug, tid, "BUY",
                        pos.avg_entry_price, exit_price, pos.quantity,
                        pnl, pnl, 0.0, time.time(), pos.spot_price, 0,
                    ),
                )
                # Mark position closed in DB so startup won't reload/reconcile it again.
                self._persistence.enqueue(
                    "DELETE FROM positions WHERE token_id = ? AND strategy = ?",
                    (tid, pos.strategy),
                )

            to_close.append(tid)

        for tid in to_close:
            self._positions.pop(tid, None)

    @property
    def win_rate(self) -> float:
        if self.trades_closed == 0:
            return 0.0
        return self.wins / self.trades_closed

    @property
    def ev_per_trade(self) -> float:
        if self.trades_closed == 0:
            return 0.0
        return self.session_pnl / self.trades_closed
