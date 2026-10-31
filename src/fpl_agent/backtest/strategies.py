"""Squad-selection strategies to replay against each other.

A backtest that only measures the optimiser against itself proves nothing. Each
strategy here answers the same question — "given the reconstructed pool before
this gameweek, what is your squad?" — so their scores are directly comparable.
"""

from __future__ import annotations

from dataclasses import dataclass, field, replace
from typing import Protocol

from fpl_agent.api.models import SQUAD_SIZE, Element, Position
from fpl_agent.models.expected_points import project_squad
from fpl_agent.models.fixtures import FixtureRun
from fpl_agent.models.minutes import MinutesModel
from fpl_agent.optimiser.squad import GameweekPlan, OptimiserInput, optimise


@dataclass(frozen=True, slots=True)
class Decision:
    """What a strategy wants to field in a gameweek."""

    squad: set[int]
    starting_xi: list[int]
    bench: list[int]
    captain: int
    transfers_in: list[int]
    transfers_out: list[int]
    hits: int


class Strategy(Protocol):
    """Decide a squad for one gameweek from the pool reconstructed before it."""

    name: str
    #: Gameweeks of fixtures the strategy plans over, starting at the current one.
    #: The replay builds fixture coverage for exactly this many.
    horizon: int

    def decide(
        self,
        *,
        event: int,
        pool: list[Element],
        runs: dict[tuple[int, int], FixtureRun],
        current_squad: set[int],
        bank: float,
        free_transfers: int,
        gameweeks_played: int,
    ) -> Decision: ...


def _legal_xi(squad: list[int], by_id: dict[int, Element], score: dict[int, float]) -> Decision:
    """Pick the best legal XI from a squad, then order the bench for auto-subs.

    Tries every legal formation and keeps the highest-scoring one. With four
    formations and a 15-man squad this is trivially cheap and avoids the classic
    bug of assuming a fixed shape.

    Selection only: the returned decision carries **no transfers and no hits**.
    A caller that also changed the squad must restore its own transfer
    accounting (see :class:`TemplateStrategy`).
    """
    keepers = sorted(
        (p for p in squad if by_id[p].position is Position.GKP),
        key=lambda p: score.get(p, 0.0),
        reverse=True,
    )
    outfield_by_position = {
        position: sorted(
            (p for p in squad if by_id[p].position is position),
            key=lambda p: score.get(p, 0.0),
            reverse=True,
        )
        for position in (Position.DEF, Position.MID, Position.FWD)
    }

    best: tuple[float, list[int]] | None = None
    for defenders in range(3, 6):
        for midfielders in range(2, 6):
            forwards = 10 - defenders - midfielders
            if not 1 <= forwards <= 3:
                continue
            picks = (
                keepers[:1]
                + outfield_by_position[Position.DEF][:defenders]
                + outfield_by_position[Position.MID][:midfielders]
                + outfield_by_position[Position.FWD][:forwards]
            )
            if len(picks) != 11:
                continue
            total = sum(score.get(p, 0.0) for p in picks)
            if best is None or total > best[0]:
                best = (total, picks)

    assert best is not None, "no legal formation from a valid 15-man squad"
    xi = best[1]
    bench_ids = [p for p in squad if p not in set(xi)]
    bench = [p for p in bench_ids if by_id[p].position is Position.GKP] + sorted(
        (p for p in bench_ids if by_id[p].position is not Position.GKP),
        key=lambda p: score.get(p, 0.0),
        reverse=True,
    )
    captain = max(xi, key=lambda p: score.get(p, 0.0))
    return Decision(set(squad), xi, bench, captain, [], [], 0)


