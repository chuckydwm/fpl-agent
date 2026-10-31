from __future__ import annotations

import pytest

from fpl_agent.api.models import Element, Position
from fpl_agent.backtest.history import HistoryStore, PlayerGameweek
from fpl_agent.backtest.metrics import compare
from fpl_agent.backtest.replay import _apply_auto_subs, replay
from fpl_agent.backtest.strategies import HoldStrategy, OptimiserStrategy, TemplateStrategy

pytest.importorskip("pulp")


@pytest.fixture
def store(elements) -> HistoryStore:
    """Three gameweeks of invented returns, deterministic per player and round."""
    rows: list[PlayerGameweek] = []
    for element in elements:
        for round_id in (1, 2, 3):
            scored = (element.id + round_id) % 7
            rows.append(
                PlayerGameweek(
                    element=element.id,
                    round=round_id,
                    total_points=scored,
                    minutes=0 if element.id % 11 == 0 else 90,
                    goals_scored=1 if scored > 5 else 0,
                    assists=1 if scored == 4 else 0,
                    clean_sheets=1 if scored % 3 == 0 else 0,
                    bonus=1 if scored > 5 else 0,
                    value=element.now_cost,
                )
            )
    return HistoryStore(rows=rows, template={e.id: e for e in elements})


class TestPointInTimeSnapshots:
    """The backtest is worthless if the snapshot leaks the future."""

    def test_snapshot_before_first_gameweek_knows_nothing(self, store):
        for element in store.snapshot_before(1):
            assert element.minutes == 0
            assert element.total_points == 0
            assert element.goals_scored == 0

    def test_snapshot_accumulates_only_earlier_rounds(self, store, elements):
        target = elements[3].id
        before_2 = {e.id: e for e in store.snapshot_before(2)}[target]
        before_3 = {e.id: e for e in store.snapshot_before(3)}[target]

        expected_gw1 = (target + 1) % 7
        expected_gw2 = (target + 2) % 7
        assert before_2.total_points == expected_gw1
        assert before_3.total_points == expected_gw1 + expected_gw2

    def test_snapshot_never_contains_the_current_gameweek(self, store, elements):
        """The canary: totals before GW3 must exclude GW3's own return."""
        for element in elements[:10]:
            gw3 = (element.id + 3) % 7
            before = {e.id: e for e in store.snapshot_before(3)}[element.id]
            all_three = sum((element.id + r) % 7 for r in (1, 2, 3))
            assert before.total_points == all_three - gw3

    def test_todays_injury_flags_are_not_leaked_backwards(self, store, elements):
        """Availability is 'now' state and cannot be reconstructed historically."""
        store.template[elements[0].id] = elements[0].model_copy(
            update={"status": "i", "chance_of_playing_next_round": 0}
        )
        recovered = {e.id: e for e in store.snapshot_before(3)}[elements[0].id]
        assert recovered.status == "a"
        assert recovered.chance_of_playing_next_round is None

    def test_price_is_taken_from_the_time_not_today(self, store, elements):
        target = elements[5]
        assert store.price_at(target.id, 2) == target.now_cost / 10.0


class TestAutoSubs:
    def _pool(self, elements) -> dict[int, Element]:
        return {e.id: e for e in elements}

    def test_non_playing_starter_is_replaced(self, elements):
        by_id = self._pool(elements)
        gk = next(e.id for e in elements if e.position is Position.GKP)
        defs = [e.id for e in elements if e.position is Position.DEF][:6]
        mids = [e.id for e in elements if e.position is Position.MID][:5]
        fwds = [e.id for e in elements if e.position is Position.FWD][:2]

        xi = [gk, *defs[:3], *mids, *fwds]
        bench = [defs[3], defs[4]]
        minutes = dict.fromkeys(xi + bench, 90)
        minutes[defs[0]] = 0  # a starter blanks

        final, subs = _apply_auto_subs(xi, bench, by_id, minutes)
        assert len(final) == 11
        assert defs[0] not in final
        assert len(subs) == 1

    def test_outfield_player_cannot_replace_a_keeper(self, elements):
        by_id = self._pool(elements)
        gk = next(e.id for e in elements if e.position is Position.GKP)
        defs = [e.id for e in elements if e.position is Position.DEF][:5]
        mids = [e.id for e in elements if e.position is Position.MID][:4]
        fwds = [e.id for e in elements if e.position is Position.FWD][:2]

        xi = [gk, *defs[:4], *mids, *fwds]
        bench = [defs[4]]  # outfield only
        minutes = dict.fromkeys(xi + bench, 90)
        minutes[gk] = 0

        final, subs = _apply_auto_subs(xi, bench, by_id, minutes)
        assert subs == []
        assert gk in final, "an outfielder must never sub in for a keeper"

    def test_substitution_respects_formation_legality(self, elements):
        """Going below three defenders is illegal, so the sub must be refused."""
        by_id = self._pool(elements)
        gk = next(e.id for e in elements if e.position is Position.GKP)
        defs = [e.id for e in elements if e.position is Position.DEF][:3]
        mids = [e.id for e in elements if e.position is Position.MID][:5]
        fwds = [e.id for e in elements if e.position is Position.FWD][:2]

        xi = [gk, *defs, *mids, *fwds]
        bench = [next(e.id for e in elements if e.position is Position.FWD and e.id not in fwds)]
        minutes = dict.fromkeys(xi + bench, 90)
        minutes[defs[0]] = 0

        final, subs = _apply_auto_subs(xi, bench, by_id, minutes)
        assert subs == [], "3-4-3 cannot drop to two defenders"
        assert len(final) == 11


