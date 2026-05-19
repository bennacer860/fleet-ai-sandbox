"""Notification bridge — handles dashboard, Telegram, and persistence side effects.

This module encapsulates all display/notification logic that was previously
scattered through Bot. It implements DispatchObserver for integration with
StrategyDispatcher.

The formatting functions are pure and testable. The bridge itself coordinates
side effects but doesn't contain business logic.
"""

from __future__ import annotations

import re
import time
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

from .core.events import (
    MarketResolved,
    OrderFill,
    OrderStatus,
    OrderTerminal,
    TickSizeChange,
)
from .core.models import OrderIntent, OrderState
from .logging_config import get_logger
from .strategy.base import Strategy, StrategyContext
from .utils.timestamps import format_slug_with_est_time

if TYPE_CHECKING:
    from .monitoring.dashboard import Dashboard
    from .storage.persistence import AsyncPersistence
    from .utils.telegram_notifier import TelegramNotifier

logger = get_logger(__name__)


# ── Pure formatting functions (testable) ──────────────────────────────────────


def clean_reason(reason: str) -> str:
    """Strip technical wrappers from error messages for display.

    Removes common exception prefixes and extracts error messages from
    PolyApiException format.
    """
    if not reason:
        return ""

    r = reason
    prefixes = ["EXCEPTION: ", "Exception: ", "PolyApiException: ", "AttributeError: "]
    for p in prefixes:
        if r.startswith(p):
            r = r[len(p) :]

    if "PolyApiException[" in r:
        match = re.search(r"error_message=({.*?})", r)
        if match:
            try:
                import ast

                d = ast.literal_eval(match.group(1))
                r = d.get("error") or d.get("errorMsg") or r
            except Exception:
                pass

    return r


def fmt_price(price: float) -> str:
    """Format a price for display with appropriate precision."""
    if price >= 100:
        return f"${price:,.2f}"
    elif price >= 1:
        return f"${price:.4f}"
    return f"${price:.6f}"


def format_proximity(
    spot: float | None,
    strike: float | None,
    proximity: float | None = None,
    age_ms: float | None = None,
) -> str:
    """Format proximity data for display."""
    parts: list[str] = []

    if spot is not None:
        parts.append(f"spot={fmt_price(spot)}")
    else:
        parts.append("spot=STALE")

    if strike is not None:
        parts.append(f"strike={fmt_price(strike)}")
    else:
        parts.append("strike=--")

    if proximity is not None:
        parts.append(f"prox={proximity:.3%}")

    if age_ms is not None:
        parts.append(f"age={age_ms:.0f}ms")

    return "  " + " ".join(parts) if parts else ""


def format_timing(state: OrderState) -> str:
    """Format order timing metrics for display."""
    timing = ""
    tick_ms = state.tick_to_order_ms
    expiry_s = state.time_to_expiry_s

    if tick_ms is not None:
        timing += f"  tick→order={tick_ms:.0f}ms"
        q_ms = state.queue_wait_ms
        e_ms = state.eval_ms
        r_ms = state.signal_to_rest_ms
        if q_ms is not None and e_ms is not None and r_ms is not None:
            rest_detail = f"rest={r_ms:.0f}ms"
            if state.sign_ms is not None and state.post_ms is not None:
                rest_detail = f"sign={state.sign_ms:.0f}ms post={state.post_ms:.0f}ms"
            timing += f" (bus={q_ms:.0f}ms eval={e_ms:.0f}ms {rest_detail})"

    if expiry_s is not None:
        timing += f"  expires={expiry_s:.0f}s"

    return timing


def build_telegram_message(
    color_emoji: str,
    title: str,
    body: str,
    profile: str,
) -> str:
    """Build a formatted Telegram message."""
    return (
        f"{color_emoji} <b>{title}</b>\n"
        f"👤 <b>Profile:</b> <code>{profile}</code>\n"
        f"━━━━━━━━━━━━━━━\n"
        f"{body}"
    )


def format_skip_reason(reason: str) -> str:
    """Format a skip reason with appropriate styling for Rich."""
    if "stale" in reason.lower():
        return f"[bold red]BLOCKED: {reason}[/bold red]"
    elif "proximity" in reason.lower():
        return f"[bold magenta]BLOCKED: {reason}[/bold magenta]"
    return reason


# ── NotificationBridge ────────────────────────────────────────────────────────


