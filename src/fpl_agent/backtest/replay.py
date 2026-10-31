"""Walk-forward replay engine.

For each gameweek in turn:

1. Reconstruct the player pool as it stood **before** that gameweek.
2. Ask the strategy for a squad, using only that snapshot.
3. Score it against what actually happened, applying auto-substitutions.
4. Carry the squad, bank and free transfers forward.

Step 1 is the whole game. Statistics always come from
:meth:`~fpl_agent.backtest.history.HistoryStore.snapshot_before`.

Transfer accounting is the replay's job, not the strategy's. Transfers are
derived from the change in squad, hits from FPL's rule (each transfer beyond
the free ones costs four points), and bank and free transfers move with them.
The first squad of a replay is a free build — like GW1 or a wildcard, it costs
no free transfers and no hits — and the next gameweek starts with one free
transfer, whatever gameweek the replay began at.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field

from fpl_agent.api.models import (
    FORMATION_BOUNDS,
    HIT_COST,
    MAX_FREE_TRANSFERS,
    Element,
    Fixture,
    Position,
    Team,
)
from fpl_agent.backtest.history import HistoryStore
from fpl_agent.backtest.strategies import Decision, Strategy
from fpl_agent.models.fixtures import build_fixture_runs

log = logging.getLogger(__name__)


@dataclass(frozen=True, slots=True)
class GameweekResult:
    event: int
    points: int
    #: Who was actually owned this gameweek. Needed for counterfactuals such as
    #: "the best captain available *from your own squad*".
    squad: frozenset[int]
    starting_xi: tuple[int, ...]
    hits: int
    captain: int
    captain_points: int
    bench_points: int
    auto_subs: list[tuple[int, int]]
    #: Players bought this gameweek. 0 for the initial build, which is not a
    #: transfer.
    transfers: int
    squad_value: float
    #: Bank *after* this gameweek's transfers (or build).
    bank: float
    #: Free transfers available going into this gameweek's decision.
    free_transfers: int = 0
    initial_build: bool = False

    @property
    def net_points(self) -> int:
        return self.points - self.hits * HIT_COST


@dataclass(frozen=True, slots=True)
class BacktestResult:
    strategy: str
    gameweeks: list[GameweekResult]

    @property
    def total_points(self) -> int:
        return sum(gw.net_points for gw in self.gameweeks)

    @property
    def total_hits(self) -> int:
        return sum(gw.hits for gw in self.gameweeks)

    @property
    def total_bench_points(self) -> int:
        return sum(gw.bench_points for gw in self.gameweeks)

    @property
    def points_per_gameweek(self) -> float:
        return self.total_points / len(self.gameweeks) if self.gameweeks else 0.0


@dataclass(slots=True)
class _State:
    squad: set[int] = field(default_factory=set)
    bank: float = 100.0
    #: 0 until the first squad is built: the build banks nothing, and the week
    #: after it starts with one (the same rule as ``free_transfers_after``).
    free_transfers: int = 0


def _apply_auto_subs(
    starting_xi: list[int],
    bench: list[int],
    by_id: dict[int, Element],
    minutes: dict[int, int],
) -> tuple[list[int], list[tuple[int, int]]]:
    """Substitute non-playing starters, respecting formation legality.

    FPL substitutes in bench order and only when the resulting formation is
    still legal — which is exactly why bench *order* is worth getting right.
    """
    final = list(starting_xi)
    subs: list[tuple[int, int]] = []
    available = [p for p in bench if minutes.get(p, 0) > 0]

    for starter in list(final):
        if minutes.get(starter, 0) > 0:
            continue
        starter_position = by_id[starter].position

        for candidate in list(available):
            candidate_position = by_id[candidate].position
            # A keeper can only be replaced by a keeper, and vice versa.
            if (starter_position is Position.GKP) != (candidate_position is Position.GKP):
                continue
            trial = [p for p in final if p != starter] + [candidate]
            if _formation_is_legal(trial, by_id):
                final = trial
                available.remove(candidate)
                subs.append((starter, candidate))
                break

    return final, subs


def _formation_is_legal(xi: list[int], by_id: dict[int, Element]) -> bool:
    if len(xi) != 11:
        return False
    for position, (low, high) in FORMATION_BOUNDS.items():
        count = sum(1 for p in xi if by_id[p].position is position)
        if not low <= count <= high:
            return False
    return True


def replay(
    store: HistoryStore,
    strategy: Strategy,
    *,
    events: list[int],
    fixtures: list[Fixture],
    teams: dict[int, Team],
    starting_bank: float = 100.0,
) -> BacktestResult:
    """Replay ``strategy`` over ``events`` and score it on real outcomes."""
    state = _State(bank=starting_bank)
    results: list[GameweekResult] = []
    horizon = max(1, int(strategy.horizon))

    for event in events:
        pool = store.snapshot_before(event)
        by_id = {e.id: e for e in pool}

        # Cover the strategy's whole planning horizon.
        horizon_events = [event + offset for offset in range(horizon)]
        runs = build_fixture_runs(fixtures, teams, events=horizon_events)

        initial_build = not state.squad
        decision = strategy.decide(
            event=event,
            pool=pool,
            runs=runs,
            current_squad=set(state.squad),
            bank=state.bank,
            free_transfers=state.free_transfers,
            # How much history exists, not where this round sits in the range:
            # a replay starting at GW5 still has four weeks of data behind it.
            gameweeks_played=len(store.events_before(event)),
        )

        bought, sold = _transfers(state.squad, decision, strategy.name, event)
        if initial_build:
            hits = 0
        else:
            hits = max(0, len(bought) - state.free_transfers)
            if decision.hits != hits:
                log.warning(
                    "%s reported %d hits in GW%d; charging %d from %d transfers and %d free",
                    strategy.name,
                    decision.hits,
                    event,
                    hits,
                    len(bought),
                    state.free_transfers,
                )

        spent = sum(by_id[pid].price for pid in bought)
        raised = sum(by_id[pid].price for pid in sold)
        bank_after = round(state.bank + raised - spent, 1)
        if bank_after < 0:
            raise ValueError(
                f"{strategy.name} overspent in GW{event}: bank {state.bank:.1f} "
                f"+ {raised:.1f} sold - {spent:.1f} bought = {bank_after:.1f}"
            )

        minutes = {pid: store.actual_minutes(pid, event) for pid in decision.squad}
        final_xi, subs = _apply_auto_subs(decision.starting_xi, decision.bench, by_id, minutes)

        points = sum(store.actual_points(pid, event) for pid in final_xi)
        captain_points = store.actual_points(decision.captain, event)
        if decision.captain in final_xi:
            points += captain_points  # doubled

        bench_points = sum(
            store.actual_points(pid, event) for pid in decision.bench if pid not in set(final_xi)
        )

        squad_value = sum(store.price_at(pid, event) or by_id[pid].price for pid in decision.squad)

        results.append(
            GameweekResult(
                event=event,
                points=points,
                squad=frozenset(decision.squad),
                starting_xi=tuple(final_xi),
                hits=hits,
                captain=decision.captain,
                captain_points=captain_points,
                bench_points=bench_points,
                auto_subs=subs,
                transfers=0 if initial_build else len(bought),
                squad_value=round(squad_value, 1),
                bank=bank_after,
                free_transfers=state.free_transfers,
                initial_build=initial_build,
            )
        )

        # ---- carry state forward -------------------------------------------
        state.bank = bank_after
        state.squad = set(decision.squad)
        used_free = 0 if initial_build else min(len(bought), state.free_transfers)
        state.free_transfers = min(MAX_FREE_TRANSFERS, state.free_transfers - used_free + 1)

    return BacktestResult(strategy=strategy.name, gameweeks=results)


def _transfers(
    current: set[int], decision: Decision, name: str, event: int
) -> tuple[list[int], list[int]]:
    """Transfers as the squad change, checked against what the strategy reported.

    A strategy that changes its squad but reports no transfers would otherwise
    get its new players for free; that is exactly the template bug this guards.
    """
    bought = sorted(decision.squad - current)
    sold = sorted(current - decision.squad)
    reported = (sorted(decision.transfers_in), sorted(decision.transfers_out))
    if current and reported != (bought, sold):
        raise ValueError(
            f"{name} GW{event}: reported transfers in {sorted(decision.transfers_in)} "
            f"out {sorted(decision.transfers_out)}, but the squad changed by "
            f"in {bought} out {sold}"
        )
    return bought, sold
