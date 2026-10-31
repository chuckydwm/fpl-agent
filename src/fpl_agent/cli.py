"""Command line interface.

    fpl deadline                 # when is it, in your timezone
    fpl squad                    # your current picks, with bench order
    fpl plan --horizon 3         # recommended transfers, XI, captain, bench

None of these commands can modify your team.
"""

from __future__ import annotations

import asyncio
import logging
from datetime import UTC, datetime

import typer
from rich.console import Console
from rich.table import Table

from fpl_agent.agent.report import format_deadline, render_plan
from fpl_agent.api.client import FPLClient
from fpl_agent.api.endpoints import (
    free_transfers_after,
    get_bootstrap,
    get_chip_events,
    get_entry,
    get_entry_history,
    get_fixtures,
    get_picks,
)
from fpl_agent.backtest.history import fetch_history
from fpl_agent.backtest.metrics import compare, render_report
from fpl_agent.backtest.replay import replay
from fpl_agent.backtest.strategies import (
    HoldStrategy,
    OptimiserStrategy,
    Strategy,
    TemplateStrategy,
)
from fpl_agent.config import Settings
from fpl_agent.models.expected_points import model_weight, project_squad
from fpl_agent.models.fixtures import build_fixture_runs
from fpl_agent.models.minutes import MinutesModel
from fpl_agent.optimiser.squad import OptimiserInput, optimise

app = typer.Typer(add_completion=False, help="An FPL analyst that recommends, never executes.")
console = Console()


def _resolve_entry(settings: Settings, entry_id: int | None) -> int:
    resolved = entry_id or settings.entry_id
    if resolved is None:
        raise typer.BadParameter(
            "No entry id. Pass --entry-id or set FPL_ENTRY_ID in your .env "
            "(it is the number in your team URL)."
        )
    return resolved


@app.callback()
def _root(verbose: bool = typer.Option(False, "--verbose", "-v")) -> None:
    logging.basicConfig(
        level=logging.DEBUG if verbose else logging.WARNING,
        format="%(levelname)s %(name)s: %(message)s",
    )


@app.command()
def deadline() -> None:
    """Show the next deadline in your configured timezone."""

    async def run() -> None:
        settings = Settings.from_env()
        async with FPLClient(cache_ttl=settings.cache_ttl) as client:
            bootstrap = await get_bootstrap(client)
            event = bootstrap.next_event or bootstrap.current_event
            if event is None:
                console.print("[yellow]No upcoming gameweek — season over?[/yellow]")
                return
            console.print(format_deadline(event, settings.timezone))

    asyncio.run(run())


@app.command()
def squad(entry_id: int | None = typer.Option(None, "--entry-id")) -> None:
    """Print your current squad, including bench order."""

    async def run() -> None:
        settings = Settings.from_env()
        resolved = _resolve_entry(settings, entry_id)
        async with FPLClient(cache_ttl=settings.cache_ttl) as client:
            bootstrap = await get_bootstrap(client)
            entry = await get_entry(client, resolved)
            event = bootstrap.current_event or bootstrap.last_settled_event
            if event is None:
                console.print("[yellow]No gameweek has started yet.[/yellow]")
                return

            picks = await get_picks(client, resolved, event.id)
            elements = bootstrap.element_by_id()
            teams = bootstrap.team_by_id()

            table = Table(title=f"{entry.name} — GW{event.id}", header_style="bold")
            for column in ("Slot", "Pos", "Player", "Club", "£", "Role"):
                table.add_column(column)

            for pick in sorted(picks, key=lambda p: p.position):
                element = elements[pick.element]
                role = "C" if pick.is_captain else "VC" if pick.is_vice_captain else ""
                if not pick.is_starter:
                    order = pick.bench_order
                    role = "GK" if order is None else f"SUB {order}"
                table.add_row(
                    str(pick.position),
                    element.position.value,
                    element.web_name,
                    teams[element.team].short_name,
                    f"{element.price:.1f}",
                    role,
                )
            console.print(table)
            console.print(f"Bank £{entry.bank:.1f}m · Squad value £{entry.squad_value:.1f}m")

    asyncio.run(run())