class TestReplay:
    def _run(self, strategy, store, fixtures, teams):
        return replay(store, strategy, events=[1, 2, 3], fixtures=fixtures, teams=teams)

    def test_replay_scores_every_gameweek(self, store, fixtures, teams):
        result = self._run(OptimiserStrategy(max_transfers=2), store, fixtures, teams)
        assert [gw.event for gw in result.gameweeks] == [1, 2, 3]
        assert result.total_points > 0

    def test_hold_strategy_never_transfers_after_the_build(self, store, fixtures, teams):
        result = self._run(HoldStrategy(), store, fixtures, teams)
        assert [gw.transfers for gw in result.gameweeks[1:]] == [0, 0]
        assert result.total_hits == 0

    def test_template_strategy_is_playable(self, store, fixtures, teams):
        result = self._run(TemplateStrategy(), store, fixtures, teams)
        assert len(result.gameweeks) == 3

    def test_captain_points_are_doubled(self, store, fixtures, teams):
        result = self._run(OptimiserStrategy(max_transfers=2), store, fixtures, teams)
        for gw in result.gameweeks:
            raw = sum(store.actual_points(pid, gw.event) for pid in [gw.captain])
            assert gw.captain_points == raw

    def test_hits_reduce_net_points(self, store, fixtures, teams):
        result = self._run(OptimiserStrategy(max_transfers=3), store, fixtures, teams)
        for gw in result.gameweeks:
            assert gw.net_points == gw.points - gw.hits * 4

    def test_report_is_produced(self, store, fixtures, teams, elements):
        result = self._run(OptimiserStrategy(max_transfers=2), store, fixtures, teams)
        report = compare(result, store, {e.id: e for e in elements})
        assert report.gameweeks == 3
        assert 0.0 <= report.captain_hit_rate <= 1.0
        assert 0.0 <= report.captain_efficiency <= 1.0


class TestSignalDetection:
    """The floor test: with real signal present, beat doing nothing.

    Deliberately *not* asserted against the template strategy. Ownership in any
    synthetic universe is a cleaner read on quality than a dozen noisy
    gameweeks, so template wins here for reasons that say nothing about the
    live game. Tuning a benchmark until the model under test wins is the exact
    failure a backtest exists to prevent.
    """

    @staticmethod
    def _universe(gameweeks: int = 10):
        import random

        from fpl_agent.api.models import Fixture, Team

        rng = random.Random(7)
        clubs = 12
        per_club = {1: 2, 2: 5, 3: 5, 4: 3}

        teams = {i: Team(id=i, name=f"Club {i}", short_name=f"C{i}") for i in range(1, clubs + 1)}

        pool: list[Element] = []
        quality: dict[int, float] = {}
        element_id = 1
        for team in range(1, clubs + 1):
            for element_type, count in per_club.items():
                for _ in range(count):
                    q = rng.betavariate(2, 5)
                    pool.append(
                        Element(
                            id=element_id,
                            web_name=f"P{element_id}",
                            team=team,
                            element_type=element_type,
                            now_cost=max(38, 40 + int(q * 100)),
                            selected_by_percent="5.0",
                        )
                    )
                    quality[element_id] = q
                    element_id += 1

        fixtures: list[Fixture] = []
        fixture_id = 1
        order = list(range(1, clubs + 1))
        for event in range(1, gameweeks + 1):
            rng.shuffle(order)
            for i in range(0, clubs, 2):
                fixtures.append(
                    Fixture(id=fixture_id, event=event, team_h=order[i], team_a=order[i + 1])
                )
                fixture_id += 1

        rows: list[PlayerGameweek] = []
        for element in pool:
            q = quality[element.id]
            for event in range(1, gameweeks + 1):
                plays = rng.random() < (0.45 + 0.5 * q)
                points = max(0, int(rng.gauss(1.5 + 7.0 * q, 2.4))) if plays else 0
                rows.append(
                    PlayerGameweek(
                        element=element.id,
                        round=event,
                        total_points=points,
                        minutes=90 if plays else 0,
                        goals_scored=1 if points >= 8 else 0,
                        assists=1 if 5 <= points < 8 else 0,
                        clean_sheets=1 if points in (5, 6) else 0,
                        bonus=1 if points >= 9 else 0,
                        value=element.now_cost,
                    )
                )

        return HistoryStore(rows=rows, template={e.id: e for e in pool}), fixtures, teams, gameweeks

    def test_optimiser_beats_doing_nothing(self):
        store, fixtures, teams, gameweeks = self._universe()
        events = list(range(1, gameweeks + 1))

        active = replay(
            store,
            OptimiserStrategy(horizon=1, max_transfers=2),
            events=events,
            fixtures=fixtures,
            teams=teams,
        )
        passive = replay(store, HoldStrategy(), events=events, fixtures=fixtures, teams=teams)
        assert active.total_points > passive.total_points, (
            f"optimiser {active.total_points} did not beat hold {passive.total_points} "
            "on a dataset where past returns are genuinely predictive"
        )


