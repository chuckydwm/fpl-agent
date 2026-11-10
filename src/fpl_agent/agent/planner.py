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
from dataclasses import dataclass, field, replace
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
#: A plan must beat holding by at least this many expected points over its
#: horizon, after hits, before it is presented as more than marginal. The
#: model's error on one player's week is several points; this is a floor,
#: not a calibrated threshold (see the prediction evaluation).
MARGINAL_GAIN = 2.0


@dataclass(frozen=True, slots=True)
class HoldComparison:
    """The plan against keeping the same squad over the same horizon, both
    with lineups and captaincy optimised under the same projections."""

    events: tuple[int, ...]
    plan_points: float
    hold_points: float
    hits: int
    #: Transfers to make for the coming gameweek: (out, in).
    now: tuple[tuple[int, int], ...]
    #: Later transfers the plan expects, to be re-planned before acting.
    later: tuple[tuple[int, int, int], ...]
    #: Expected points per gameweek, plan minus hold, after hits.
    by_event: tuple[tuple[int, float], ...]
    uncertainties: tuple[str, ...] = ()

    @property
    def gain(self) -> float:
        return round(self.plan_points - self.hold_points, 2)

    @property
    def marginal(self) -> bool:
        return self.gain < MARGINAL_GAIN

    def describe(self, names: dict[int, str]) -> list[str]:
        span = f"GW{self.events[0]}" + (f"-{self.events[-1]}" if len(self.events) > 1 else "")
        verdict = (
            "marginal: within the model's error, not a confident move"
            if self.marginal
            else "worth acting on if the reasons hold"
        )
        lines = [
            f"Versus holding over {span}: {self.gain:+.1f} expected points after "
            f"{self.hits * 4} in hits ({self.plan_points:.1f} vs {self.hold_points:.1f}); "
            f"{verdict}."
        ]
        if self.now:
            moves = ", ".join(f"{names[o]} -> {names[i]}" for o, i in self.now)
            lines.append(f"Now (GW{self.events[0]}): {moves}.")
        else:
            lines.append(f"Now (GW{self.events[0]}): no transfer.")
        if self.later:
            moves = ", ".join(f"{names[o]} -> {names[i]} (GW{g})" for g, o, i in self.later)
            lines.append(f"Later, contingent (re-plan first): {moves}.")
        lines.extend(f"Uncertain: {u}" for u in self.uncertainties)
        return lines


def compare_to_hold(
    plan: TransferPlan,
    hold: TransferPlan,
    *,
    projections: dict[tuple[int, int], ExpectedPoints] | None = None,
    estimated: frozenset[int] = frozenset(),
    flagged: frozenset[int] = frozenset(),
) -> HoldComparison:
    """Undecayed expected points of each, after hits. Pure.

    Hits already committed before the plan starts are the same for both and
    left out. Uncertainties name what the gain rests on: moves involving a
    flagged player, minutes from the sparse-history fallback, estimated
    selling prices, and a plan or hold not proven optimal."""
    events = tuple(gw.event for gw in plan.gameweeks)
    if events != tuple(gw.event for gw in hold.gameweeks):
        raise ValueError("plan and hold must cover the same gameweeks")
    if any(gw.hits or gw.transfers_in for gw in hold.gameweeks):
        raise ValueError("the hold plan must make no transfers")
    by_event = tuple(
        (p.event, round(p.expected_points - 4 * p.hits - h.expected_points, 2))
        for p, h in zip(plan.gameweeks, hold.gameweeks, strict=True)
    )
    first = plan.first
    now = tuple(zip(first.transfers_out, first.transfers_in, strict=False))
    later = tuple(
        (gw.event, o, i)
        for gw in plan.gameweeks[1:]
        for o, i in zip(gw.transfers_out, gw.transfers_in, strict=False)
    )
    moved = {pid for out_id, in_id in now for pid in (out_id, in_id)}
    uncertainties: list[str] = []
    if moved & flagged:
        uncertainties.append("a move involves a player flagged for the coming week")
    if projections is not None:
        sparse = [
            pid
            for pid in sorted(moved)
            if "season average" in projections.get((pid, events[0]), _EMPTY).reason
        ]
        if sparse:
            uncertainties.append(f"{len(sparse)} player(s) in the move have too few recent matches")
    if moved & estimated:
        uncertainties.append("a selling price in the move is estimated")
    if not (plan.proven_optimal and hold.proven_optimal):
        uncertainties.append("the solver hit its time limit: feasible, not proven optimal")
    return HoldComparison(
        events=events,
        plan_points=round(sum(gw.expected_points - 4 * gw.hits for gw in plan.gameweeks), 2),
        hold_points=round(sum(gw.expected_points for gw in hold.gameweeks), 2),
        hits=sum(gw.hits for gw in plan.gameweeks),
        now=now,
        later=later,
        by_event=by_event,
        uncertainties=tuple(uncertainties),
    )


_EMPTY = ExpectedPoints(0, 0, 0.0, 0.0, "")


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
    #: The plan against holding the same squad; ``None`` for a fresh build.
    versus_hold: HoldComparison | None = None

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

    spec = OptimiserInput(
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
        unlimited_first_week=bool(state and state.unlimited_week and state.chip != "freehit"),
    )
    plan = optimise(spec)
    versus_hold = None
    if state is not None:
        # Same squad, projections, horizon and rules; transfers forbidden.
        hold = optimise(replace(spec, max_transfers_per_event=0, unlimited_first_week=False))
        versus_hold = compare_to_hold(
            plan,
            hold,
            projections=projections,
            estimated=state.estimated,
            flagged=not_bought,
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
        versus_hold=versus_hold,
    )


__all__ = [
    "MARGINAL_GAIN",
    "HoldComparison",
    "PlanRun",
    "SquadStateError",
    "compare_to_hold",
    "run_plan",
    "squad_state",
]
