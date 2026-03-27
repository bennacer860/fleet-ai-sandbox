"""Rich-based live terminal dashboard (TUI).

Renders a multi-panel view refreshed every second showing markets,
orders, P&L, risk status, system health, and a scrolling event feed.
Activated via ``--dashboard`` flag.
"""

from __future__ import annotations

import asyncio
import re
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
from ..markets.fifteen_min import detect_duration_from_slug
from ..utils.timestamps import format_slug_with_est_time
from .metrics import Metrics
from ..utils.telegram_notifier import TelegramNotifier
from ..config import (
    TELEGRAM_BOT_TOKEN, TELEGRAM_CHAT_ID, TELEGRAM_ENABLED,
    PROXIMITY_FILTER_ENABLED, PROXIMITY_MIN_DISTANCE,
)

if TYPE_CHECKING:
    from ..execution.auto_claimer import AutoClaimer
    from ..execution.order_manager import OrderManager
    from ..execution.position_tracker import PositionTracker
    from ..execution.risk_manager import RiskManager
    from ..gateway.crypto_ws import CryptoWebSocket
    from ..gateway.market_ws import MarketWebSocket
    from ..gateway.user_ws import UserWebSocket

logger = get_logger(__name__)

MAX_EVENTS = 35
REFRESH_INTERVAL = 1.0


