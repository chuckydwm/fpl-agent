"""Multi-gameweek transfer planner, as an integer linear program.

Maximise, over a horizon H:

    sum_g decay^g * [ sum_p xP[p,g] * (start[p,g] + captain[p,g]) - 4 * hits[g] ]

subject to squad composition, budget, the three-per-club rule, a legal
formation, and transfer/free-transfer accounting.

Money is integer tenths of a million throughout (``now_cost`` units), so the
budget never drifts on float rounding. Selling follows FPL's rule per
holding: a player owned at the start sells at his ``sell_price_tenths`` (the
purchase price plus half any rise); a player bought inside the horizon is
bought at today's price and, prices being held flat over the horizon, sells
back at that same price. Selling, rebuying and selling again therefore credits
the rebuy price the second time, not the original holding's.

The solver's answer is never trusted as-is. Every plan is re-checked by
:func:`validate_plan`, which recomputes the rules from the extracted squads
alone. A run stopped by the time limit returns only a validated incumbent,
labelled "feasible, not proven optimal"; no incumbent, an invalid one or a
fractional one raises :class:`SolverError`.

Known simplifications:

* **Chips** are not modelled. A wildcard cannot be expressed through
  ``free_transfers``: free transfers are capped at five.
* **Price changes** within the horizon are not forecast.
* **Vice-captain** is chosen after solving (the best projected starter other
  than the captain); the objective does not value captain insurance.
"""

from __future__ import annotations

import logging
import math
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

#: Tolerance for reading a binary or integer variable. Anything further from an
#: integer is a fractional solution and is rejected, never rounded.
INTEGRALITY_TOLERANCE = 1e-6


class SolverError(ValueError):
    """The solver produced no usable plan (none, infeasible, fractional or invalid)."""


@dataclass(slots=True)
class OptimiserInput:
    elements: list[Element]
    #: ``(element_id, event) -> expected points``
    xp: dict[tuple[int, int], float]
    events: list[int]
    current_squad: set[int]
    #: Money in the bank, in tenths of a million.
    bank_tenths: int
    free_transfers: int
    decay: float = 0.85
    #: Players that may not be bought (injured, suspended, personal blacklist).
    #: A banned player already in ``current_squad`` may be kept.
    banned: set[int] = field(default_factory=set)
    #: Players that must stay in the squad across the horizon.
    locked: set[int] = field(default_factory=set)
    max_transfers_per_event: int = 3
    solver_time_limit: int = 60
    #: ``element_id -> tenths`` FPL pays for each player in ``current_squad``:
    #: purchase price plus half any rise. Missing holdings sell at ``now_cost``.
    sell_price_tenths: dict[int, int] = field(default_factory=dict)


@dataclass(frozen=True, slots=True)
class GameweekPlan:
    event: int
    transfers_in: list[int]
    transfers_out: list[int]
    starting_xi: list[int]
    bench: list[int]
    captain: int
    vice_captain: int
    hits: int
    expected_points: float
    #: Free transfers available going into this gameweek.
    free_transfers: int = 0
    #: Bank after this gameweek's transfers, in tenths.
    bank_tenths: int = 0

    @property
    def squad(self) -> set[int]:
        return set(self.starting_xi) | set(self.bench)


@dataclass(frozen=True, slots=True)
class TransferPlan:
    gameweeks: list[GameweekPlan]
    objective: float
    status: str
    #: True only when the solver proved optimality. A validated incumbent from
    #: a time-limited run is usable but may leave points on the table.
    proven_optimal: bool = True

    @property
    def first(self) -> GameweekPlan:
        return self.gameweeks[0]

    @property
    def quality(self) -> str:
        return "optimal" if self.proven_optimal else "feasible, not proven optimal"


