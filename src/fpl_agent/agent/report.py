"""Render a transfer plan for a human.

The agent recommends; the manager executes. Nothing here writes to FPL.
"""

from __future__ import annotations

from datetime import datetime
from zoneinfo import ZoneInfo

from rich.console import Console
from rich.panel import Panel
from rich.table import Table

from fpl_agent.api.models import Element, Event, Team
from fpl_agent.optimiser.squad import TransferPlan

_POSITION_ORDER = {"GKP": 0, "DEF": 1, "MID": 2, "FWD": 3}


def format_deadline(event: Event, tz: ZoneInfo, now: datetime | None = None) -> str:
    """Deadline in the manager's own timezone, plus time remaining.

    UK evening deadlines land after midnight in eastern timezones, so the local
    weekday frequently differs from the UK one. Always show the local day.
    """
    local = event.deadline_time.astimezone(tz)
    remaining = event.seconds_until_deadline(now)
    if remaining <= 0:
        return f"{event.name} deadline PASSED ({local:%a %d %b %H:%M} {tz.key})"
    hours, minutes = divmod(int(remaining) // 60, 60)
    days, hours = divmod(hours, 24)
    parts = [f"{days}d" if days else "", f"{hours}h", f"{minutes}m"]
    return (
        f"{event.name} deadline {local:%a %d %b %H:%M} {tz.key} "
        f"— in {' '.join(p for p in parts if p)}"
    )


def render_plan(
    plan: TransferPlan,
    elements: dict[int, Element],
    *,
    teams: dict[int, Team] | None = None,
    event: Event | None = None,
    tz: ZoneInfo | None = None,
    console: Console | None = None,
) -> None:
    console = console or Console()
    tz = tz or ZoneInfo("Europe/London")

    def club(element: Element) -> str:
        if teams and element.team in teams:
            return teams[element.team].short_name
        return str(element.team)

    if event is not None:
        console.print(Panel(format_deadline(event, tz), style="bold"))

    for gw in plan.gameweeks:
        table = Table(
            title=f"Gameweek {gw.event} — {gw.expected_points:.1f} xP"
            + (f" (−{gw.hits * 4} hit)" if gw.hits else ""),
            header_style="bold",
        )
        table.add_column("")
        table.add_column("Player")
        table.add_column("Club", justify="left")
        table.add_column("£", justify="right")
        table.add_column("Role", justify="left")

        for pid in sorted(
            gw.starting_xi, key=lambda p: _POSITION_ORDER[elements[p].position.value]
        ):
            element = elements[pid]
            role = "CAPTAIN" if pid == gw.captain else ""
            table.add_row(
                element.position.value,
                element.web_name,
                club(element),
                f"{element.price:.1f}",
                role,
            )

        for order, pid in enumerate(gw.bench):
            element = elements[pid]
            label = "GK" if order == 0 and element.position.value == "GKP" else f"SUB {order}"
            table.add_row(
                f"[dim]{element.position.value}[/dim]",
                f"[dim]{element.web_name}[/dim]",
                f"[dim]{club(element)}[/dim]",
                f"[dim]{element.price:.1f}[/dim]",
                f"[dim]{label}[/dim]",
            )

        console.print(table)

        if gw.transfers_in or gw.transfers_out:
            moves = ", ".join(
                f"{elements[o].web_name} -> {elements[i].web_name}"
                for o, i in zip(gw.transfers_out, gw.transfers_in, strict=False)
            )
            console.print(f"  [bold]Transfers:[/bold] {moves}")
        else:
            console.print("  [dim]No transfer — roll it.[/dim]")

    console.print(
        Panel(
            "These are recommendations only. This tool has no write access to "
            "your FPL account — enter any changes yourself before the deadline.",
            style="dim",
        )
    )
