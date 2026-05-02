"""
src/monitor/dashboard.py
Live terminal dashboard using Rich library.
Shows real-time status, trades, and P&L.
"""

import time
from datetime import datetime
from typing import Optional

from rich.console import Console
from rich.layout import Layout
from rich.live import Live
from rich.panel import Panel
from rich.table import Table
from rich.text import Text
from rich import box

from src.models import Trade, PolyWindow, BTCPrice
from src.risk.manager import RiskManager
from config.settings import Settings


console = Console()


def make_header(settings: Settings) -> Panel:
    mode_color = "red" if settings.trading_mode == "live" else "green"
    mode_text = f"[bold {mode_color}]{'🔴 LIVE — REAL MONEY' if settings.trading_mode == 'live' else '🟢 PAPER MODE'}[/]"
    now = datetime.now().strftime("%H:%M:%S")
    return Panel(
        f"[bold white]POLYMARKET 5-MIN BTC BOT[/]  •  {mode_text}  •  [dim]{now}[/]",
        style="bold blue",
        box=box.DOUBLE_EDGE,
    )


def make_market_panel(
    window: Optional[PolyWindow],
    btc: Optional[BTCPrice],
    delta_pct: Optional[float],
) -> Panel:
    if window is None:
        content = "[dim]Searching for active market...[/]"
    else:
        secs = window.seconds_remaining
        bar_len = 20
        filled = max(0, int((1 - secs / 300) * bar_len))
        bar = "█" * filled + "░" * (bar_len - filled)
        color = "red" if secs < 30 else "yellow" if secs < 60 else "green"

        btc_str = f"${btc.price:,.2f}" if btc else "—"
        delta_str = ""
        if delta_pct is not None:
            sign = "+" if delta_pct >= 0 else ""
            color_d = "green" if delta_pct > 0 else "red" if delta_pct < 0 else "white"
            delta_str = f"[{color_d}]{sign}{delta_pct:.3f}%[/] (20s)"

        content = (
            f"[bold]BTC:[/] {btc_str}  {delta_str}\n"
            f"[bold]Window:[/] {window.slug}\n"
            f"[bold]Closes:[/] [{color}]{secs:.0f}s[/]  [{color}]{bar}[/]"
        )

    return Panel(content, title="[bold]Market[/]", border_style="cyan")


def make_stats_panel(risk: RiskManager, settings: Settings) -> Panel:
    stats = risk.get_all_time_stats()
    daily = risk.get_daily_stats()

    win_color = "green" if stats["win_rate"] >= 0.60 else "yellow" if stats["win_rate"] >= 0.50 else "red"
    pnl_color = "green" if stats["total_pnl"] >= 0 else "red"

    content = (
        f"[bold]Capital:[/]     ${stats['current_capital']:.2f}  "
        f"([dim]peak ${stats['peak_capital']:.2f}[/])\n"
        f"[bold]Daily P&L:[/]   [{pnl_color}]${daily.net_pnl:+.2f}[/]\n"
        f"[bold]All-Time P&L:[/][{pnl_color}]${stats['total_pnl']:+.2f}[/]\n"
        f"[bold]Win Rate:[/]    [{win_color}]{stats['win_rate']:.1%}[/]  "
        f"({stats['wins']}W / {stats['losses']}L)\n"
        f"[bold]Trades Today:[/]{daily.trades}/{settings.max_trades_per_day}\n"
        f"[bold]Fees Paid:[/]   ${stats['total_fees']:.2f}"
    )

    return Panel(content, title="[bold]Performance[/]", border_style="magenta")


def make_trades_table(trades: list[Trade]) -> Panel:
    table = Table(box=box.SIMPLE, show_header=True, header_style="bold dim")
    table.add_column("ID", style="dim", width=12)
    table.add_column("Side", width=6)
    table.add_column("Entry", justify="right", width=6)
    table.add_column("Size", justify="right", width=8)
    table.add_column("Result", width=10)
    table.add_column("PnL", justify="right", width=10)
    table.add_column("Time", width=8)

    recent = sorted(trades, key=lambda t: t.entry_time, reverse=True)[:10]

    for t in recent:
        side_color = "green" if t.side == "UP" else "red"
        result_color = {
            "WIN": "green", "LOSS": "red", "PENDING": "yellow", "CANCELLED": "dim"
        }.get(t.result, "white")
        pnl_color = "green" if t.pnl_usd >= 0 else "red"
        entry_time = datetime.fromtimestamp(t.entry_time).strftime("%H:%M:%S")

        table.add_row(
            t.id[-8:],
            f"[{side_color}]{t.side}[/]",
            f"{t.entry_price:.3f}",
            f"${t.size_usd:.2f}",
            f"[{result_color}]{t.result}[/]",
            f"[{pnl_color}]${t.pnl_usd:+.2f}[/]" if t.is_closed else "[yellow]...[/]",
            entry_time,
        )

    return Panel(table, title="[bold]Recent Trades[/]", border_style="blue")


class Dashboard:
    """Live-updating terminal dashboard."""

    def __init__(self, settings: Settings, risk: RiskManager) -> None:
        self._settings = settings
        self._risk = risk
        self._trades: list[Trade] = []
        self._window: Optional[PolyWindow] = None
        self._btc: Optional[BTCPrice] = None
        self._delta: Optional[float] = None
        self._live: Optional[Live] = None

    def update(
        self,
        window: Optional[PolyWindow] = None,
        btc: Optional[BTCPrice] = None,
        delta: Optional[float] = None,
        new_trade: Optional[Trade] = None,
    ) -> None:
        if window is not None:
            self._window = window
        if btc is not None:
            self._btc = btc
        if delta is not None:
            self._delta = delta
        if new_trade is not None:
            self._trades.append(new_trade)

        if self._live:
            self._live.update(self._render())

    def _render(self) -> Layout:
        layout = Layout()
        layout.split_column(
            Layout(make_header(self._settings), size=3),
            Layout(name="middle", size=8),
            Layout(make_trades_table(self._trades)),
        )
        layout["middle"].split_row(
            Layout(make_market_panel(self._window, self._btc, self._delta)),
            Layout(make_stats_panel(self._risk, self._settings)),
        )
        return layout

    def start(self) -> Live:
        self._live = Live(
            self._render(),
            console=console,
            refresh_per_second=2,
            screen=True,
        )
        return self._live