@dataclass
class NotificationBridge:
    """Coordinates notification side effects for the bot.

    Implements the DispatchObserver protocol for integration with StrategyDispatcher.
    Can also be used directly for order lifecycle events.
    """

    dashboard: "Dashboard | None" = None
    telegram: "TelegramNotifier | None" = None
    persistence: "AsyncPersistence | None" = None
    profile: str = "0"
    dry_run: bool = False
    tag: str = ""

    # For tracking dedup/risk stats
    _last_dedup_skips: int = field(default=0, init=False)
    _last_risk_blocks: int = field(default=0, init=False)

    # ── DispatchObserver implementation ───────────────────────────────────────

    async def on_strategy_skip(
        self,
        event: Any,
        strategy: Strategy,
        reason: str,
        ctx: StrategyContext,
    ) -> None:
        """Called when a strategy returns no intents."""
        if not self.dashboard:
            return

        if not isinstance(event, TickSizeChange):
            return

        display_slug = format_slug_with_est_time(event.slug)
        price = getattr(strategy, "last_best_price", None)
        price_str = f"  price={price:.3f}" if price is not None else ""
        watching = getattr(strategy, "last_watching", False)

        if watching:
            self.dashboard.push_event(
                f"👀 [yellow]WATCHING[/yellow]  {display_slug}{price_str}  waiting for bid >= threshold"
            )
        else:
            reason_fmt = format_skip_reason(reason)
            self.dashboard.push_event(
                f"⏭️ [dim]NO_TRADE[/dim]  {display_slug}{price_str}  {reason_fmt}"
            )

        # Log decision to persistence
        if self.persistence and getattr(strategy, "last_skip_reason", None):
            self.persistence.enqueue(
                "INSERT INTO decisions (timestamp, strategy, slug, trigger, decision, reason, dry_run, tag) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    time.time(),
                    strategy.name(),
                    event.slug,
                    "tick_size_change",
                    "SKIP",
                    reason,
                    1 if self.dry_run else 0,
                    self.tag,
                ),
            )

    async def on_intent_submitted(
        self,
        intent: OrderIntent,
        state: OrderState | None,
        strategy: Strategy,
        event: Any,
        ctx: StrategyContext,
    ) -> None:
        """Called after an intent is submitted to OrderManager."""
        display_slug = format_slug_with_est_time(intent.slug)

        # Handle rejection/dedup case
        if state is None:
            await self._notify_submission_blocked(intent, display_slug)
            return

        # Handle successful submission
        await self._notify_submission_success(intent, state, strategy, display_slug)

    async def on_market_resolved(self, event: MarketResolved) -> None:
        """Called when a market resolves."""
        if not self.dashboard:
            return

        display_slug = format_slug_with_est_time(event.slug)
        self.dashboard.push_event(
            f"🏁 [blue]RESOLVED[/blue]  {display_slug}  unsubscribed"
        )

    # ── Order lifecycle notifications ─────────────────────────────────────────

    async def on_fill(
        self,
        event: OrderFill,
        state: OrderState | None,
    ) -> None:
        """Notify dashboard and Telegram of a fill."""
        await self._dashboard_on_fill(event, state)
        await self._telegram_on_fill(event, state)

    async def on_terminal(
        self,
        event: OrderTerminal,
        state: OrderState | None,
    ) -> None:
        """Notify dashboard of terminal order."""
        if not self.dashboard:
            return

        slug = state.intent.slug if state else "?"
        display = format_slug_with_est_time(slug) if slug != "?" else "?"
        reason = clean_reason(event.reason or event.status.value)
        self.dashboard.push_event(
            f"[red]{event.status.value}[/red]  {display}  {reason}"
        )

    # ── Tick/book event display ───────────────────────────────────────────────

    def on_tick_size_event(
        self,
        event: TickSizeChange,
        ctx: StrategyContext,
        proximity_str: str = "",
    ) -> None:
        """Display tick size change in dashboard."""
        if not self.dashboard:
            return

        if event.latency_ms is not None:
            self.dashboard.push_latency(event.latency_ms)

        display_slug = format_slug_with_est_time(event.slug)
        bp = ctx.best_prices.get(event.token_id, {})
        bid = bp.get("bid")
        price_tag = f"  bid={bid:.3f}" if bid is not None else ""
        lat_tag = f"  (lat: {event.latency_ms:.1f}ms)" if event.latency_ms is not None else ""

        self.dashboard.push_event(
            f"📊 TICK_SIZE  {display_slug}  "
            f"{event.old_tick_size} → {event.new_tick_size}{price_tag}{lat_tag}{proximity_str}"
        )

    def on_gabagool_intent(
        self,
        strategy_name: str,
        intent: OrderIntent,
    ) -> None:
        """Display gabagool-specific intent notification."""
        if not self.dashboard:
            return

        if strategy_name not in {"gabagool", "gabagool_dual"}:
            return

        display_slug = format_slug_with_est_time(intent.slug)
        self.dashboard.push_event(
            f"📊 [green]{strategy_name.upper()}[/green]  {display_slug}  "
            f"BUY {intent.token_id[:12]}… @ {intent.price:.4f} x {intent.size:.2f}"
        )

    def on_market_add(self, slug: str, is_stock: bool = False) -> None:
        """Display market subscription notification."""
        if not self.dashboard:
            return

        display = format_slug_with_est_time(slug)
        prefix = "📍 STOCK_ADD" if is_stock else "📍 MARKET_ADD"
        self.dashboard.push_event(f"{prefix}  {display}")

    # ── Internal helpers ──────────────────────────────────────────────────────

    async def _notify_submission_blocked(
        self,
        intent: OrderIntent,
        display_slug: str,
    ) -> None:
        """Handle notification when submission is blocked (dedup/risk)."""
        # Note: Would need access to order_manager stats to determine reason
        # For now, this is a placeholder — in full integration, pass stats in
        pass

    async def _notify_submission_success(
        self,
        intent: OrderIntent,
        state: OrderState,
        strategy: Strategy,
        display_slug: str,
    ) -> None:
        """Handle notification for successful submission."""
        if not self.dashboard:
            return

        timing = format_timing(state)

        if state.is_terminal:
            reason = clean_reason(state.rejection_reason or state.status.value)
            self.dashboard.push_event(
                f"🛑 [red]{state.status.value}[/red]  {display_slug}  {reason}{timing}"
            )
        else:
            self.dashboard.push_order_metrics(
                state.tick_to_order_ms,
                state.time_to_expiry_s,
                state.sign_ms,
                state.post_ms,
            )

            bid_str = f"{state.best_bid:.3f}" if state.best_bid is not None else "--"
            ask_str = f"{state.best_ask:.3f}" if state.best_ask is not None else "--"
            prox_str = format_proximity(
                state.spot_price,
                state.strike_price,
                state.proximity,
                state.spot_price_age_ms,
            )
            self.dashboard.push_event(
                f"📤 [green]SUBMITTED[/green]  {display_slug}  "
                f"{intent.side.value} {intent.price:.4f} x {intent.size:.2f}  "
                f"(bid={bid_str} ask={ask_str}){timing}{prox_str}"
            )

    async def _dashboard_on_fill(
        self,
        event: OrderFill,
        state: OrderState | None,
    ) -> None:
        """Push fill event to dashboard."""
        if not self.dashboard:
            return

        slug = state.intent.slug if state else "?"
        display = format_slug_with_est_time(slug) if slug != "?" else "?"
        label = "FILLED" if state and state.status == OrderStatus.FILLED else "PARTIAL"
        color = "green" if label == "FILLED" else "cyan"
        emoji = "🎯" if label == "FILLED" else "🌓"

        self.dashboard.push_event(
            f"{emoji} [{color}]{label}[/{color}]  {display}  "
            f"@ {event.fill_price:.4f} x {event.fill_size:.2f}"
        )

        if state:
            self.dashboard.record_filled_submission_source(
                event.order_id,
                getattr(state, "submission_source", "unknown"),
                is_final_fill=(label == "FILLED"),
            )

    async def _telegram_on_fill(
        self,
        event: OrderFill,
        state: OrderState | None,
    ) -> None:
        """Send fill notification to Telegram."""
        if not self.telegram or not self.telegram.enabled:
            return
        if not state:
            return

        slug = state.intent.slug
        display = format_slug_with_est_time(slug)
        thresh = state.intent.size * 0.99
        fs = state.filled_size
        eff = fs + event.fill_size
        label = "FILLED" if (fs >= thresh or eff >= thresh) else "PARTIAL"

        body = (
            f"📍 <b>Market:</b> <code>{display}</code>\n"
            f"💵 <b>Price:</b> ${event.fill_price:.4f}\n"
            f"📦 <b>Size:</b> {event.fill_size:.2f} shares"
        )
        color = "🟢" if label == "FILLED" else "🔵"

        msg = build_telegram_message(color, f"ORDER {label}", body, self.profile)
        await self.telegram.push_message(msg)

    async def notify_risk_blocked(
        self,
        display_slug: str,
        reason: str,
    ) -> None:
        """Notify when an order is blocked by risk manager."""
        if self.dashboard:
            self.dashboard.push_event(
                f"⚠️ [yellow]SKIP[/yellow]  {display_slug}  RISK: {reason}"
            )

        if self.telegram and self.telegram.enabled:
            body = (
                f"📍 <b>Market:</b> <code>{display_slug}</code>\n"
                f"🚫 <b>Reason:</b> <code>{reason}</code>"
            )
            msg = build_telegram_message("⚠️", "ORDER RISK BLOCKED", body, self.profile)
            await self.telegram.push_message(msg)

    async def notify_dedup_blocked(self, display_slug: str) -> None:
        """Notify when an order is blocked by dedup."""
        if self.dashboard:
            self.dashboard.push_event(
                f"🛡️ [yellow]SKIP[/yellow]  {display_slug}  DEDUP: already ordered this session"
            )

    async def notify_claim(
        self,
        title: str,
        balance: float | None,
        tx_hash: str,
    ) -> None:
        """Notify of successful claim."""
        if self.dashboard:
            bal_str = f"  [dim]bal=${balance:.2f}[/dim]" if balance is not None else ""
            self.dashboard.push_event(
                f"💎 [bold green]CLAIMED[/bold green]  {title}{bal_str}  tx={tx_hash[:10]}…"
            )

        if self.telegram and self.telegram.enabled:
            bal_tele = f"\n💰 <b>Balance: ${balance:.2f}</b>" if balance is not None else ""
            body = (
                f"📦 <b>Market:</b> <code>{title}</code>\n"
                f"🔗 <b>Tx:</b> <a href='https://polygonscan.com/tx/{tx_hash}'>{tx_hash[:10]}...</a>"
                f"{bal_tele}"
            )
            msg = build_telegram_message("🟢", "WINNINGS COLLECTED", body, self.profile)
            await self.telegram.push_message(msg)
