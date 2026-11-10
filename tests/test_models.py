from __future__ import annotations

from datetime import UTC, datetime, timedelta
from zoneinfo import ZoneInfo

import pytest

from fpl_agent.agent.report import format_deadline
from fpl_agent.api.endpoints import free_transfers_after
from fpl_agent.api.models import Element, EntryHistoryRow, Event, Pick
from fpl_agent.models.minutes import MinutesModel


def _element(**kwargs) -> Element:
    base = dict(id=1, web_name="X", team=1, element_type=3, now_cost=50)
    return Element(**{**base, **kwargs})


class TestMinutesModel:
    def test_injured_player_never_starts(self):
        model = MinutesModel(gameweeks_played=5)
        prob = model.probability(_element(status="i", minutes=450))
        assert prob.p_start == 0.0
        assert prob.reason == "injured"

    def test_suspended_player_never_starts(self):
        model = MinutesModel(gameweeks_played=5)
        assert model.probability(_element(status="s")).p_start == 0.0

    def test_regular_starter_scores_high(self):
        model = MinutesModel(gameweeks_played=5)
        assert model.probability(_element(minutes=450)).p_start > 0.9

    def test_bench_player_scores_low(self):
        model = MinutesModel(gameweeks_played=5)
        assert model.probability(_element(minutes=60)).p_start < 0.2

    def test_flagged_player_is_discounted(self):
        model = MinutesModel(gameweeks_played=5)
        fit = model.probability(_element(minutes=450))
        prob = model.probability(_element(minutes=450, chance_of_playing_next_round=50))
        # Availability times his usual role: a nailed starter at 50% starts half the time.
        assert prob.p_start == pytest.approx(0.5 * fit.p_start)
        assert prob.p_60 == pytest.approx(0.5 * fit.p_60)
        assert "50%" in prob.reason

    def test_a_cleared_flag_is_not_a_discount(self):
        # FPL keeps chance=100 after a flag is lifted; that player is fit.
        model = MinutesModel(gameweeks_played=5)
        cleared = model.probability(_element(minutes=450, chance_of_playing_next_round=100))
        never = model.probability(_element(minutes=450))
        assert cleared.p_start == never.p_start > 0.9

    def test_override_beats_the_statistical_baseline(self):
        """Press-conference signal must be able to overrule minutes history."""
        model = MinutesModel(gameweeks_played=5)
        element = _element(minutes=450)
        assert model.probability(element).p_start > 0.9
        model.add_override(element.id, 0.1, "manager confirmed he is rested")
        prob = model.probability(element)
        assert prob.p_start == pytest.approx(0.1)
        assert prob.reason == "manager confirmed he is rested"

    def test_preseason_refuses_to_guess(self):
        model = MinutesModel(gameweeks_played=0)
        assert model.probability(_element(minutes=0)).p_start == 0.5


def _row(event: int, transfers: int = 0) -> EntryHistoryRow:
    return EntryHistoryRow(event=event, points=0, total_points=0, event_transfers=transfers)


class TestFreeTransfers:
    def test_first_week_banks_nothing(self):
        """GW1 changes are free and do not bank: one transfer for GW2, not two."""
        assert free_transfers_after([_row(1)]) == 1

    def test_unused_transfers_roll(self):
        # GW1 grants 1 for GW2; GW2 and GW3 unused bank one each: 3 for GW4.
        assert free_transfers_after([_row(g) for g in range(1, 4)]) == 3

    def test_rollover_is_capped_at_five(self):
        assert free_transfers_after([_row(g) for g in range(1, 12)]) == 5

    def test_taking_hits_does_not_push_below_one(self):
        assert free_transfers_after([_row(1, transfers=6)]) == 1

    def test_the_gw4_ledger(self):
        """The real 2026/27 ledger: 0, 1, 1 transfers then 2 in GW4.

        GW4 had one free transfer, not two — the second move cost a hit. The
        old implementation started at one and reported two.
        """
        history = [_row(1), _row(2, 1), _row(3, 1)]
        assert free_transfers_after(history) == 1

    def test_wildcard_week_spends_nothing_and_still_grants_one(self):
        # Two banked by GW3; a wildcard in GW3 with 11 changes leaves them
        # untouched and adds the usual one: 3 for GW4.
        history = [_row(1), _row(2), _row(3, transfers=11)]
        assert free_transfers_after(history, chip_events={3: "wildcard"}) == 3
        # Without the chip those 11 changes would have consumed the bank.
        assert free_transfers_after(history) == 1


