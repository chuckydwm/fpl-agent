"""Synthetic fixtures: a miniature but *legal* FPL universe.

Six clubs, enough players to fill a squad twice over, so the optimiser has real
choices to make and the club-limit constraint can actually bind.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from fpl_agent.api.models import Element, Fixture, Team

CLUBS = 6
PER_CLUB_BY_TYPE = {1: 3, 2: 6, 3: 6, 4: 4}


@pytest.fixture
def teams() -> dict[int, Team]:
    return {
        i: Team(
            id=i,
            name=f"Club {i}",
            short_name=f"C{i}",
            strength_attack_home=1000 + i * 20,
            strength_attack_away=980 + i * 20,
            strength_defence_home=1000 + i * 20,
            strength_defence_away=980 + i * 20,
        )
        for i in range(1, CLUBS + 1)
    }


@pytest.fixture
def elements() -> list[Element]:
    out: list[Element] = []
    element_id = 1
    for team in range(1, CLUBS + 1):
        for element_type, count in PER_CLUB_BY_TYPE.items():
            for n in range(count):
                out.append(
                    Element(
                        id=element_id,
                        web_name=f"T{team}P{element_type}{n}",
                        team=team,
                        element_type=element_type,
                        now_cost=40 + (element_type * 10) + (n * 5),
                        minutes=90 * 3,
                        goals_scored=n % 3,
                        assists=n % 2,
                        bonus=n % 2,
                        defensive_contribution=4 if element_type in (2, 3) else 0,
                        status="a",
                    )
                )
                element_id += 1
    return out


@pytest.fixture
def fixtures() -> list[Fixture]:
    """Two gameweeks; every club plays exactly once per gameweek."""
    kickoff = datetime(2026, 8, 29, 11, 30, tzinfo=UTC)
    out: list[Fixture] = []
    fixture_id = 1
    for event in (1, 2):
        pairs = [(1, 2), (3, 4), (5, 6)] if event == 1 else [(2, 3), (4, 5), (6, 1)]
        for home, away in pairs:
            out.append(
                Fixture(
                    id=fixture_id,
                    event=event,
                    team_h=home,
                    team_a=away,
                    team_h_difficulty=2,
                    team_a_difficulty=4,
                    kickoff_time=kickoff + timedelta(days=7 * (event - 1)),
                )
            )
            fixture_id += 1
    return out
