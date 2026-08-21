"""Multi-gameweek transfer planner, as an integer linear program.

Maximise, over a horizon H:

    sum_g decay^g * [ sum_p xP[p,g] * (start[p,g] + captain[p,g]) - 4 * hits[g] ]

subject to squad composition, budget, the three-per-club rule, a legal
formation, and transfer/free-transfer accounting.

Known simplifications, all deliberate and all worth fixing before you trust
this with a hit:

* **Double gameweeks** collapse to their first fixture (see
  ``models/fixtures.py``), so DGW teams are undervalued.
* **Sell-on tax** is ignored: players are sold at purchase price. Real FPL
  charges 50% of profit rounded down. Needs per-player purchase price from
  ``/entry/{id}/transfers/``.
* **Chips** are not modelled. Wildcard is expressible by setting
  ``free_transfers`` very high for one gameweek.
* **Price changes** within the horizon are not forecast.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field

import pulp

from fpl_agent.api.models import (
    FORMATION_BOUNDS,
    HIT_COST,
    MAX_FREE_TRANSFERS,
    MAX_PER_CLUB,
    SQUAD_QUOTA,
    SQUAD_SIZE,
    XI_SIZE,
    Element,
    Position,
)

log = logging.getLogger(__name__)


@dataclass(slots=True)
class OptimiserInput:
    elements: list[Element]
    #: ``(element_id, event) -> expected points``
    xp: dict[tuple[int, int], float]
    events: list[int]
    current_squad: set[int]
    bank: float
    free_transfers: int
    decay: float = 0.85
    #: Players excluded outright (injured, suspended, personal blacklist).
    banned: set[int] = field(default_factory=set)
    #: Players that must stay in the squad across the horizon.
    locked: set[int] = field(default_factory=set)
    max_transfers_per_event: int = 3
    solver_time_limit: int = 60


@dataclass(frozen=True, slots=True)
class GameweekPlan:
    event: int
    transfers_in: list[int]
    transfers_out: list[int]
    starting_xi: list[int]
    bench: list[int]
    captain: int
    hits: int
    expected_points: float


@dataclass(frozen=True, slots=True)
class TransferPlan:
    gameweeks: list[GameweekPlan]
    objective: float
    status: str

    @property
    def first(self) -> GameweekPlan:
        return self.gameweeks[0]


def optimise(spec: OptimiserInput) -> TransferPlan:
    """Solve the transfer plan. Raises ``ValueError`` if no legal plan exists."""
    if not spec.events:
        raise ValueError("optimise() needs at least one gameweek in `events`")

    elements = [e for e in spec.elements if e.id not in spec.banned or e.id in spec.current_squad]
    ids = [e.id for e in elements]
    by_id = {e.id: e for e in elements}
    events = sorted(spec.events)

    missing = spec.current_squad - set(ids)
    if missing:
        raise ValueError(f"current squad references unknown element ids: {sorted(missing)}")

    # Building from nothing (preseason, or a wildcard) is not "15 transfers":
    # the first gameweek's selection is free and unconstrained by the per-week
    # transfer cap. Without this the model is trivially infeasible.
    initial_build = not spec.current_squad

    prob = pulp.LpProblem("fpl_transfer_plan", pulp.LpMaximize)

    # ---- decision variables ------------------------------------------------
    squad = _binaries("squad", ids, events)
    start = _binaries("start", ids, events)
    cap = _binaries("cap", ids, events)
    buy = _binaries("buy", ids, events)
    sell = _binaries("sell", ids, events)

    hits = {g: pulp.LpVariable(f"hits_{g}", lowBound=0, cat="Integer") for g in events}
    # lowBound is 0, not 1: during an unlimited build week (preseason or a
    # wildcard) no free transfer is consumed, and the following week must start
    # from one. A floor of 1 is reimposed for every normal gameweek below.
    free = {
        g: pulp.LpVariable(f"free_{g}", lowBound=0, upBound=MAX_FREE_TRANSFERS, cat="Integer")
        for g in events
    }
    used_free = {g: pulp.LpVariable(f"usedfree_{g}", lowBound=0, cat="Integer") for g in events}
    bank = {g: pulp.LpVariable(f"bank_{g}", lowBound=0, cat="Continuous") for g in events}

    # ---- objective ---------------------------------------------------------
    prob += pulp.lpSum(
        (spec.decay**index)
        * (
            pulp.lpSum(spec.xp.get((pid, g), 0.0) * (start[pid][g] + cap[pid][g]) for pid in ids)
            - HIT_COST * hits[g]
        )
        for index, g in enumerate(events)
    )

    # ---- per-gameweek constraints -----------------------------------------
    for index, g in enumerate(events):
        prob += pulp.lpSum(squad[pid][g] for pid in ids) == SQUAD_SIZE, f"squad_size_{g}"

        for position, quota in SQUAD_QUOTA.items():
            prob += (
                (
                    pulp.lpSum(squad[pid][g] for pid in ids if by_id[pid].position == position)
                    == quota
                ),
                f"quota_{position.value}_{g}",
            )

        prob += pulp.lpSum(start[pid][g] for pid in ids) == XI_SIZE, f"xi_size_{g}"

        for position, (lo, hi) in FORMATION_BOUNDS.items():
            in_position = [start[pid][g] for pid in ids if by_id[pid].position == position]
            prob += pulp.lpSum(in_position) >= lo, f"form_min_{position.value}_{g}"
            prob += pulp.lpSum(in_position) <= hi, f"form_max_{position.value}_{g}"

        for pid in ids:
            prob += start[pid][g] <= squad[pid][g], f"start_in_squad_{pid}_{g}"
            prob += cap[pid][g] <= start[pid][g], f"cap_starts_{pid}_{g}"

        prob += pulp.lpSum(cap[pid][g] for pid in ids) == 1, f"one_captain_{g}"

        for club in {e.team for e in elements}:
            prob += (
                (
                    pulp.lpSum(squad[pid][g] for pid in ids if by_id[pid].team == club)
                    <= MAX_PER_CLUB
                ),
                f"club_limit_{club}_{g}",
            )

        for pid in spec.locked:
            prob += squad[pid][g] == 1, f"locked_{pid}_{g}"

        # ---- transfer continuity ------------------------------------------
        for pid in ids:
            previous = squad[pid][events[index - 1]] if index else int(pid in spec.current_squad)
            prob += squad[pid][g] - previous == buy[pid][g] - sell[pid][g], f"flow_{pid}_{g}"
            prob += buy[pid][g] + sell[pid][g] <= 1, f"no_churn_{pid}_{g}"

        transfers = pulp.lpSum(buy[pid][g] for pid in ids)
        free_build = initial_build and index == 0
        cap_this_event = SQUAD_SIZE if free_build else spec.max_transfers_per_event
        prob += transfers <= cap_this_event, f"max_transfers_{g}"

        if free_build:
            # An unlimited build consumes no free transfers and incurs no hits,
            # so the usual hits >= transfers - used_free linkage does not apply.
            prob += hits[g] == 0, f"free_build_hits_{g}"
            prob += used_free[g] == 0, f"free_build_used_{g}"
            prob += free[g] == 0, f"free_build_free_{g}"
        else:
            prob += free[g] >= 1, f"free_floor_{g}"
            # used_free = min(free, transfers); the solver maximises it to dodge hits
            prob += used_free[g] <= free[g], f"usedfree_le_free_{g}"
            prob += used_free[g] <= transfers, f"usedfree_le_transfers_{g}"
            prob += hits[g] >= transfers - used_free[g], f"hits_def_{g}"

        if not free_build and index == 0:
            prob += free[g] == min(spec.free_transfers, MAX_FREE_TRANSFERS), f"free_init_{g}"
        elif index > 0:
            previous_event = events[index - 1]
            prob += (
                (free[g] <= free[previous_event] - used_free[previous_event] + 1),
                f"free_roll_{g}",
            )

        # ---- budget --------------------------------------------------------
        spend = pulp.lpSum(by_id[pid].price * buy[pid][g] for pid in ids)
        proceeds = pulp.lpSum(by_id[pid].price * sell[pid][g] for pid in ids)
        previous_bank = bank[events[index - 1]] if index else spec.bank
        prob += bank[g] == previous_bank + proceeds - spend, f"bank_{g}"

    # ---- solve -------------------------------------------------------------
    prob.solve(_solver(spec.solver_time_limit))
    status = pulp.LpStatus[prob.status]
    if status not in {"Optimal", "Not Solved"}:
        raise ValueError(f"optimiser returned status {status!r} — constraints may be infeasible")

    # ---- extract -----------------------------------------------------------
    plans: list[GameweekPlan] = []
    for g in events:
        chosen_xi = [pid for pid in ids if _on(start[pid][g])]
        chosen_squad = [pid for pid in ids if _on(squad[pid][g])]
        captain = next((pid for pid in ids if _on(cap[pid][g])), chosen_xi[0])
        bench_ids = [pid for pid in chosen_squad if pid not in set(chosen_xi)]

        plans.append(
            GameweekPlan(
                event=g,
                transfers_in=sorted(pid for pid in ids if _on(buy[pid][g])),
                transfers_out=sorted(pid for pid in ids if _on(sell[pid][g])),
                starting_xi=sorted(
                    chosen_xi,
                    key=lambda p: (by_id[p].element_type, -spec.xp.get((p, g), 0.0)),
                ),
                bench=_bench_order(bench_ids, by_id, spec.xp, g),
                captain=captain,
                hits=int(round(pulp.value(hits[g]) or 0)),
                expected_points=round(
                    sum(spec.xp.get((pid, g), 0.0) for pid in chosen_xi)
                    + spec.xp.get((captain, g), 0.0),
                    2,
                ),
            )
        )

    return TransferPlan(
        gameweeks=plans, objective=round(pulp.value(prob.objective) or 0.0, 3), status=status
    )


def _bench_order(
    bench_ids: list[int],
    by_id: dict[int, Element],
    xp: dict[tuple[int, int], float],
    event: int,
) -> list[int]:
    """Order the bench for auto-subs: reserve keeper first, then by xP.

    FPL substitutes in slot order, so the most likely returner should be sub 1.
    This is the single easiest gameweek to lose points in and the easiest to
    get right.
    """
    keepers = [pid for pid in bench_ids if by_id[pid].position is Position.GKP]
    outfield = sorted(
        (pid for pid in bench_ids if by_id[pid].position is not Position.GKP),
        key=lambda pid: xp.get((pid, event), 0.0),
        reverse=True,
    )
    return keepers + outfield


def _solver(time_limit: int) -> pulp.LpSolver:
    """Pick a CBC solver that works across PuLP versions.

    ``PULP_CBC_CMD`` is deprecated in favour of ``COIN_CMD`` in PuLP 4.0, but
    ``COIN_CMD`` needs a CBC binary on PATH (``pip install "pulp[cbc]"``).
    Prefer it when it is actually available, and fall back otherwise.
    """
    coin = getattr(pulp, "COIN_CMD", None)
    if coin is not None:
        candidate = coin(msg=False, timeLimit=time_limit)
        if candidate.available():
            return candidate
    return pulp.PULP_CBC_CMD(msg=False, timeLimit=time_limit)


def _binaries(
    name: str, ids: list[int], events: list[int]
) -> dict[int, dict[int, pulp.LpVariable]]:
    return {
        pid: {g: pulp.LpVariable(f"{name}_{pid}_{g}", cat="Binary") for g in events} for pid in ids
    }


def _on(var: pulp.LpVariable) -> bool:
    value = var.value()
    return value is not None and value > 0.5
