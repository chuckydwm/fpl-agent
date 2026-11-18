"""Team expected goals for and against, and the clean-sheet chance they imply.

FPL publishes no team xG, but every player row in ``element-summary`` carries
the fixture's xG for that player. Summing a club's players per fixture gives
the club's xG in that match, and the same figure is its opponent's xG
against. Per-match rates are shrunk toward the league average by
``PRIOR_MATCHES`` imaginary average matches, so a club three games in is not
rated on three games alone.

A clean sheet is then a Poisson zero: goals against in a match average
``opponent xG for × own xG against / league average``, adjusted for venue.
"""

from __future__ import annotations

import math
from collections.abc import Iterable
from dataclasses import dataclass

from fpl_agent.api.models import Fixture

#: Imaginary league-average matches blended into every club's rates.
PRIOR_MATCHES = 5.0

#: Home sides concede roughly a tenth less than average, away sides more.
HOME_CONCEDE = 0.9
AWAY_CONCEDE = 1.1


@dataclass(frozen=True, slots=True)
class TeamXG:
    #: Per-match, shrunk toward the league average.
    xg_for: float
    xg_against: float
    matches: int


@dataclass(frozen=True, slots=True)
class LeagueXG:
    teams: dict[int, TeamXG]
    #: League-average xG per team per match (raw, before shrinkage).
    average: float

    def clean_sheet_chance(self, team: int, opponent: int, *, is_home: bool) -> float | None:
        """P(``team`` concedes nothing against ``opponent``); None without data."""
        own, opp = self.teams.get(team), self.teams.get(opponent)
        if own is None or opp is None or self.average <= 0:
            return None
        venue = HOME_CONCEDE if is_home else AWAY_CONCEDE
        expected_against = opp.xg_for * own.xg_against / self.average * venue
        return math.exp(-expected_against)


def league_xg(rows: Iterable[tuple[int, int, float]], fixtures: Iterable[Fixture]) -> LeagueXG:
    """Rates from ``(fixture_id, team_id, xg)`` rows, one per player per match.

    ``team_id`` is the player's club in that match. Rows for fixtures not in
    ``fixtures`` are ignored. Returns an empty league when nothing is known.
    """
    by_id = {f.id: f for f in fixtures}
    match_xg: dict[tuple[int, int], float] = {}
    for fixture_id, team, xg in rows:
        fixture = by_id.get(fixture_id)
        if fixture is None or fixture.opponent_of(team) is None:
            continue
        match_xg[(fixture_id, team)] = match_xg.get((fixture_id, team), 0.0) + xg
    if not match_xg:
        return LeagueXG(teams={}, average=0.0)

    scored: dict[int, list[float]] = {}
    conceded: dict[int, list[float]] = {}
    for (fixture_id, team), xg in match_xg.items():
        opponent = by_id[fixture_id].opponent_of(team)
        assert opponent is not None
        scored.setdefault(team, []).append(xg)
        conceded.setdefault(opponent, []).append(xg)

    average = sum(match_xg.values()) / len(match_xg)

    def shrunk(values: list[float]) -> float:
        return (sum(values) + PRIOR_MATCHES * average) / (len(values) + PRIOR_MATCHES)

    teams = {
        team: TeamXG(
            xg_for=shrunk(scored.get(team, [])),
            xg_against=shrunk(conceded.get(team, [])),
            matches=max(len(scored.get(team, [])), len(conceded.get(team, []))),
        )
        for team in set(scored) | set(conceded)
    }
    return LeagueXG(teams=teams, average=average)


__all__ = ["LeagueXG", "TeamXG", "league_xg"]