def optimise(spec: OptimiserInput) -> TransferPlan:
    """Solve the transfer plan. Raises :class:`SolverError` if no valid plan exists."""
    if not spec.events:
        raise ValueError("optimise() needs at least one gameweek in `events`")

    elements = [e for e in spec.elements if e.id not in spec.banned or e.id in spec.current_squad]
    ids = [e.id for e in elements]
    by_id = {e.id: e for e in elements}
    events = sorted(spec.events)

    missing = spec.current_squad - set(ids)
    if missing:
        raise ValueError(f"current squad references unknown element ids: {sorted(missing)}")

    too_high = {
        pid: price
        for pid, price in spec.sell_price_tenths.items()
        if pid in by_id and price > by_id[pid].now_cost
    }
    if too_high:
        raise ValueError(f"selling prices above today's price: {too_high}")

    # Building from nothing (preseason, or a wildcard) is not "15 transfers":
    # the first gameweek's selection is free and unconstrained by the per-week
    # transfer cap. Without this the model is trivially infeasible.
    initial_build = not spec.current_squad
    held = sorted(spec.current_squad)
    # Profit FPL keeps when an original holding is sold: today's price minus
    # its selling price (never negative; a fall is already in now_cost).
    kept_by_fpl = {
        pid: max(0, by_id[pid].now_cost - spec.sell_price_tenths.get(pid, by_id[pid].now_cost))
        for pid in held
    }

    prob = pulp.LpProblem("fpl_transfer_plan", pulp.LpMaximize)

    # ---- decision variables ------------------------------------------------
    squad = _binaries("squad", ids, events)
    start = _binaries("start", ids, events)
    cap = _binaries("cap", ids, events)
    buy = _binaries("buy", ids, events)
    sell = _binaries("sell", ids, events)
    # original[p][g]: still holding the copy of p owned at the start. It can
    # only drop when p is sold, so (original[g-1] - original[g]) marks the one
    # sale that is priced at the holding's selling price.
    original = _binaries("orig", held, events)

    hits = {g: pulp.LpVariable(f"hits_{g}", lowBound=0, cat="Integer") for g in events}
    # lowBound is 0, not 1: during an unlimited build week no free transfer is
    # consumed, and the following week must start from one. A floor of 1 is
    # reimposed for every normal gameweek below.
    free = {
        g: pulp.LpVariable(f"free_{g}", lowBound=0, upBound=MAX_FREE_TRANSFERS, cat="Integer")
        for g in events
    }
    used_free = {g: pulp.LpVariable(f"usedfree_{g}", lowBound=0, cat="Integer") for g in events}
    bank = {g: pulp.LpVariable(f"bank_{g}", lowBound=0, cat="Integer") for g in events}

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

        for pid in held:
            before = original[pid][events[index - 1]] if index else 1
            prob += original[pid][g] <= before, f"orig_mono_{pid}_{g}"
            prob += original[pid][g] >= before - sell[pid][g], f"orig_until_sold_{pid}_{g}"
            prob += original[pid][g] <= squad[pid][g], f"orig_owned_{pid}_{g}"

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

        # ---- budget (tenths) -----------------------------------------------
        spend = pulp.lpSum(by_id[pid].now_cost * buy[pid][g] for pid in ids)
        proceeds = pulp.lpSum(by_id[pid].now_cost * sell[pid][g] for pid in ids) - pulp.lpSum(
            kept_by_fpl[pid]
            * ((original[pid][events[index - 1]] if index else 1) - original[pid][g])
            for pid in held
            if kept_by_fpl[pid]
        )
        previous_bank = bank[events[index - 1]] if index else spec.bank_tenths
        prob += bank[g] == previous_bank + proceeds - spend, f"bank_{g}"

    # ---- solve -------------------------------------------------------------
    prob.solve(_solver(spec.solver_time_limit))
    status = pulp.LpStatus[prob.status]
    solution = getattr(prob, "sol_status", None)
    proven = prob.status == pulp.LpStatusOptimal and solution in (None, pulp.LpSolutionOptimal)
    usable = proven or solution == pulp.LpSolutionIntegerFeasible
    if not usable:
        label = pulp.LpSolution.get(solution, "unknown") if solution is not None else "unknown"
        raise SolverError(f"no usable plan: solver status {status!r}, solution {label!r}")

    # ---- extract (no rounding: a fractional value is a failure) -----------
    def on(var: pulp.LpVariable) -> bool:
        return _binary_value(var)

    buys = [sum(1 for pid in ids if on(buy[pid][g])) for g in events]
    ledger = transfer_ledger(initial_build, spec.free_transfers, buys)
    plans: list[GameweekPlan] = []
    for index, g in enumerate(events):
        free_now, rule_hits = ledger[index]
        if _integer_value(hits[g]) < rule_hits:
            raise SolverError(
                f"GW{g}: solver charged {_integer_value(hits[g])} hits, the rules give {rule_hits}"
            )
        chosen_xi = [pid for pid in ids if on(start[pid][g])]
        chosen_squad = [pid for pid in ids if on(squad[pid][g])]
        captains = [pid for pid in ids if on(cap[pid][g])]
        if len(captains) != 1 or len(chosen_xi) != XI_SIZE:
            raise SolverError(
                f"GW{g}: incomplete assignment ({len(chosen_xi)} starters, "
                f"{len(captains)} captains)"
            )
        captain = captains[0]
        bench_ids = [pid for pid in chosen_squad if pid not in set(chosen_xi)]
        vice = max(
            (pid for pid in chosen_xi if pid != captain),
            key=lambda pid: (spec.xp.get((pid, g), 0.0), -pid),
        )

        plans.append(
            GameweekPlan(
                event=g,
                transfers_in=sorted(pid for pid in ids if on(buy[pid][g])),
                transfers_out=sorted(pid for pid in ids if on(sell[pid][g])),
                starting_xi=sorted(
                    chosen_xi,
                    key=lambda p: (by_id[p].element_type, -spec.xp.get((p, g), 0.0)),
                ),
                bench=_bench_order(bench_ids, by_id, spec.xp, g),
                captain=captain,
                vice_captain=vice,
                hits=rule_hits,
                expected_points=round(
                    sum(spec.xp.get((pid, g), 0.0) for pid in chosen_xi)
                    + spec.xp.get((captain, g), 0.0),
                    2,
                ),
                free_transfers=free_now,
                bank_tenths=_integer_value(bank[g]),
            )
        )

    plan = TransferPlan(
        gameweeks=plans,
        objective=round(pulp.value(prob.objective) or 0.0, 3),
        status=status,
        proven_optimal=proven,
    )
    problems = validate_plan(spec, plan)
    if problems:
        raise SolverError("solver plan failed validation: " + "; ".join(problems))
    if not proven:
        log.warning("time limit reached: returning a validated plan, not proven optimal")
    return plan