@dataclass
class OptimiserStrategy:
    """The system under test: project, then solve the integer program."""

    name: str = "optimiser"
    horizon: int = 1
    max_transfers: int = 2
    decay: float = 0.85

    def decide(
        self,
        *,
        event: int,
        pool: list[Element],
        runs: dict[tuple[int, int], FixtureRun],
        current_squad: set[int],
        bank: float,
        free_transfers: int,
        gameweeks_played: int,
    ) -> Decision:
        events = [event + offset for offset in range(self.horizon)]
        minutes_model = MinutesModel(gameweeks_played=gameweeks_played)
        projections = project_squad(pool, runs, minutes_model, events)
        xp = {key: value.xp for key, value in projections.items()}

        plan = optimise(
            OptimiserInput(
                elements=pool,
                xp=xp,
                events=events,
                current_squad=current_squad,
                bank=bank,
                free_transfers=free_transfers,
                decay=self.decay,
                max_transfers_per_event=self.max_transfers,
            )
        )
        return _from_plan(plan.first)


def _from_plan(gw: GameweekPlan) -> Decision:
    return Decision(
        squad=set(gw.starting_xi) | set(gw.bench),
        starting_xi=gw.starting_xi,
        bench=gw.bench,
        captain=gw.captain,
        transfers_in=gw.transfers_in,
        transfers_out=gw.transfers_out,
        hits=gw.hits,
    )


@dataclass
class HoldStrategy:
    """Build once, then never transfer again.

    The honest floor. Any strategy that cannot beat doing nothing is costing
    you points through churn.
    """

    name: str = "hold"
    horizon: int = 1
    _initial: OptimiserStrategy = field(default_factory=lambda: OptimiserStrategy(horizon=1))

    def decide(
        self,
        *,
        event: int,
        pool: list[Element],
        runs: dict[tuple[int, int], FixtureRun],
        current_squad: set[int],
        bank: float,
        free_transfers: int,
        gameweeks_played: int,
    ) -> Decision:
        if not current_squad:
            return self._initial.decide(
                event=event,
                pool=pool,
                runs=runs,
                current_squad=current_squad,
                bank=bank,
                free_transfers=free_transfers,
                gameweeks_played=gameweeks_played,
            )
        by_id = {e.id: e for e in pool if e.id in current_squad}
        minutes_model = MinutesModel(gameweeks_played=gameweeks_played)
        projections = project_squad(list(by_id.values()), runs, minutes_model, [event])
        score = {pid: projections[(pid, event)].xp for pid in by_id}
        return _legal_xi(sorted(current_squad), by_id, score)


@dataclass
class TemplateStrategy:
    """Own the most-selected legal squad you can afford.

    Approximates the crowd. Beating it is what actually moves your rank.

    Built with the same integer program as the optimiser, but maximising
    ownership instead of expected points. Reusing the solver rather than a
    greedy fill guarantees a legal squad whenever one exists — a greedy pass
    has to guess how much budget to reserve, and guesses wrong on any pool
    whose price floor differs from the live game's.
    """

    name: str = "template"
    horizon: int = 1

    def decide(
        self,
        *,
        event: int,
        pool: list[Element],
        runs: dict[tuple[int, int], FixtureRun],
        current_squad: set[int],
        bank: float,
        free_transfers: int,
        gameweeks_played: int,
    ) -> Decision:
        by_id = {e.id: e for e in pool}
        ownership = {(e.id, event): e.ownership for e in pool}

        plan = optimise(
            OptimiserInput(
                elements=pool,
                xp=ownership,
                events=[event],
                current_squad=current_squad,
                bank=bank,
                free_transfers=free_transfers,
                max_transfers_per_event=SQUAD_SIZE,
            )
        )
        decision = _from_plan(plan.first)
        # Re-derive the XI by ownership so the captain is the template captain,
        # but keep the solver's transfers, hits and therefore bank accounting:
        # _legal_xi only chooses who starts.
        flat = {e.id: e.ownership for e in pool}
        selection = _legal_xi(sorted(decision.squad), by_id, flat)
        return replace(
            decision,
            starting_xi=selection.starting_xi,
            bench=selection.bench,
            captain=selection.captain,
        )
