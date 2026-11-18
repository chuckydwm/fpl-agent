"""xG/xA attacking rates and the robustness re-solve."""

from __future__ import annotations

import pytest

from fpl_agent.api.models import Element
from fpl_agent.backtest.history import HistoryStore, PlayerGameweek
from fpl_agent.models.expected_points import XG_WEIGHT, attacking_input, project, project_squad
from fpl_agent.models.fixtures import FixtureRun, build_fixture_runs
from fpl_agent.models.minutes import MinutesModel


def _forward(**kw) -> Element:
    base = dict(
        id=1, web_name="F", team=1, element_type=4, now_cost=80, minutes=900, goals_scored=6
    )
    return Element(**{**base, **kw})


RUN = FixtureRun(1, 11, 2, True, 3, None, 1.0, 1.0)


class TestAttackingInput:
    def test_without_xg_the_actual_total_is_used(self):
        assert attacking_input(6, None) == 6.0

    def test_with_xg_it_is_weighted_toward_expected(self):
        assert attacking_input(6, 2.0) == pytest.approx(XG_WEIGHT * 2.0 + (1 - XG_WEIGHT) * 6)

    def test_api_strings_parse_as_floats(self):
        element = Element.model_validate(
            {
                "id": 1,
                "web_name": "F",
                "team": 1,
                "element_type": 4,
                "now_cost": 80,
                "expected_goals": "3.41",
                "expected_assists": "0.90",
            }
        )
        assert element.expected_goals == pytest.approx(3.41)
        assert element.expected_assists == pytest.approx(0.90)


class TestProjectionUsesXg:
    def test_a_lucky_finisher_projects_lower_than_his_goals_suggest(self):
        model = MinutesModel(gameweeks_played=10)
        lucky = project(_forward(expected_goals=2.0), (RUN,), model, event=11)
        plain = project(_forward(), (RUN,), model, event=11)
        assert lucky.xp < plain.xp

    def test_an_unlucky_finisher_projects_higher(self):
        model = MinutesModel(gameweeks_played=10)
        unlucky = project(_forward(goals_scored=1, expected_goals=5.0), (RUN,), model, event=11)
        plain = project(_forward(goals_scored=1), (RUN,), model, event=11)
        assert unlucky.xp > plain.xp

    def test_xg_equal_to_goals_changes_nothing(self):
        model = MinutesModel(gameweeks_played=10)
        same = project(_forward(expected_goals=6.0, expected_assists=0.0), (RUN,), model, event=11)
        plain = project(_forward(), (RUN,), model, event=11)
        assert same.xp == pytest.approx(plain.xp)


class TestHistoryCarriesXg:
    def _store(self, elements, *, with_xg: bool) -> HistoryStore:
        rows = [
            PlayerGameweek(
                element=e.id,
                round=r,
                minutes=90,
                goals_scored=1,
                expected_goals=0.25 if with_xg else None,
                expected_assists=0.1 if with_xg else None,
                value=e.now_cost,
            )
            for e in elements
            for r in (1, 2, 3)
        ]
        return HistoryStore(rows=rows, template={e.id: e for e in elements})

    def test_snapshot_sums_xg_from_earlier_rounds_only(self, elements):
        snap = {e.id: e for e in self._store(elements, with_xg=True).snapshot_before(3)}
        one = snap[elements[0].id]
        assert one.expected_goals == pytest.approx(0.5)
        assert one.expected_assists == pytest.approx(0.2)

    def test_rows_without_xg_leave_none_not_zero(self, elements):
        snap = self._store(elements, with_xg=False).snapshot_before(3)
        assert all(e.expected_goals is None and e.expected_assists is None for e in snap)

    def test_before_the_first_round_nothing_is_known(self, elements):
        snap = self._store(elements, with_xg=True).snapshot_before(1)
        assert all(e.expected_goals is None for e in snap)


class TestRobustness:
    pytest.importorskip("pulp")

    def _spec(self, elements, fixtures, teams):
        from fpl_agent.optimiser.squad import OptimiserInput

        events = [1, 2]
        runs = build_fixture_runs(fixtures, teams, events=events)
        projections = project_squad(elements, runs, MinutesModel(gameweeks_played=3), events)
        return OptimiserInput(
            elements=elements,
            xp={k: v.xp for k, v in projections.items()},
            events=events,
            current_squad=set(),
            bank_tenths=1000,
            free_transfers=15,
            max_transfers_per_event=15,
        )

    def test_zero_noise_reproduces_the_base_plan_every_time(self, elements, fixtures, teams):
        from fpl_agent.optimiser.robust import first_week_moves, robustness
        from fpl_agent.optimiser.squad import optimise

        spec = self._spec(elements, fixtures, teams)
        base = optimise(spec)
        check = robustness(spec, samples=3, noise=0.0, seed=1)
        assert check.failed == 0
        assert check.moves[first_week_moves(base)] == 3
        assert check.captains[base.first.captain] == 3
        assert any("100%" in line for line in check.describe(base, {}))

    def test_noise_is_seeded_and_counts_add_up(self, elements, fixtures, teams):
        from fpl_agent.optimiser.robust import robustness

        spec = self._spec(elements, fixtures, teams)
        a = robustness(spec, samples=4, noise=0.5, seed=7)
        b = robustness(spec, samples=4, noise=0.5, seed=7)
        assert a.moves == b.moves and a.captains == b.captains
        assert sum(a.moves.values()) + a.failed == 4
        assert sum(a.captains.values()) == a.solved

    def test_one_factor_per_player_across_the_horizon(self):
        import random

        from fpl_agent.optimiser.robust import perturb

        xp = {(1, 1): 2.0, (1, 2): 4.0, (2, 1): 3.0}
        out = perturb(xp, random.Random(3), 0.3)
        assert out[(1, 2)] / out[(1, 1)] == pytest.approx(2.0)
        assert all(v >= 0 for v in out.values())

    def test_bad_arguments_are_rejected(self, elements, fixtures, teams):
        from fpl_agent.optimiser.robust import robustness

        spec = self._spec(elements, fixtures, teams)
        with pytest.raises(ValueError):
            robustness(spec, samples=0)
        with pytest.raises(ValueError):
            robustness(spec, noise=-0.1)
