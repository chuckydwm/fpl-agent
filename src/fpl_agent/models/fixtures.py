"""Fixture difficulty over a rolling horizon.

FPL ships its own 1-5 difficulty rating, which is coarse and set before the
season. We keep it as a baseline but also derive a continuous rating from the
opponent's attack/defence strength split by home and away, which reacts to the
actual matchup rather than a preseason guess.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime

from fpl_agent.api.models import Fixture, Team

#: FPL's published strengths sit near this value; used to normalise to ~1.0.
_STRENGTH_BASELINE = 1100.0


@dataclass(frozen=True, slots=True)
class FixtureRun:
    """One team's fixture in one gameweek, from that team's point of view."""

    team_id: int
    event: int
    opponent_id: int
    is_home: bool
    fdr: int
    kickoff: datetime | None

    #: >1.0 means an easier-than-average attacking matchup.
    attack_multiplier: float
    #: >1.0 means a better-than-average chance of a clean sheet.
    defence_multiplier: float


def _ratio(value: int) -> float:
    return value / _STRENGTH_BASELINE if value else 1.0


def build_fixture_runs(
    fixtures: list[Fixture],
    teams: dict[int, Team],
    *,
    events: list[int] | None = None,
) -> dict[tuple[int, int], FixtureRun]:
    """Index fixtures by ``(team_id, event)``.

    Blank gameweeks are simply absent from the mapping; double gameweeks keep
    only the first fixture, which the optimiser treats conservatively. Handling
    doubles properly is a known gap — see the TODO in ``optimiser/squad.py``.
    """
    runs: dict[tuple[int, int], FixtureRun] = {}

    for fixture in fixtures:
        if fixture.event is None:
            continue
        if events is not None and fixture.event not in events:
            continue

        for team_id in (fixture.team_h, fixture.team_a):
            key = (team_id, fixture.event)
            if key in runs:
                continue  # first fixture of a double gameweek wins

            opponent_id = fixture.opponent_of(team_id)
            if opponent_id is None or opponent_id not in teams:
                continue

            is_home = fixture.is_home_for(team_id)
            opponent = teams[opponent_id]

            # Facing a weak defence helps our attack; facing a weak attack
            # helps our clean-sheet odds. Invert so higher is always better.
            opp_defence = _ratio(
                opponent.strength_defence_away if is_home else opponent.strength_defence_home
            )
            opp_attack = _ratio(
                opponent.strength_attack_away if is_home else opponent.strength_attack_home
            )

            home_bonus = 1.08 if is_home else 0.94

            runs[key] = FixtureRun(
                team_id=team_id,
                event=fixture.event,
                opponent_id=opponent_id,
                is_home=is_home,
                fdr=fixture.difficulty_for(team_id) or 3,
                kickoff=fixture.kickoff_time,
                attack_multiplier=round(home_bonus / max(opp_defence, 0.5), 4),
                defence_multiplier=round(home_bonus / max(opp_attack, 0.5), 4),
            )

    return runs
