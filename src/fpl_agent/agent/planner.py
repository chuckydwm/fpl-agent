"""Compute a transfer plan for an entry: the work behind ``fpl plan``.

Shared by the CLI and the deadline brief, so the phone and the terminal
always show the same recommendation. Read-only: nothing here writes to FPL.

Every plan starts from one :class:`~fpl_agent.agent.squad_state.SquadState`:
a squad, bank, free transfers and selling prices that belong to the same
moment, with its source, timestamp, the later transfers it counted and any
uncertain fields attached. Freshness follows the client's policy: the
deadline job builds a client that refuses stale data, the CLI one that
accepts bounded stale data and labels it.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from fpl_agent.agent.squad_state import (
    SquadState,
    SquadStateError,
    load_snapshot,
    read_snapshot,
    reconstruct,
)
from fpl_agent.api.client import FPLClient
from fpl_agent.api.endpoints import (
    get_bootstrap,
    get_chip_events,
    get_entry_history,
    get_fixtures,
)
from fpl_agent.api.models import MAX_FREE_TRANSFERS, Bootstrap, Event
from fpl_agent.backtest.history import fetch_history
from fpl_agent.models.expected_points import ExpectedPoints, model_weight, project_squad
from fpl_agent.models.fixtures import build_fixture_runs
from fpl_agent.models.minutes import MinutesModel
from fpl_agent.optimiser.squad import OptimiserInput, TransferPlan, optimise

log = logging.getLogger(__name__)

#: Match history only changes after a match, so a few hours of cache is safe.
HISTORY_TTL = 6 * 3600


@dataclass(frozen=True, slots=True)
class PlanRun:
    plan: TransferPlan
    bootstrap: Bootstrap
    event: Event
    #: The squad state the plan starts from; ``None`` for a preseason build.
    state: SquadState | None
    settled: int
    #: How much of the projection is the model rather than FPL's ep_next.
    confidence: float
    #: True when no gameweek has locked yet, so the plan builds from scratch.
    fresh_build: bool
    projections: dict[tuple[int, int], ExpectedPoints] = field(default_factory=dict)
    #: Where the inputs came from and how old they are (FPLClient.freshness_note).
    freshness: str = ""
    #: True when any input was an old copy served after a failed fetch.
    stale: bool = False
    #: Flagged players the plan will not buy (a policy, stated in the output).
    not_bought: frozenset[int] = frozenset()

    @property
    def free_transfers(self) -> int:
        return self.state.free_transfers if self.state else 0

    @property
    def bank_tenths(self) -> int:
        return self.state.bank_tenths if self.state else 1000


async def squad_state(
    client: FPLClient,
    entry_id: int,
    bootstrap: Bootstrap,
    *,
    first_squad_price: dict[int, int],
    snapshot: Path | None = None,
) -> SquadState | None:
    """The state going into the next deadline, or ``None`` before any lock.

    A valid snapshot for the coming gameweek, newer than the last deadline,
    replaces the public reconstruction entirely."""
    nxt = bootstrap.next_event
    source = bootstrap.current_event or bootstrap.last_settled_event
    if nxt is None or source is None:
        return None

    raw = read_snapshot(snapshot)
    if raw is not None:
        state = load_snapshot(
            raw,
            entry_id=entry_id,
            for_event=nxt.id,
            last_deadline=source.deadline_time,
            elements=bootstrap.element_by_id(),
        )
        if state is not None:
            return state
        log.warning(
            "squad snapshot is for another gameweek or older than GW%s's deadline", source.id
        )

    picks = await client.get_json(f"/entry/{entry_id}/event/{source.id}/picks/")
    history = await get_entry_history(client, entry_id)
    chips = await get_chip_events(client, entry_id)
    transfers: list[dict[str, Any]] = await client.get_json(f"/entry/{entry_id}/transfers/")
    return reconstruct(
        source_event=source.id,
        source_deadline=source.deadline_time,
        for_event=nxt.id,
        picks=[int(p["element"]) for p in picks.get("picks", [])],
        source_bank=int(picks.get("entry_history", {}).get("bank", 0)),
        history=history,
        chips=chips,
        transfers=transfers,
        first_squad_price=first_squad_price,
        now_cost={e.id: e.now_cost for e in bootstrap.elements},
    )


async def run_plan(
    client: FPLClient,
    entry_id: int,
    *,
    horizon: int,
    max_transfers: int,
    decay: float,
    snapshot: Path | None = None,
) -> PlanRun | None:
    """The recommended plan for the next gameweek, or ``None`` if there is none.

    Raises :class:`SquadStateError` when the squad state cannot be trusted
    (an invalid snapshot, or public transfers that do not add up)."""
    bootstrap = await get_bootstrap(client)
    next_event = bootstrap.next_event
    if next_event is None:
        return None

    events = [e.id for e in bootstrap.events if next_event.id <= e.id < next_event.id + horizon]
    settled = [e for e in bootstrap.events if e.is_settled]
    fixtures = await get_fixtures(client)
    history_rows = await get_entry_history(client, entry_id)
    first_event = min((row.event for row in history_rows), default=next_event.id)

    # Match-level minutes for everyone who has played, plus the current squad.
    source = bootstrap.current_event or bootstrap.last_settled_event
    owned: set[int] = set()
    if source is not None:
        picks = await client.get_json(f"/entry/{entry_id}/event/{source.id}/picks/")
        owned = {int(p["element"]) for p in picks.get("picks", [])}
    pool = [e for e in bootstrap.elements if e.minutes > 0 or e.id in owned]
    store = await fetch_history(client, pool, ttl=HISTORY_TTL)
    first_squad_price: dict[int, int] = {}
    for row in sorted(store.rows, key=lambda r: r.round, reverse=True):
        if row.round >= first_event:
            first_squad_price[row.element] = row.value  # ends on the earliest round

    state = await squad_state(
        client, entry_id, bootstrap, first_squad_price=first_squad_price, snapshot=snapshot
    )
    free_transfers = state.free_transfers if state else 1
    if state is not None and state.chip == "freehit":
        # The coming week's squad is temporary and not planned: start the
        # plan from the week after, from the squad that comes back, which
        # keeps its banked transfers and gains the usual one.
        later = [e for e in events if e > next_event.id]
        if later:
            events = later
            free_transfers = min(MAX_FREE_TRANSFERS, free_transfers + 1)

    runs = build_fixture_runs(fixtures, bootstrap.team_by_id(), events=events)
    minutes_model = MinutesModel(
        gameweeks_played=len(settled), recent=dict(store.recent_appearances(next_event.id))
    )
    projections = project_squad(bootstrap.elements, runs, minutes_model, events)
    xp = {key: value.xp for key, value in projections.items()}
    # Policy: never buy a player flagged for the coming week. One already
    # owned may be kept and is projected as returning after that week.
    not_bought = frozenset(e.id for e in bootstrap.elements if not e.is_available)

    plan = optimise(
        OptimiserInput(
            elements=bootstrap.elements,
            xp=xp,
            events=events,
            current_squad=set(state.squad) if state else set(),
            bank_tenths=state.bank_tenths if state else 1000,
            free_transfers=free_transfers,
            decay=decay,
            banned=set(not_bought),
            max_transfers_per_event=max_transfers,
            sell_price_tenths=dict(state.selling_tenths) if state else {},
            unlimited_first_week=bool(state and state.unlimited_week),
        )
    )
    return PlanRun(
        plan=plan,
        bootstrap=bootstrap,
        event=next_event,
        state=state,
        settled=len(settled),
        confidence=model_weight(len(settled)),
        fresh_build=state is None,
        projections=projections,
        freshness=client.freshness_note(),
        stale=any(f.is_fallback for f in client.fetches.values()),
        not_bought=not_bought,
    )


__all__ = ["PlanRun", "SquadStateError", "run_plan", "squad_state"]