def transfer_ledger(
    initial_build: bool, free_transfers: int, buys: list[int]
) -> list[tuple[int, int]]:
    """``(free transfers going in, hits)`` per gameweek under FPL's rules.

    The first week of a build is unlimited and banks nothing; every other week
    has at least one free transfer, unused ones roll over up to five, and each
    transfer beyond the free ones is a hit."""
    out: list[tuple[int, int]] = []
    free_next = min(free_transfers, MAX_FREE_TRANSFERS)
    for index, count in enumerate(buys):
        if initial_build and index == 0:
            out.append((0, 0))
            free_next = 1
            continue
        free_now = max(1, free_next)
        used = min(count, free_now)
        out.append((free_now, count - used))
        free_next = min(MAX_FREE_TRANSFERS, free_now - used + 1)
    return out


def validate_plan(spec: OptimiserInput, plan: TransferPlan) -> list[str]:
    """Every rule the plan must satisfy, recomputed from its squads alone.

    Independent of the solver's own variables: it replays the plan from
    ``spec.current_squad`` with FPL's transfer, free-transfer, hit and
    selling-price rules in integer tenths, and lists each breach. Empty means valid.
    """
    by_id = {e.id: e for e in spec.elements}
    problems: list[str] = []
    events = sorted(spec.events)
    if [gw.event for gw in plan.gameweeks] != events:
        return [f"plan covers {[gw.event for gw in plan.gameweeks]}, expected {events}"]

    owned = set(spec.current_squad)
    # What each held player sells for: original holdings at their selling
    # price, anything bought in the plan at the price paid (prices held flat).
    sale_value = {
        pid: spec.sell_price_tenths.get(pid, by_id[pid].now_cost) for pid in owned if pid in by_id
    }
    bank = spec.bank_tenths
    initial_build = not owned
    ledger = transfer_ledger(
        initial_build, spec.free_transfers, [len(gw.transfers_in) for gw in plan.gameweeks]
    )

    for index, gw in enumerate(plan.gameweeks):
        g = gw.event
        bought, sold = set(gw.transfers_in), set(gw.transfers_out)
        unknown = (bought | sold | gw.squad) - set(by_id)
        if unknown:
            problems.append(f"GW{g}: unknown players {sorted(unknown)}")
            return problems

        if bought & owned:
            problems.append(f"GW{g}: buys players already owned {sorted(bought & owned)}")
        if not sold <= owned:
            problems.append(f"GW{g}: sells players not owned {sorted(sold - owned)}")
        expected_squad = (owned - sold) | bought
        if gw.squad != expected_squad:
            problems.append(f"GW{g}: squad does not follow from the transfers")
        squad_list = list(gw.starting_xi) + list(gw.bench)
        if len(squad_list) != SQUAD_SIZE or len(set(squad_list)) != SQUAD_SIZE:
            problems.append(f"GW{g}: squad is {len(set(squad_list))} unique of {len(squad_list)}")
        for position, quota in SQUAD_QUOTA.items():
            count = sum(1 for p in gw.squad if by_id[p].position is position)
            if count != quota:
                problems.append(f"GW{g}: {count} {position.value}, need {quota}")
        clubs: dict[int, int] = {}
        for p in gw.squad:
            clubs[by_id[p].team] = clubs.get(by_id[p].team, 0) + 1
        over = {c: n for c, n in clubs.items() if n > MAX_PER_CLUB}
        if over:
            problems.append(f"GW{g}: club limit broken {over}")
        if len(gw.starting_xi) != XI_SIZE or not set(gw.starting_xi) <= gw.squad:
            problems.append(f"GW{g}: starting XI is not 11 squad players")
        for position, (lo, hi) in FORMATION_BOUNDS.items():
            count = sum(1 for p in gw.starting_xi if by_id[p].position is position)
            if not lo <= count <= hi:
                problems.append(f"GW{g}: {count} {position.value} starting, allowed {lo}-{hi}")
        if gw.captain not in gw.starting_xi:
            problems.append(f"GW{g}: captain is not starting")
        if gw.vice_captain not in gw.starting_xi or gw.vice_captain == gw.captain:
            problems.append(f"GW{g}: vice-captain must be a different starter")
        if bought & spec.banned:
            problems.append(f"GW{g}: buys banned players {sorted(bought & spec.banned)}")
        if not spec.locked <= gw.squad:
            problems.append(f"GW{g}: drops locked players {sorted(spec.locked - gw.squad)}")

        free_build = initial_build and index == 0
        limit = SQUAD_SIZE if free_build else spec.max_transfers_per_event
        if len(bought) > limit:
            problems.append(f"GW{g}: {len(bought)} transfers, limit {limit}")
        free_now, hits = ledger[index]
        if gw.hits != hits:
            problems.append(f"GW{g}: plan says {gw.hits} hits, the rules give {hits}")
        if gw.free_transfers != free_now:
            problems.append(
                f"GW{g}: plan says {gw.free_transfers} free transfers, the rules give {free_now}"
            )

        bank += sum(sale_value.pop(p) for p in sold) - sum(by_id[p].now_cost for p in bought)
        sale_value.update({p: by_id[p].now_cost for p in bought})
        if bank < 0:
            problems.append(f"GW{g}: bank goes to {bank} tenths")
        if gw.bank_tenths != bank:
            problems.append(f"GW{g}: plan says bank {gw.bank_tenths}, the rules give {bank}")
        owned = gw.squad
    return problems


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


def _integer_value(var: pulp.LpVariable) -> int:
    """The variable's integer value; a missing, non-finite or fractional value
    means the solution is unusable."""
    value = var.value()
    if value is None or not math.isfinite(value):
        raise SolverError(f"{var.name} has no value")
    nearest = round(value)
    if abs(value - nearest) > INTEGRALITY_TOLERANCE:
        raise SolverError(f"{var.name} = {value} is fractional")
    return int(nearest)


def _binary_value(var: pulp.LpVariable) -> bool:
    value = _integer_value(var)
    if value not in (0, 1):
        raise SolverError(f"{var.name} = {value} is not binary")
    return value == 1