class TestPickSlots:
    def test_starters_have_no_bench_order(self):
        assert Pick(element=1, position=11, multiplier=1).bench_order is None

    def test_reserve_keeper_is_not_an_outfield_sub(self):
        assert Pick(element=1, position=12, multiplier=0).bench_order is None

    def test_outfield_subs_are_numbered_in_order(self):
        assert Pick(element=1, position=13, multiplier=0).bench_order == 1
        assert Pick(element=1, position=15, multiplier=0).bench_order == 3


class TestDeadlineFormatting:
    def _event(self, deadline: datetime) -> Event:
        return Event(id=2, name="Gameweek 2", deadline_time=deadline)

    def test_uk_evening_deadline_shows_as_next_day_in_utc_plus_8(self):
        """A Friday 18:30 BST deadline is Saturday 01:30 for a UTC+8 manager."""
        event = self._event(datetime(2026, 8, 28, 17, 30, tzinfo=UTC))
        text = format_deadline(
            event,
            ZoneInfo("Asia/Ulaanbaatar"),
            now=datetime(2026, 8, 28, 11, 30, tzinfo=UTC),
        )
        assert "Sat 29 Aug 01:30" in text

    def test_passed_deadline_says_so(self):
        event = self._event(datetime(2026, 8, 28, 17, 30, tzinfo=UTC))
        text = format_deadline(
            event,
            ZoneInfo("Asia/Ulaanbaatar"),
            now=datetime(2026, 8, 29, 0, 0, tzinfo=UTC),
        )
        assert "PASSED" in text

    def test_remaining_time_is_reported(self):
        now = datetime(2026, 8, 28, 11, 30, tzinfo=UTC)
        event = self._event(now + timedelta(hours=6, minutes=30))
        assert "6h 30m" in format_deadline(event, ZoneInfo("Asia/Ulaanbaatar"), now=now)


class TestEventSettlement:
    def test_finished_but_unchecked_is_not_settled(self):
        """Bonus is provisional until data_checked flips."""
        event = Event(
            id=1,
            name="Gameweek 1",
            deadline_time=datetime(2026, 8, 21, 17, 30, tzinfo=UTC),
            finished=True,
            data_checked=False,
        )
        assert not event.is_settled


class TestDefcon:
    """``defensive_contribution`` is a count of actions, not a probability."""

    def test_chance_tracks_the_real_per_match_threshold(self):
        from fpl_agent.models.expected_points import defcon_chance

        assert defcon_chance(0.0, 10) == 0.0
        assert defcon_chance(10.0, 10) == pytest.approx(0.542, abs=0.001)
        # Real 2026/27 rates: Hall hit 5 of 10 starts, Groß 0 of 10.
        assert 0.3 < defcon_chance(8.81, 10) < 0.6
        assert defcon_chance(5.83, 12) < 0.05

    def test_a_midfielder_with_few_actions_no_longer_gets_two_free_points(self):
        from fpl_agent.models.expected_points import DEFCON_POINTS, project
        from fpl_agent.models.fixtures import FixtureRun

        run = FixtureRun(1, 11, 2, True, 3, None, 1.0, 1.0)
        model = MinutesModel(gameweeks_played=10)
        base = dict(element_type=3, minutes=900, goals_scored=0, assists=0, bonus=0)
        busy = project(_element(**base, defensive_contribution=200), (run,), model, event=11)
        quiet = project(_element(**base, defensive_contribution=58), (run,), model, event=11)
        # 58 actions in 900' is 5.8 per 90: almost never 12 in a match.
        assert busy.xp - quiet.xp > 0.8 * DEFCON_POINTS
        assert quiet.xp < 2.0 + 0.1 * DEFCON_POINTS + 1.0  # appearance + a sliver