class Dashboard:
    """Live terminal dashboard backed by Rich."""

    def __init__(
        self,
        market_ws: MarketWebSocket | None = None,
        user_ws: UserWebSocket | None = None,
        crypto_ws: CryptoWebSocket | None = None,
        order_manager: OrderManager | None = None,
        position_tracker: PositionTracker | None = None,
        risk_manager: RiskManager | None = None,
        dry_run: bool = False,
        profile: int | None = None,
        funder: str = "",
        claim_min_value: float | None = None,
        auto_claimer: AutoClaimer | None = None,
        eval_cache: dict[str, dict] | None = None,
        strategy_name: str = "sweep",
    ) -> None:
        self._market_ws = market_ws
        self._user_ws = user_ws
        self._crypto_ws = crypto_ws
        self._order_mgr = order_manager
        self._pos_tracker = position_tracker
        self._risk_mgr = risk_manager
        self._dry_run = dry_run
        self._profile = profile
        self._funder = funder
        self._claim_min_value = claim_min_value
        self._auto_claimer = auto_claimer
        self._eval_cache = eval_cache if eval_cache is not None else {}
        self._strategy_name = strategy_name
        self._recent_events: deque[str] = deque(maxlen=MAX_EVENTS)
        self._exchange_latencies: deque[float] = deque(maxlen=100)
        self._tick_latencies: deque[float] = deque(maxlen=100)
        self._sign_latencies: deque[float] = deque(maxlen=100)
        self._post_latencies: deque[float] = deque(maxlen=100)
        self._expiry_times: deque[float] = deque(maxlen=100)
        self._running = False
        self._slug_display_cache: dict[str, str] = {}
        
        # Telegram integration
        self._telegram = TelegramNotifier(
            token=TELEGRAM_BOT_TOKEN,
            chat_id=TELEGRAM_CHAT_ID,
            enabled=TELEGRAM_ENABLED
        )

    def _format_slug(self, slug: str) -> str:
        """Convert a raw slug to a human-readable form with EST time, cached."""
        if slug not in self._slug_display_cache:
            self._slug_display_cache[slug] = format_slug_with_est_time(slug)
        return self._slug_display_cache[slug]

    @staticmethod
    def _market_end_ts(slug: str) -> int:
        """Extract unix end timestamp from market slug if present."""
        m = re.search(r"-(\d{10})$", slug)
        if not m:
            return 0
        try:
            return int(m.group(1))
        except ValueError:
            return 0

    def _market_sort_key(self, slug: str, now_ts: int) -> tuple[int, int, str]:
        """Sort markets by window state: live, future, then stale."""
        end_ts = self._market_end_ts(slug)
        duration_m = detect_duration_from_slug(slug)
        if end_ts > 0 and duration_m:
            start_ts = end_ts - (duration_m * 60)
            if start_ts <= now_ts < end_ts:
                # Live market first, closest expiry first.
                return (0, end_ts, slug)
            if now_ts < start_ts:
                # Future market next, soonest start first.
                return (1, start_ts, slug)
            # Stale market last, most recently ended first.
            return (2, -end_ts, slug)
        # Unknown timestamp/duration: put after live/future.
        return (3, 10**12, slug)

    def push_latency(self, latency_ms: float) -> None:
        self._exchange_latencies.append(latency_ms)
        
    def push_order_metrics(
        self,
        tick_ms: float | None,
        expiry_s: float | None,
        sign_ms: float | None = None,
        post_ms: float | None = None,
    ) -> None:
        if tick_ms is not None:
            self._tick_latencies.append(tick_ms)
        if expiry_s is not None:
            self._expiry_times.append(expiry_s)
        if sign_ms is not None:
            self._sign_latencies.append(sign_ms)
        if post_ms is not None:
            self._post_latencies.append(post_ms)

    def push_event(self, text: str) -> None:
        ts = datetime.now().strftime("%H:%M:%S")
        self._recent_events.append(f"  {ts}  {text}")
        
        # Note: We don't push general events to Telegram from here to avoid noise. 
        # Important trading events are handled with specialized formatting in bot.py.

    # ── Panel builders ────────────────────────────────────────────────────

    def _header(self) -> Text:
        metrics = Metrics.get()
        uptime = int(metrics.uptime_s)
        h, m = divmod(uptime // 60, 60)
        tag = "  [DRY-RUN]" if self._dry_run else ""
        profile_tag = f"  Profile {self._profile}" if self._profile else ""
        strategy_tag = f"  Strategy: {self._strategy_name.upper()}"
        return Text(
            f"  POLYMARKET HFT BOT v1{profile_tag}{strategy_tag}          Uptime: {h}h {m:02d}m{tag}",
            style="bold white on blue",
        )

    @staticmethod
    def _fmt_strike(price: float) -> str:
        if price >= 100:
            return f"${price:,.2f}"
        elif price >= 1:
            return f"${price:.4f}"
        return f"${price:.6f}"

    def _markets_panel(self) -> Panel:
        table = Table(show_header=True, header_style="bold", box=None, pad_edge=False)
        table.add_column("Market", min_width=12)
        table.add_column("Prices", min_width=20)
        table.add_column("Strike", width=12, justify="right")
        table.add_column("", width=3)

        if self._market_ws:
            active = [s for s, a in self._market_ws.market_active.items() if a]
            # Show currently live markets first, then future windows.
            now_ts = int(time.time())
            active.sort(key=lambda s: self._market_sort_key(s, now_ts))
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

                eval_data = self._eval_cache.get(slug, {})
                ptb = eval_data.get("price_to_beat")
                strike_str = self._fmt_strike(ptb) if ptb is not None else "[dim]--[/dim]"

                table.add_row(display, price_str, strike_str, "[green]OK[/green]")
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

            if self._tick_latencies:
                t_lats = list(self._tick_latencies)
                avg_tick = sum(t_lats) / len(t_lats)
                last_tick = t_lats[-1]
                lines.append(f"Tick→Order: {last_tick:.0f}ms (avg {avg_tick:.0f}ms)")

            if self._sign_latencies:
                s = list(self._sign_latencies)
                lines.append(f"  Sign:  min={min(s):.0f}  avg={sum(s)/len(s):.0f}  max={max(s):.0f}ms")
            if self._post_latencies:
                p = list(self._post_latencies)
                lines.append(f"  Post:  min={min(p):.0f}  avg={sum(p)/len(p):.0f}  max={max(p):.0f}ms")
            
            if self._expiry_times:
                e_vals = list(self._expiry_times)
                avg_exp = sum(e_vals) / len(e_vals)
                last_exp = e_vals[-1]
                lines.append(f"To Expiry:  {last_exp:.0f}s  (avg {avg_exp:.0f}s)")

            if self._exchange_latencies:
                lats = list(self._exchange_latencies)
                avg_lat = sum(lats) / len(lats)
                min_lat = min(lats)
                max_lat = max(lats)
                lines.append(f"Exch Latency: avg {avg_lat:.1f}ms | min {min_lat:.1f}ms | max {max_lat:.1f}ms")
        else:
            lines.append("No data")
        return Panel("\n".join(lines), title="ORDERS", border_style="yellow")

    def _pnl_panel(self) -> Panel:
        lines: list[str] = []
        if self._pos_tracker:
            pt = self._pos_tracker
            tag = " [SIMULATED]" if self._dry_run else ""
            lines.append(f"Session:   ${pt.session_pnl:.4f}{tag}")
            lines.append(f"Win Rate:  {pt.win_rate:.1%} ({pt.wins}/{pt.trades_closed})")
            lines.append(f"EV/Trade:  ${pt.ev_per_trade:.4f}")
            lines.append(f"Unrealised: ${pt.get_total_unrealized_pnl():.4f}{tag}")
        else:
            lines.append("No data")
        title = "P&L [SIMULATED]" if self._dry_run else "P&L"
        return Panel("\n".join(lines), title=title, border_style="green")

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

        # Account info
        if self._profile or self._funder:
            profile_str = f"Profile {self._profile}" if self._profile else "Default"
            funder_short = f"{self._funder[:6]}...{self._funder[-4:]}" if len(self._funder) > 12 else (self._funder or "N/A")
            lines.append(f"Account:   {profile_str}  Funder: {funder_short}")

        # AutoClaim status
        ac = self._auto_claimer
        if ac and self._claim_min_value is not None:
            interval_m = int(ac.interval / 60)
            last_check = ""
            if ac.last_check_time:
                age = int(time.time() - ac.last_check_time)
                last_check = f"  Last check: {age}s ago"
            claimed_str = f"  Claimed: {ac.total_claimed}" if ac.total_claimed else ""
            lines.append(
                f"AutoClaim: [green]ON[/green] (>= ${self._claim_min_value:.2f} every {interval_m}m){last_check}{claimed_str}"
            )
        else:
            lines.append("AutoClaim: [dim]OFF[/dim]")

        if self._market_ws and self._market_ws.connected:
            ws_market = "[green]Connected[/green]"
        elif self._market_ws:
            ws_market = "[red]Disconnected[/red]"
        else:
            ws_market = "Disconnected"
        token_count = sum(len(t) for t in self._market_ws.token_ids.values()) if self._market_ws else 0
        market_reconnects = self._market_ws.reconnect_count if self._market_ws else 0
        msg_age = f"{self._market_ws.last_message_age_s:.0f}s ago" if self._market_ws and self._market_ws.last_message_age_s >= 0 else "N/A"

        if self._user_ws:
            user_reconnects = self._user_ws.reconnect_count
            user_age = self._user_ws.last_message_age_s
            user_age_str = f"Last msg: {user_age:.0f}s ago" if user_age >= 0 else "Last msg: N/A"
            if self._dry_run:
                ws_user = "[yellow]Skipped (dry-run)[/yellow]"
            elif self._user_ws.connected:
                ws_user = f"[green]Connected[/green]  (reconnects: {user_reconnects})   {user_age_str}"
            else:
                ws_user = f"[red]Disconnected[/red]  (reconnects: {user_reconnects})   {user_age_str}"
        else:
            ws_user = "[dim]N/A[/dim]"

        if self._crypto_ws:
            if self._crypto_ws.connected:
                crypto_age = self._crypto_ws.last_message_age_s
                age_str = f"{crypto_age:.0f}s ago" if crypto_age >= 0 else "N/A"
                n_prices = len(self._crypto_ws.latest_prices)
                def _fmt(p: float) -> str:
                    if p >= 100:
                        return f"${p:,.2f}"
                    elif p >= 1:
                        return f"${p:.4f}"
                    return f"${p:.6f}"

                prices_str = "  ".join(
                    f"{asset}={_fmt(price)}"
                    for asset, price in sorted(self._crypto_ws.latest_prices.items())
                )
                crypto_reconnects = self._crypto_ws.reconnect_count if self._crypto_ws else 0
                ws_crypto = f"[green]Connected[/green]  ({n_prices} assets)  (reconnects: {crypto_reconnects})   Last msg: {age_str}"
                ws_crypto_prices = prices_str
            else:
                crypto_reconnects = self._crypto_ws.reconnect_count if self._crypto_ws else 0
                ws_crypto = f"[red]Disconnected[/red]  (reconnects: {crypto_reconnects})"
                ws_crypto_prices = ""
        else:
            ws_crypto = "[dim]N/A[/dim]"
            ws_crypto_prices = ""

        lines.append(f"WS Market: {ws_market} ({token_count} tokens)  (reconnects: {market_reconnects})   Last msg: {msg_age}")
        lines.append(f"WS User:   {ws_user}")
        lines.append(f"WS Crypto: {ws_crypto}")
        if ws_crypto_prices:
            lines.append(f"  Spot:    {ws_crypto_prices}")
        if PROXIMITY_FILTER_ENABLED:
            lines.append(f"Proximity:  [green]ON[/green]  (threshold: {PROXIMITY_MIN_DISTANCE:.4%})")
        else:
            lines.append("Proximity:  [yellow]OFF[/yellow]")
        lines.append(f"Event Loop Lag: {metrics.get_gauge('event_loop_lag_ms'):.1f}ms")
        lines.append(f"SQLite Queue: {metrics.get_gauge('persistence_pending'):.0f} pending")

        return Panel("\n".join(lines), title="SYSTEM", border_style="blue")

    def _positions_panel(self) -> Panel:
        table = Table(show_header=True, header_style="bold", box=None, pad_edge=False)
        table.add_column("Market", min_width=16)
        table.add_column("Side", width=10)
        table.add_column("Qty", width=6, justify="right")
        table.add_column("Entry", width=7, justify="right")
        table.add_column("Current", width=7, justify="right")
        table.add_column("uPnL", width=8, justify="right")

        if self._pos_tracker:
            all_positions = self._pos_tracker.positions
            filtered = {
                tid: pos for tid, pos in all_positions.items()
                if pos.strategy == self._strategy_name
            }
            if filtered:
                for tid, pos in list(filtered.items())[:8]:
                    display = self._format_slug(pos.slug) if pos.slug else tid[:16]
                    current = 0.0
                    if self._market_ws:
                        bp = self._market_ws.best_prices.get(tid, {})
                        current = bp.get("bid", 0.0)
                    upnl = pos.unrealized_pnl(current) if current > 0 else 0.0
                    pnl_style = "green" if upnl >= 0 else "red"
                    outcome = "?"
                    if self._market_ws:
                        raw_outcome = self._market_ws.token_outcomes.get(tid)
                        if raw_outcome:
                            outcome = str(raw_outcome).upper()
                    side_label = f"BUY {outcome}" if outcome != "?" else "BUY"

                    table.add_row(
                        display,
                        side_label,
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

        count = len(filtered) if self._pos_tracker and filtered else 0
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
            Layout(name="top", size=12),
            Layout(name="middle", size=5),
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
            Layout(self._system_panel(), name="system", size=12),
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
