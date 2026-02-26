"""Rich-based live terminal dashboard (TUI).

Renders a multi-panel view refreshed every second showing markets,
orders, P&L, risk status, system health, and a scrolling event feed.
Activated via ``--dashboard`` flag.
"""

from __future__ import annotations

import asyncio
import time
from collections import deque
from datetime import datetime
from typing import TYPE_CHECKING, Any

from rich.layout import Layout
from rich.live import Live
from rich.panel import Panel
from rich.table import Table
from rich.text import Text

from ..logging_config import get_logger
from ..utils.timestamps import format_slug_with_est_time
from .metrics import Metrics

if TYPE_CHECKING:
    from ..execution.order_manager import OrderManager
    from ..execution.position_tracker import PositionTracker
    from ..execution.risk_manager import RiskManager
    from ..gateway.market_ws import MarketWebSocket
    from ..gateway.user_ws import UserWebSocket

logger = get_logger(__name__)

MAX_EVENTS = 15
REFRESH_INTERVAL = 1.0


class Dashboard:
    """Live terminal dashboard backed by Rich."""

    def __init__(
        self,
        market_ws: MarketWebSocket | None = None,
        user_ws: UserWebSocket | None = None,
        order_manager: OrderManager | None = None,
        position_tracker: PositionTracker | None = None,
        risk_manager: RiskManager | None = None,
        dry_run: bool = False,
    ) -> None:
        self._market_ws = market_ws
        self._user_ws = user_ws
        self._order_mgr = order_manager
        self._pos_tracker = position_tracker
        self._risk_mgr = risk_manager
        self._dry_run = dry_run
        self._recent_events: deque[str] = deque(maxlen=MAX_EVENTS)
        self._running = False
        self._slug_display_cache: dict[str, str] = {}

    def _format_slug(self, slug: str) -> str:
        """Convert a raw slug to a human-readable form with EST time, cached."""
        if slug not in self._slug_display_cache:
            self._slug_display_cache[slug] = format_slug_with_est_time(slug)
        return self._slug_display_cache[slug]

    def push_event(self, text: str) -> None:
        ts = datetime.now().strftime("%H:%M:%S")
        self._recent_events.append(f"  {ts}  {text}")

    # ── Panel builders ────────────────────────────────────────────────────

    def _header(self) -> Text:
        metrics = Metrics.get()
        uptime = int(metrics.uptime_s)
        h, m = divmod(uptime // 60, 60)
        tag = "  [DRY-RUN]" if self._dry_run else ""
        return Text(
            f"  POLYMARKET HFT BOT v1          Uptime: {h}h {m:02d}m{tag}",
            style="bold white on blue",
        )

    def _markets_panel(self) -> Panel:
        table = Table(show_header=True, header_style="bold", box=None, pad_edge=False)
        table.add_column("Market", min_width=12)
        table.add_column("Prices", min_width=20)
        table.add_column("", width=3)

        if self._market_ws:
            active = [s for s, a in self._market_ws.market_active.items() if a]
            for slug in active[:12]:
                tids = self._market_ws.token_ids.get(slug, [])
                parts: list[str] = []
                for tid in tids[:2]:
                    outcome = self._market_ws.token_outcomes.get(tid, "?")
                    bp = self._market_ws.best_prices.get(tid, {})
                    price = bp.get("bid", 0.0)
                    parts.append(f"{outcome}:{price:.2f}")
                price_str = "  ".join(parts)
                display = self._format_slug(slug)
                table.add_row(display, price_str, "[green]OK[/green]")
            count = len(active)
        else:
            count = 0

        return Panel(table, title=f"MARKETS ({count} active)", border_style="cyan")

    def _orders_panel(self) -> Panel:
        lines: list[str] = []
        if self._order_mgr:
            s = self._order_mgr.stats
            lines.append(f"Placed:    {s.get('submitted', 0):<6} Filled:   {s.get('filled', 0)}")
            lines.append(f"Rejected:  {s.get('rejected', 0):<6} Pending:  {self._order_mgr.pending_count}")
            lines.append(f"Cancelled: {s.get('cancelled', 0):<6} Expired:  {s.get('expired', 0)}")
            total = s.get("submitted", 0)
            filled = s.get("filled", 0)
            rate = (filled / total * 100) if total > 0 else 0
            lines.append(f"Fill Rate: {rate:.1f}%")
            lines.append(f"Dedup Skips: {s.get('dedup_skips', 0)}")
        else:
            lines.append("No data")
        return Panel("\n".join(lines), title="ORDERS", border_style="yellow")

    def _pnl_panel(self) -> Panel:
        lines: list[str] = []
        if self._pos_tracker:
            pt = self._pos_tracker
            lines.append(f"Session:   ${pt.session_pnl:.4f}")
            lines.append(f"Win Rate:  {pt.win_rate:.1%} ({pt.wins}/{pt.trades_closed})")
            lines.append(f"EV/Trade:  ${pt.ev_per_trade:.4f}")
            lines.append(f"Unrealised: ${pt.get_total_unrealized_pnl():.4f}")
        else:
            lines.append("No data")
        return Panel("\n".join(lines), title="P&L", border_style="green")

    def _risk_panel(self) -> Panel:
        lines: list[str] = []
        if self._risk_mgr:
            rm = self._risk_mgr
            cfg = rm.config
            lines.append(f"Exposure:  ${rm.total_exposure:.2f} / ${cfg.max_total_exposure:.0f}")
            lines.append(f"Daily Loss: ${rm.daily_loss:.2f} / ${cfg.max_daily_loss:.0f}")
            lines.append(f"Orders/min: {rm.orders_last_minute} / {cfg.max_orders_per_minute}")
            cb_status = "[red]TRIPPED[/red]" if rm.circuit_breaker_active else "[green]OK[/green]"
            lines.append(f"Circuit Breaker: {cb_status}")
        else:
            lines.append("No data")
        return Panel("\n".join(lines), title="RISK", border_style="red")

    def _system_panel(self) -> Panel:
        metrics = Metrics.get()
        lines: list[str] = []

        ws_market = "Connected" if (self._market_ws and self._market_ws.connected) else "Disconnected"
        token_count = sum(len(t) for t in self._market_ws.token_ids.values()) if self._market_ws else 0
        msg_age = f"{self._market_ws.last_message_age_s:.0f}s ago" if self._market_ws and self._market_ws.last_message_age_s >= 0 else "N/A"

        if self._user_ws:
            if self._dry_run:
                ws_user = "[yellow]Skipped (dry-run)[/yellow]"
            elif self._user_ws.connected:
                ws_user = f"[green]Connected[/green]  (reconnects: {self._user_ws.reconnect_count})"
            else:
                ws_user = f"[red]Disconnected[/red]  (reconnects: {self._user_ws.reconnect_count})"
        else:
            ws_user = "[dim]N/A[/dim]"

        lines.append(f"WS Market: {ws_market} ({token_count} tokens)   Last msg: {msg_age}")
        lines.append(f"WS User:   {ws_user}")
        lines.append(f"Event Loop Lag: {metrics.get_gauge('event_loop_lag_ms'):.1f}ms")
        lines.append(f"SQLite Queue: {metrics.get_gauge('persistence_pending'):.0f} pending")

        return Panel("\n".join(lines), title="SYSTEM", border_style="blue")

    def _positions_panel(self) -> Panel:
        table = Table(show_header=True, header_style="bold", box=None, pad_edge=False)
        table.add_column("Market", min_width=16)
        table.add_column("Side", width=4)
        table.add_column("Qty", width=6, justify="right")
        table.add_column("Entry", width=7, justify="right")
        table.add_column("Current", width=7, justify="right")
        table.add_column("uPnL", width=8, justify="right")

        if self._pos_tracker:
            positions = self._pos_tracker.positions
            if positions:
                for tid, pos in list(positions.items())[:8]:
                    display = self._format_slug(pos.slug) if pos.slug else tid[:16]
                    current = 0.0
                    if self._market_ws:
                        bp = self._market_ws.best_prices.get(tid, {})
                        current = bp.get("bid", 0.0)
                    upnl = pos.unrealized_pnl(current) if current > 0 else 0.0
                    pnl_style = "green" if upnl >= 0 else "red"
                    table.add_row(
                        display,
                        "BUY",
                        f"{pos.quantity:.1f}",
                        f"{pos.avg_entry_price:.4f}",
                        f"{current:.4f}" if current > 0 else "[dim]--[/dim]",
                        f"[{pnl_style}]${upnl:+.4f}[/{pnl_style}]",
                    )
            else:
                table.add_row("[dim]No open positions[/dim]", "", "", "", "", "")

            if self._pos_tracker.trades_closed > 0:
                table.add_row("", "", "", "", "", "")
                table.add_row(
                    f"[dim]Closed: {self._pos_tracker.trades_closed}[/dim]",
                    "", "", "",
                    "[dim]Realized:[/dim]",
                    f"[dim]${self._pos_tracker.total_realized_pnl:+.4f}[/dim]",
                )
        else:
            table.add_row("[dim]No data[/dim]", "", "", "", "", "")

        count = len(self._pos_tracker.positions) if self._pos_tracker else 0
        return Panel(table, title=f"POSITIONS ({count} open)", border_style="green")

    def _events_panel(self) -> Panel:
        if self._recent_events:
            content = "\n".join(self._recent_events)
        else:
            content = "  (waiting for events...)"
        return Panel(content, title=f"RECENT EVENTS (last {MAX_EVENTS})", border_style="magenta")

    # ── Layout assembly ───────────────────────────────────────────────────

    def _build_layout(self) -> Layout:
        layout = Layout()
        layout.split_column(
            Layout(name="header", size=1),
            Layout(name="top", size=9),
            Layout(name="middle", size=8),
            Layout(name="positions", size=8),
            Layout(name="bottom"),
        )
        layout["header"].update(self._header())
        layout["top"].split_row(
            Layout(self._markets_panel(), name="markets"),
            Layout(self._orders_panel(), name="orders"),
        )
        layout["middle"].split_row(
            Layout(self._pnl_panel(), name="pnl"),
            Layout(self._risk_panel(), name="risk"),
        )
        layout["positions"].update(self._positions_panel())
        layout["bottom"].split_column(
            Layout(self._system_panel(), name="system", size=7),
            Layout(self._events_panel(), name="events"),
        )
        return layout

    # ── Run loop ──────────────────────────────────────────────────────────

    async def run(self) -> None:
        self._running = True
        logger.info("[DASHBOARD] Starting terminal dashboard")

        loop = asyncio.get_event_loop()

        def _render_in_thread() -> None:
            with Live(self._build_layout(), refresh_per_second=1, screen=True) as live:
                while self._running:
                    time.sleep(REFRESH_INTERVAL)
                    try:
                        live.update(self._build_layout())
                    except Exception:
                        pass

        try:
            await loop.run_in_executor(None, _render_in_thread)
        except asyncio.CancelledError:
            pass
        finally:
            self._running = False

    async def stop(self) -> None:
        self._running = False