# ---------------------------------------------------------------------------
# Regression tests for the backtest-correctness audit (main @ d155c531).
# ---------------------------------------------------------------------------


def _dgw_rows(element_id: int = 1) -> list[PlayerGameweek]:
    """One player, GW1 single, GW2 double (two fixtures), GW3 single."""
    return [
        PlayerGameweek(
            element=element_id,
            round=1,
            fixture=10,
            kickoff_time="2026-08-15T14:00:00Z",
            total_points=2,
            minutes=90,
            bps=10,
            value=60,
        ),
        PlayerGameweek(
            element=element_id,
            round=2,
            fixture=20,
            kickoff_time="2026-08-22T14:00:00Z",
            total_points=6,
            minutes=90,
            goals_scored=1,
            clean_sheets=0,
            bonus=1,
            bps=30,
            defensive_contribution=4,
            value=61,
        ),
        PlayerGameweek(
            element=element_id,
            round=2,
            fixture=21,
            kickoff_time="2026-08-25T19:00:00Z",
            total_points=9,
            minutes=75,
            goals_scored=1,
            assists=1,
            clean_sheets=1,
            bonus=3,
            bps=40,
            defensive_contribution=7,
            value=62,
        ),
        PlayerGameweek(
            element=element_id,
            round=3,
            fixture=30,
            kickoff_time="2026-08-29T14:00:00Z",
            total_points=1,
            minutes=10,
            value=62,
        ),
    ]


class TestDoubleGameweeks:
    """Two fixtures in one round must add up, not overwrite each other."""

    @pytest.mark.parametrize("reverse", [False, True], ids=["in-order", "reversed"])
    def test_both_fixtures_count_whatever_the_input_order(self, elements, reverse):
        rows = _dgw_rows(elements[0].id)
        if reverse:
            rows = list(reversed(rows))
        store = HistoryStore(rows=rows, template={elements[0].id: elements[0]})
        pid = elements[0].id

        assert store.actual_points(pid, 2) == 15
        assert store.actual_minutes(pid, 2) == 165
        assert store.fixtures_played(pid, 2) == 2
        for stat, total in {
            "goals_scored": 2,
            "assists": 1,
            "clean_sheets": 1,
            "bonus": 4,
            "bps": 70,
            "defensive_contribution": 11,
        }.items():
            assert store.actual(pid, 2, stat) == total, stat

        before_gw3 = {e.id: e for e in store.snapshot_before(3)}[pid]
        assert before_gw3.total_points == 2 + 15
        assert before_gw3.minutes == 90 + 165
        assert before_gw3.goals_scored == 2
        assert before_gw3.bonus == 4
        assert before_gw3.bps == 80
        assert before_gw3.defensive_contribution == 11

    @pytest.mark.parametrize("reverse", [False, True], ids=["in-order", "reversed"])
    def test_price_is_a_snapshot_not_a_sum(self, elements, reverse):
        rows = _dgw_rows(elements[0].id)
        if reverse:
            rows = list(reversed(rows))
        store = HistoryStore(rows=rows, template={elements[0].id: elements[0]})
        pid = elements[0].id

        # At the GW2 deadline the price was the first fixture's, not 61 + 62.
        assert store.price_at(pid, 2) == 6.1
        # Going into GW3 the latest known price is the GW2 second fixture's.
        assert {e.id: e for e in store.snapshot_before(3)}[pid].now_cost == 62

    def test_a_repeated_fixture_row_is_not_double_counted(self, elements):
        rows = _dgw_rows(elements[0].id)
        store = HistoryStore(rows=rows + [rows[1]], template={elements[0].id: elements[0]})
        assert store.actual_points(elements[0].id, 2) == 15
        assert store.fixtures_played(elements[0].id, 2) == 2

    def test_a_double_gameweek_scores_both_fixtures_in_replay(self, store, elements):
        """The replay itself must see the summed return, not the last fixture."""
        pid = elements[0].id
        extra = PlayerGameweek(
            element=pid,
            round=1,
            fixture=999,
            total_points=20,
            minutes=90,
            value=elements[0].now_cost,
        )
        single = store.actual_points(pid, 1)
        doubled = HistoryStore(rows=[*store.rows, extra], template=store.template)
        assert doubled.actual_points(pid, 1) == single + 20
