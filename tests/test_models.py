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
        prob = model.probability(_element(minutes=450, chance_of_playing_next_round=50))
        assert 0.0 < prob.p_start < 0.5
        assert "50%" in prob.reason

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


class TestFreeTransfers:
    def test_unused_transfers_roll(self):
        history = [EntryHistoryRow(event=g, points=0, total_points=0) for g in range(1, 4)]
        assert free_transfers_after(history) == 4

    def test_rollover_is_capped_at_five(self):
        history = [EntryHistoryRow(event=g, points=0, total_points=0) for g in range(1, 12)]
        assert free_transfers_after(history) == 5

    def test_taking_hits_does_not_push_below_one(self):
        history = [EntryHistoryRow(event=1, points=0, total_points=0, event_transfers=6)]
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
