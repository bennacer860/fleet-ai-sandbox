"""Rich panel builders for the cheap_side strategy dashboard."""

from __future__ import annotations

from typing import Any

from rich.panel import Panel
from rich.table import Table
from rich.text import Text


def _balance_bar(up_pct: float, width: int = 24) -> str:
    up_blocks = int(round(up_pct / 100 * width))
    up_blocks = max(0, min(width, up_blocks))
  dn_blocks = width - up_blocks
    return f"[green]{'█' * up_blocks}[/green][red]{'█' * dn_blocks}[/red]"


def build_cheap_side_panel(snapshot: dict[str, Any] | None) -> Panel:
    """Render portfolio balance, capital, P&L, and fill stats."""
    if not snapshot:
        return Panel("[dim]No cheap_side data[/dim]", title="CHEAP SIDE", border_style="magenta")

    up_pct = snapshot.get("up_pct", 0.0)
    dn_pct = snapshot.get("down_pct", 0.0)
    bar = _balance_bar(up_pct)

    pnl = snapshot.get("total_pnl", 0.0)
    pnl_color = "green" if pnl >= 0 else "red"
    roi = snapshot.get("roi_pct", 0.0)

    lines = [
        f"[bold]Balance[/bold]  Up {up_pct:.0f}% ({snapshot.get('up_markets', 0)})  "
        f"|  Down {dn_pct:.0f}% ({snapshot.get('down_markets', 0)})",
        bar,
        "",
        f"[bold]Capital[/bold]  "
        f"Initial ${snapshot.get('initial_capital', 0):,.0f}  |  "
        f"Deployed ${snapshot.get('deployed_capital', 0):,.0f}  |  "
        f"Available ${snapshot.get('available_capital', 0):,.0f}",
        "",
        f"[bold]Session[/bold]  "
        f"Markets {snapshot.get('markets_traded', 0)}  |  "
        f"W {snapshot.get('wins', 0)} / L {snapshot.get('losses', 0)}  |  "
        f"Win rate {snapshot.get('win_rate_pct', 0):.1f}%",
        f"[bold]P&L[/bold]  [{pnl_color}]${pnl:+,.2f} ({roi:+.1f}% ROI)[/{pnl_color}]  |  "
        f"Avg entry ${snapshot.get('avg_entry_price', 0):.3f}/sh",
        "",
        f"[bold]Fills[/bold]  "
        f"{snapshot.get('order_fills', 0)}/{snapshot.get('order_attempts', 0)} attempts "
        f"({snapshot.get('fill_rate_pct', 0):.1f}%)",
    ]

    recent = snapshot.get("recent_positions") or []
    if recent:
        lines.append("")
        lines.append("[bold]Recent[/bold]")
        for r in recent[:5]:
            slug_short = (r.get("slug") or "")[-28:]
            won = r.get("won")
            tag = "[green]W[/green]" if won else "[red]L[/red]"
            lines.append(
                f"  {tag} {slug_short}  {r.get('outcome', '?')}  "
                f"{r.get('shares', 0):.0f}sh @ ${r.get('avg_price', 0):.3f}  "
                f"pnl ${r.get('pnl', 0):+.2f}"
            )

    dry = " [DRY-RUN]" if snapshot.get("dry_run") else ""
    return Panel(
        "\n".join(lines),
        title=f"CHEAP SIDE{dry}",
        border_style="magenta",
    )


def build_cheap_side_summary_table(snapshot: dict[str, Any] | None) -> Table | None:
    """Optional compact table for middle row."""
    if not snapshot:
        return None
    table = Table(show_header=False, box=None, padding=(0, 1))
    table.add_column("k", style="dim")
    table.add_column("v")
    table.add_row("Up/Down", f"{snapshot.get('up_pct', 0):.0f}% / {snapshot.get('down_pct', 0):.0f}%")
    table.add_row("Capital", f"${snapshot.get('available_capital', 0):,.0f} free")
    table.add_row("P&L", f"${snapshot.get('total_pnl', 0):+,.2f}")
    return table
