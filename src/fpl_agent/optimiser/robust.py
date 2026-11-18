"""How much a plan depends on the exact projections.

A deterministic solve treats every expected-points figure as exact. They are
not: a 0.3-point edge between two transfers is well inside the model's error.
``robustness`` re-solves the same problem with each player's projections
scaled by random noise and counts how often each coming-week decision comes
back. A move that survives most perturbed solves is a real edge; one that
appears in a minority is a coin flip the base plan happened to land on.

The noise is one factor per player across the whole horizon (a player the
model over-rates is over-rated every week), drawn as ``max(0, 1 + N(0, noise))``.
Every perturbed plan goes through ``optimise``, so it is validated like any
other; a perturbed solve that fails is counted, not hidden.
"""

from __future__ import annotations

import random
from collections import Counter
from dataclasses import dataclass, field, replace

from fpl_agent.optimiser.squad import OptimiserInput, SolverError, TransferPlan, optimise

#: Relative spread of the per-player noise. FPL projections typically miss by
#: a third or more per gameweek; 0.25 per player over the horizon is a
#: deliberately moderate default.
DEFAULT_NOISE = 0.25
DEFAULT_SAMPLES = 20

#: Each perturbed solve is a full MIP; keep any one of them short.
SAMPLE_TIME_LIMIT = 20

#: The first-week moves as a sorted (out, in) signature; empty means "hold".
Moves = tuple[tuple[int, ...], tuple[int, ...]]


def first_week_moves(plan: TransferPlan) -> Moves:
    first = plan.first
    return (tuple(sorted(first.transfers_out)), tuple(sorted(first.transfers_in)))


@dataclass(frozen=True, slots=True)
class Robustness:
    samples: int
    failed: int
    noise: float
    #: How often each set of coming-week moves came back.
    moves: Counter[Moves] = field(default_factory=Counter)
    #: How often each player was captain in the coming week.
    captains: Counter[int] = field(default_factory=Counter)
    #: How often each player was bought in the coming week (any move set).
    buys: Counter[int] = field(default_factory=Counter)

    @property
    def solved(self) -> int:
        return self.samples - self.failed

    def share(self, count: int) -> float:
        return count / self.solved if self.solved else 0.0

    def describe(self, base: TransferPlan, names: dict[int, str]) -> list[str]:
        """Lines for the CLI: the base plan's support first, then the alternatives."""

        def label(moves: Moves) -> str:
            out, into = moves
            if not out and not into:
                return "hold"
            return (
                f"out {', '.join(str(names.get(o, o)) for o in out)}; "
                f"in {', '.join(str(names.get(i, i)) for i in into)}"
            )

        if not self.solved:
            return [f"Robustness: all {self.samples} perturbed solves failed."]
        base_moves = first_week_moves(base)
        lines = [
            f"Robustness over {self.solved} perturbed solves (±{self.noise:.0%} per player"
            + (f", {self.failed} failed" if self.failed else "")
            + "):",
            f"  this plan's moves ({label(base_moves)}): {self.share(self.moves[base_moves]):.0%}",
        ]
        for moves, count in self.moves.most_common(4):
            if moves != base_moves:
                lines.append(f"  instead {label(moves)}: {self.share(count):.0%}")
        captain = base.first.captain
        lines.append(
            f"  captain {names.get(captain, captain)}: {self.share(self.captains[captain]):.0%}"
            + "".join(
                f" · {names.get(c, c)} {self.share(n):.0%}"
                for c, n in self.captains.most_common(3)
                if c != captain
            )
        )
        return lines


def perturb(
    xp: dict[tuple[int, int], float], rng: random.Random, noise: float
) -> dict[tuple[int, int], float]:
    """Scale every projection of a player by one shared random factor."""
    factor: dict[int, float] = {}
    out: dict[tuple[int, int], float] = {}
    for (element_id, event), value in xp.items():
        if element_id not in factor:
            factor[element_id] = max(0.0, 1.0 + rng.gauss(0.0, noise))
        out[(element_id, event)] = value * factor[element_id]
    return out


def robustness(
    spec: OptimiserInput,
    *,
    samples: int = DEFAULT_SAMPLES,
    noise: float = DEFAULT_NOISE,
    seed: int | None = None,
    time_limit: int = SAMPLE_TIME_LIMIT,
) -> Robustness:
    """Re-solve ``spec`` ``samples`` times with perturbed projections."""
    if samples < 1:
        raise ValueError("samples must be at least 1")
    if noise < 0:
        raise ValueError("noise must be non-negative")
    rng = random.Random(seed)
    moves: Counter[Moves] = Counter()
    captains: Counter[int] = Counter()
    buys: Counter[int] = Counter()
    failed = 0
    limit = min(spec.solver_time_limit, time_limit)
    for _ in range(samples):
        trial = replace(spec, xp=perturb(spec.xp, rng, noise), solver_time_limit=limit)
        try:
            plan = optimise(trial)
        except SolverError:
            failed += 1
            continue
        moves[first_week_moves(plan)] += 1
        captains[plan.first.captain] += 1
        buys.update(plan.first.transfers_in)
    return Robustness(
        samples=samples, failed=failed, noise=noise, moves=moves, captains=captains, buys=buys
    )


__all__ = [
    "DEFAULT_NOISE",
    "DEFAULT_SAMPLES",
    "Robustness",
    "first_week_moves",
    "perturb",
    "robustness",
]