@app.command()
def plan(
    entry_id: int | None = typer.Option(None, "--entry-id"),
    horizon: int | None = typer.Option(None, "--horizon", "-h"),
    max_transfers: int = typer.Option(2, "--max-transfers"),
) -> None:
    """Recommend transfers, starting XI, captain and bench order."""

    async def run() -> None:
        settings = Settings.from_env()
        resolved = _resolve_entry(settings, entry_id)
        window = horizon or settings.horizon

        async with FPLClient(cache_ttl=settings.cache_ttl) as client:
            bootstrap = await get_bootstrap(client)
            next_event = bootstrap.next_event
            if next_event is None:
                console.print("[yellow]No upcoming gameweek to plan for.[/yellow]")
                return

            if next_event.seconds_until_deadline() <= 0:
                console.print(
                    "[bold red]The deadline has already passed.[/bold red] "
                    "Your team is locked for this gameweek."
                )

            events = [
                e.id for e in bootstrap.events if next_event.id <= e.id < next_event.id + window
            ]

            settled = [e for e in bootstrap.events if e.is_settled]
            # Picks exist as soon as a gameweek locks, so an in-progress
            # gameweek is still the right source for "what do I own now".
            squad_event = bootstrap.current_event or bootstrap.last_settled_event

            fixtures = await get_fixtures(client)
            history = await get_entry_history(client, resolved)

            if squad_event is None:
                console.print(
                    "[yellow]No gameweek has locked yet — building a squad from scratch.[/yellow]"
                )
                current: set[int] = set()
                bank = 100.0
                free = 1
            else:
                picks = await get_picks(client, resolved, squad_event.id)
                current = {p.element for p in picks}
                entry = await get_entry(client, resolved)
                bank = entry.bank
                chips = await get_chip_events(client, resolved)
                free = free_transfers_after(history, chip_events=chips)

        runs = build_fixture_runs(fixtures, bootstrap.team_by_id(), events=events)
        minutes_model = MinutesModel(gameweeks_played=len(settled))
        projections = project_squad(bootstrap.elements, runs, minutes_model, events)

        confidence = model_weight(len(settled))
        if confidence < 1.0:
            console.print(
                f"[yellow]Only {len(settled)} settled gameweek(s) of data — projections are "
                f"blended {1 - confidence:.0%} toward FPL's own ep_next. Treat transfer "
                f"suggestions as directional until a few gameweeks have been played."
                f"[/yellow]"
            )
        xp = {key: value.xp for key, value in projections.items()}

        banned = {e.id for e in bootstrap.elements if not e.is_available}

        plan_result = optimise(
            OptimiserInput(
                elements=bootstrap.elements,
                xp=xp,
                events=events,
                current_squad=current,
                bank=bank,
                free_transfers=free,
                decay=settings.decay,
                banned=banned,
                max_transfers_per_event=max_transfers,
            )
        )

        console.print(
            f"[dim]Solver: {plan_result.status} · objective {plan_result.objective} · "
            f"{free} free transfer(s) · £{bank:.1f}m in the bank[/dim]"
        )
        render_plan(
            plan_result,
            bootstrap.element_by_id(),
            teams=bootstrap.team_by_id(),
            event=next_event,
            tz=settings.timezone,
            console=console,
        )

    asyncio.run(run())


@app.command()
def now() -> None:
    """Print the current time in UTC and your configured timezone.

    Trivial, and here on purpose: never infer the time from a rendered clock.
    """
    settings = Settings.from_env()
    utc = datetime.now(UTC)
    console.print(f"UTC   {utc:%Y-%m-%d %H:%M:%S}")
    local = utc.astimezone(settings.timezone)
    console.print(f"Local {local:%Y-%m-%d %H:%M:%S} {settings.timezone.key}")


if __name__ == "__main__":
    app()


@app.command()
def backtest(
    start: int = typer.Option(1, "--start", help="First gameweek to replay"),
    end: int | None = typer.Option(None, "--end", help="Last gameweek (default: last settled)"),
    horizon: int = typer.Option(1, "--horizon", help="Planning horizon inside the replay"),
    max_transfers: int = typer.Option(2, "--max-transfers"),
    limit: int | None = typer.Option(
        None, "--limit", help="Only consider the N most-owned players (faster)"
    ),
) -> None:
    """Walk forward through past gameweeks and score the optimiser against baselines.

    Player statistics and prices are rebuilt from per-fixture history before each
    gameweek. Club, ownership, team strengths and fixture rounds are today's
    values, so this is an approximate reconstruction, not a lookahead-free test;
    the report says which inputs are approximate.
    """

    async def run() -> None:
        settings = Settings.from_env()
        async with FPLClient(cache_ttl=settings.cache_ttl) as client:
            bootstrap = await get_bootstrap(client)
            fixtures = await get_fixtures(client)

            settled = [e for e in bootstrap.events if e.is_settled]
            if not settled:
                console.print(
                    "[yellow]No settled gameweeks yet — there is nothing to back-test.[/yellow]\n"
                    "[dim]A backtest needs completed gameweeks with final points. Come back "
                    "once a few have been played.[/dim]"
                )
                raise typer.Exit(code=0)

            last = end or max(e.id for e in settled)
            events = [e.id for e in settled if start <= e.id <= last]
            if not events:
                console.print(f"[yellow]No settled gameweeks in range {start}-{last}.[/yellow]")
                raise typer.Exit(code=0)

            pool = bootstrap.elements
            if limit:
                pool = sorted(pool, key=lambda e: e.ownership, reverse=True)[:limit]

            console.print(
                f"[dim]Fetching per-gameweek history for {len(pool)} players "
                f"(cached for 24h)…[/dim]"
            )
            store = await fetch_history(client, pool)
            if limit:
                store.provenance = store.provenance.with_note(
                    f"the pool is the {limit} most-owned players today, for every strategy"
                )

        teams = bootstrap.team_by_id()
        elements = {e.id: e for e in pool}

        strategies: list[Strategy] = [
            OptimiserStrategy(horizon=horizon, max_transfers=max_transfers),
            HoldStrategy(),
            TemplateStrategy(),
        ]

        reports = []
        for strategy in strategies:
            console.print(f"[dim]Replaying {strategy.name}…[/dim]")
            result = replay(store, strategy, events=events, fixtures=fixtures, teams=teams)
            reports.append(compare(result, store, elements))

        averages = {e.id: e.average_entry_score for e in settled if e.id in events}
        render_report(reports, average_scores=averages, console=console)

    asyncio.run(run())
