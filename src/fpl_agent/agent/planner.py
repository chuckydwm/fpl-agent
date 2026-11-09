"""Compute a transfer plan for an entry: the work behind ``fpl plan``.

Shared by the CLI and the deadline brief, so the phone and the terminal
always show the same recommendation. Read-only: nothing here writes to FPL.
"""

from __future__ import annotations

from dataclasses import dataclass

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
from fpl_agent.api.models import Bootstrap, Event
from fpl_agent.models.expected_points import model_weight, project_squad
from fpl_agent.models.fixtures import build_fixture_runs
from fpl_agent.models.minutes import MinutesModel
from fpl_agent.optimiser.squad import OptimiserInput, TransferPlan, optimise


@dataclass(frozen=True, slots=True)
class PlanRun:
    plan: TransferPlan
    bootstrap: Bootstrap
    event: Event
    free_transfers: int
    bank: float
    settled: int
    #: How much of the projection is the model rather than FPL's ep_next.
    confidence: float
    #: True when no gameweek has locked yet, so the plan builds from scratch.
    fresh_build: bool


async def run_plan(
    client: FPLClient,
    entry_id: int,
    *,
    horizon: int,
    max_transfers: int,
    decay: float,
) -> PlanRun | None:
    """The recommended plan for the next gameweek, or ``None`` if there is none."""
    bootstrap = await get_bootstrap(client)
    next_event = bootstrap.next_event
    if next_event is None:
        return None

    events = [e.id for e in bootstrap.events if next_event.id <= e.id < next_event.id + horizon]
    settled = [e for e in bootstrap.events if e.is_settled]
    # Picks exist as soon as a gameweek locks, so an in-progress
    # gameweek is still the right source for "what do I own now".
    squad_event = bootstrap.current_event or bootstrap.last_settled_event

    fixtures = await get_fixtures(client)
    history = await get_entry_history(client, entry_id)

    if squad_event is None:
        current: set[int] = set()
        bank = 100.0
        free = 1
    else:
        picks = await get_picks(client, entry_id, squad_event.id)
        current = {p.element for p in picks}
        entry = await get_entry(client, entry_id)
        bank = entry.bank
        chips = await get_chip_events(client, entry_id)
        free = free_transfers_after(history, chip_events=chips)

    runs = build_fixture_runs(fixtures, bootstrap.team_by_id(), events=events)
    minutes_model = MinutesModel(gameweeks_played=len(settled))
    projections = project_squad(bootstrap.elements, runs, minutes_model, events)
    xp = {key: value.xp for key, value in projections.items()}
    banned = {e.id for e in bootstrap.elements if not e.is_available}

    plan = optimise(
        OptimiserInput(
            elements=bootstrap.elements,
            xp=xp,
            events=events,
            current_squad=current,
            bank=bank,
            free_transfers=free,
            decay=decay,
            banned=banned,
            max_transfers_per_event=max_transfers,
        )
    )
    return PlanRun(
        plan=plan,
        bootstrap=bootstrap,
        event=next_event,
        free_transfers=free,
        bank=bank,
        settled=len(settled),
        confidence=model_weight(len(settled)),
        fresh_build=squad_event is None,
    )