class TestEveryFixture:
    """Doubles, blanks and FPL's own projection (audit item 3)."""

    def _run(self, event: int = 11, attack: float = 1.0):
        from fpl_agent.models.fixtures import FixtureRun

        return FixtureRun(1, event, 2, True, 3, None, attack, 1.0)

    def _player(self, **kw):
        return _element(element_type=4, minutes=900, goals_scored=6, assists=2, bonus=4, **kw)

    def test_a_double_is_two_fixtures_summed_and_a_blank_is_zero(self):
        from fpl_agent.models.expected_points import project

        model = MinutesModel(gameweeks_played=10)
        one = project(self._player(), (self._run(),), model, event=11)
        two = project(self._player(), (self._run(), self._run()), model, event=11)
        blank = project(self._player(), (), model, event=11)
        assert two.xp == pytest.approx(2 * one.xp, abs=0.002)
        assert two.fixtures == 2
        assert blank.xp == 0.0 and blank.reason == "blank gameweek"

    def test_build_fixture_runs_keeps_both_games_of_a_double(self, teams):
        from fpl_agent.api.models import Fixture
        from fpl_agent.models.fixtures import build_fixture_runs

        fixtures = [
            Fixture(id=1, event=11, team_h=1, team_a=2, kickoff_time="2026-11-21T15:00:00Z"),
            Fixture(id=2, event=11, team_h=3, team_a=1, kickoff_time="2026-11-24T19:45:00Z"),
        ]
        runs = build_fixture_runs(fixtures, teams, events=[11])
        assert [r.opponent_id for r in runs[(1, 11)]] == [2, 3]
        assert len(runs[(2, 11)]) == 1 and (4, 11) not in runs

    def test_ep_next_is_a_gameweek_total_not_per_fixture_or_matchup_adjusted(self):
        from fpl_agent.models.expected_points import market_for, project

        player = self._player(ep_next="6.0")
        market = market_for(player, {11: 2, 12: 1}, [11, 12])
        # The coming double takes 6.0 as is; a later single is the per-fixture 3.0.
        assert market == {11: 6.0, 12: 3.0}
        model = MinutesModel(gameweeks_played=0)  # weight 0: all market
        easy = project(
            player,
            (self._run(attack=1.5), self._run(attack=1.5)),
            model,
            event=11,
            market=market[11],
            weight=0.0,
        )
        assert easy.xp == pytest.approx(6.0)

    def test_a_flagged_players_ep_next_does_not_carry_into_later_weeks(self):
        from fpl_agent.models.expected_points import market_for

        injured = self._player(ep_next="0.0", status="i")
        assert market_for(injured, {11: 1, 12: 1}, [11, 12]) == {11: 0.0, 12: None}
        doubt = self._player(ep_next="2.5", status="d", chance_of_playing_next_round=50)
        assert market_for(doubt, {11: 1, 12: 1}, [11, 12]) == {11: 2.5, 12: None}

    def test_availability_flags_speak_for_the_coming_gameweek_only(self):
        model = MinutesModel(gameweeks_played=5)
        injured = _element(minutes=450, status="i")
        assert model.probability(injured).p_start == 0.0
        later = model.probability(injured, next_round=False)
        assert later.p_start > 0.9 and "assumed back" in later.reason
        gone = _element(minutes=450, status="u")
        assert model.probability(gone, next_round=False).p_start == 0.0


def _matches(*played: tuple[int, bool]):
    from fpl_agent.models.minutes import Appearance

    return [Appearance(event=i + 1, minutes=m, started=s) for i, (m, s) in enumerate(played)]


class TestMinutesFromMatches:
    """Recent match-level minutes drive the outlook (audit item 8)."""

    def test_a_recent_decline_is_caught_not_averaged_away(self):
        # Cherki's real GW4-9: 45, 84, 90, 0, 45, 35 minutes.
        cherki = _matches((45, True), (84, True), (90, True), (0, False), (45, True), (35, False))
        nailed = _matches(*[(90, True)] * 6)
        model = MinutesModel(gameweeks_played=9, recent={1: cherki, 2: nailed})
        element = _element(minutes=299 + 45)  # his season average still looks fine
        declining = model.probability(element)
        steady = model.probability(_element(id=2, minutes=810))
        assert declining.p_60 < 0.5 < steady.p_60
        assert declining.p_start < steady.p_start
        assert "latest 0/45/35'" in declining.reason

    def test_minutes_when_starting_come_from_his_own_matches(self):
        early_off = _matches(*[(62, True)] * 6)
        prob = MinutesModel(gameweeks_played=6, recent={1: early_off}).probability(
            _element(minutes=372)
        )
        assert prob.minutes_if_start == pytest.approx(62)
        assert prob.p_60 > 0.8

    def test_sparse_history_falls_back_to_the_season_average(self):
        model = MinutesModel(gameweeks_played=5, recent={1: _matches((90, True), (90, True))})
        prob = model.probability(_element(minutes=450))
        assert "season average" in prob.reason
        assert prob.p_60 == pytest.approx(prob.p_start * 0.85)

    def test_backtest_appearances_stop_before_the_round(self):
        from fpl_agent.backtest.history import HistoryStore, PlayerGameweek

        rows = [
            PlayerGameweek(element=1, round=r, fixture=r, minutes=m, starts=1 if m >= 45 else 0)
            for r, m in [(1, 90), (2, 90), (3, 0), (4, 90)]
        ]
        store = HistoryStore(rows=rows, template={})
        before_4 = store.recent_appearances(4)[1]
        assert [a.event for a in before_4] == [1, 2, 3]
        assert [a.started for a in before_4] == [True, True, False]
        assert 1 not in store.recent_appearances(1)
