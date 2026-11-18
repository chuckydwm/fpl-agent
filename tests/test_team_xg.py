"""Team xG and the clean-sheet chance it implies."""

from __future__ import annotations

import math

import pytest

from fpl_agent.api.models import Fixture
from fpl_agent.backtest.history import HistoryStore, PlayerGameweek
from fpl_agent.models.fixtures import build_fixture_runs
from fpl_agent.models.team_xg import AWAY_CONCEDE, HOME_CONCEDE, PRIOR_MATCHES, league_xg

FIXTURES = [
    Fixture(id=1, event=1, team_h=1, team_a=2),
    Fixture(id=2, event=1, team_h=3, team_a=4),
    Fixture(id=3, event=2, team_h=2, team_a=3),
    Fixture(id=4, event=2, team_h=4, team_a=1),
]


class TestLeagueXG:
    def test_no_rows_means_no_data(self):
        league = league_xg([], FIXTURES)
        assert league.teams == {}
        assert league.clean_sheet_chance(1, 2, is_home=True) is None

    def test_players_add_up_per_match_and_count_against_the_opponent(self):
        rows = [(1, 1, 1.0), (1, 1, 1.0), (1, 2, 0.5), (2, 3, 1.5), (2, 4, 1.0)]
        league = league_xg(rows, FIXTURES)
        avg = (2.0 + 0.5 + 1.5 + 1.0) / 4
        assert league.average == pytest.approx(avg)
        one = league.teams[1]
        assert one.matches == 1
        assert one.xg_for == pytest.approx((2.0 + PRIOR_MATCHES * avg) / (1 + PRIOR_MATCHES))
        assert one.xg_against == pytest.approx((0.5 + PRIOR_MATCHES * avg) / (1 + PRIOR_MATCHES))

    def test_an_average_matchup_is_a_poisson_zero_at_the_league_rate(self):
        rows = [(1, 1, 1.25), (1, 2, 1.25), (2, 3, 1.25), (2, 4, 1.25)]
        league = league_xg(rows, FIXTURES)
        home = league.clean_sheet_chance(1, 2, is_home=True)
        away = league.clean_sheet_chance(2, 1, is_home=False)
        assert home == pytest.approx(math.exp(-1.25 * HOME_CONCEDE))
        assert away == pytest.approx(math.exp(-1.25 * AWAY_CONCEDE))
        assert home > away

    def test_a_leaky_side_facing_a_potent_attack_rarely_keeps_one(self):
        rows = [(1, 1, 3.0), (1, 2, 0.2), (2, 3, 1.2), (2, 4, 1.2)]
        league = league_xg(rows, FIXTURES)
        # Club 2 conceded 3.0 xG to club 1; club 1 conceded 0.2.
        assert league.clean_sheet_chance(2, 1, is_home=True) < league.clean_sheet_chance(
            1, 2, is_home=True
        )

    def test_unknown_fixtures_are_ignored(self):
        assert league_xg([(99, 1, 2.0)], FIXTURES).teams == {}


class TestFixtureRuns:
    def test_runs_carry_the_chance_only_when_both_clubs_have_data(self, teams):
        rows = [(1, 1, 1.0), (1, 2, 1.0)]
        league = league_xg(rows, FIXTURES)
        runs = build_fixture_runs(FIXTURES, teams, events=[2], league=league)
        (four_v_one,) = runs[(1, 2)]
        assert four_v_one.clean_sheet_chance is None  # club 4 has no xG yet
        (two_v_three,) = runs[(2, 2)]
        assert two_v_three.clean_sheet_chance is None
        runs_without = build_fixture_runs(FIXTURES, teams, events=[1])
        assert all(r.clean_sheet_chance is None for rs in runs_without.values() for r in rs)

    def test_projection_uses_the_xg_chance_for_defenders(self):
        from fpl_agent.api.models import Element
        from fpl_agent.models.expected_points import project
        from fpl_agent.models.fixtures import FixtureRun
        from fpl_agent.models.minutes import MinutesModel

        defender = Element(id=1, web_name="D", team=1, element_type=2, now_cost=50, minutes=900)
        model = MinutesModel(gameweeks_played=10)
        tight = FixtureRun(1, 11, 2, True, 3, None, 1.0, 1.0, clean_sheet_chance=0.6)
        open_ = FixtureRun(1, 11, 2, True, 3, None, 1.0, 1.0, clean_sheet_chance=0.1)
        gap = project(defender, (tight,), model, event=11).xp
        gap -= project(defender, (open_,), model, event=11).xp
        p60 = model.probability(defender).p_60
        assert gap == pytest.approx(0.5 * 4 * p60, abs=1e-3)


class TestHistoryRows:
    def test_rows_before_the_round_only_with_the_club_from_the_fixture(self, elements):
        player = elements[0]  # today at club 1
        rows = [
            PlayerGameweek(
                element=player.id, round=1, fixture=2, opponent_team=4, expected_goals=0.4
            ),
            PlayerGameweek(
                element=player.id, round=2, fixture=3, opponent_team=3, expected_goals=0.7
            ),
            PlayerGameweek(element=player.id, round=1, fixture=None, expected_goals=9.0),
        ]
        store = HistoryStore(rows=rows, template={e.id: e for e in elements})
        # In fixture 2 he played for club 3 (a transfer since), not club 1.
        assert store.team_xg_rows(2, FIXTURES) == [(2, 3, 0.4)]
        assert sorted(store.team_xg_rows(3, FIXTURES)) == [(2, 3, 0.4), (3, 2, 0.7)]
