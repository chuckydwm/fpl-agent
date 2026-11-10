"""Dated prediction capture and evaluation (audit item 8)."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from fpl_agent.agent import predictions
from fpl_agent.models.expected_points import ExpectedPoints

DEADLINE = datetime(2026, 11, 21, 11, 0, tzinfo=UTC)


def _proj(pid, event, xp, p_start, p_60, p_appear=1.0, minutes=80.0):
    return ExpectedPoints(
        pid, event, xp, p_start, "test", p_appear=p_appear, p_60=p_60, expected_minutes=minutes
    )


class TestCapture:
    def test_only_the_requested_gameweek_is_captured_with_its_time(self):
        rows = predictions.capture_rows(
            {(1, 11): _proj(1, 11, 5.0, 0.9, 0.8), (1, 12): _proj(1, 12, 4.0, 0.9, 0.8)},
            11,
            DEADLINE - timedelta(hours=3),
        )
        assert len(rows) == 1 and rows[0]["xp"] == 5.0
        assert rows[0]["captured_at"] == (DEADLINE - timedelta(hours=3)).isoformat()

    def test_the_last_capture_before_the_deadline_is_the_one_scored(self, tmp_path):
        path = tmp_path / "predictions.jsonl"
        for hours, xp in ((24, 4.0), (3, 6.0), (-1, 9.0)):  # -1: after the deadline
            predictions.append(
                path,
                predictions.capture_rows(
                    {(1, 11): _proj(1, 11, xp, 0.9, 0.8)}, 11, DEADLINE - timedelta(hours=hours)
                ),
            )
        chosen = predictions.latest_before(path, 11, DEADLINE)
        assert chosen[1]["xp"] == 6.0


class TestEvaluate:
    def test_errors_brier_scores_and_calibration(self):
        captured = {
            1: {
                "xp": 6.0,
                "p_appear": 1.0,
                "p_start": 0.9,
                "p_60": 0.8,
                "expected_minutes": 80.0,
                "captured_at": "2026-11-21T08:00:00+00:00",
            },
            2: {
                "xp": 2.0,
                "p_appear": 0.5,
                "p_start": 0.3,
                "p_60": 0.2,
                "expected_minutes": 30.0,
                "captured_at": "2026-11-21T08:00:00+00:00",
            },
            3: {
                "xp": 0.0,
                "p_appear": 0.0,
                "p_start": 0.0,
                "p_60": 0.0,
                "expected_minutes": 0.0,
                "captured_at": "2026-11-21T08:00:00+00:00",
            },
        }
        outcomes = {
            1: {"total_points": 2, "minutes": 90, "starts": 1},
            2: {"total_points": 1, "minutes": 0, "starts": 0},
            3: {"total_points": 0, "minutes": 0, "starts": 0},
        }
        result = predictions.evaluate(11, captured, outcomes)
        assert result is not None
        assert result.players == 2, "a player given no chance of appearing is not scored"
        assert result.points_mae == pytest.approx((4 + 1) / 2)
        assert result.points_bias == pytest.approx((4 + 1) / 2)
        assert result.minutes_mae == pytest.approx((10 + 30) / 2)
        assert result.start_brier == pytest.approx((0.1**2 + 0.3**2) / 2)
        assert result.sixty_brier == pytest.approx((0.2**2 + 0.2**2) / 2)
        assert [(round(low, 1), n) for low, _, _, _, n in result.calibration] == [
            (0.2, 1),
            (0.8, 1),
        ]
        assert "Brier" in "\n".join(result.describe())

    def test_nothing_to_score(self):
        assert predictions.evaluate(11, {}, {1: {"total_points": 3}}) is None


class TestNoLeakInReplay:
    """A decision before round 3 must not change when round 3's own minutes do."""

    def test_round_three_minutes_do_not_reach_the_round_three_decision(
        self, elements, fixtures, teams
    ):
        from fpl_agent.backtest.history import HistoryStore, PlayerGameweek

        target = elements[5].id

        def store(round_three_minutes: int) -> HistoryStore:
            rows = [
                PlayerGameweek(
                    element=e.id,
                    round=r,
                    fixture=r * 100 + e.team,
                    minutes=round_three_minutes if (e.id == target and r == 3) else 90,
                    total_points=2,
                    value=e.now_cost,
                )
                for e in elements
                for r in (1, 2, 3)
            ]
            return HistoryStore(rows=rows, template={e.id: e for e in elements})

        from fpl_agent.models.minutes import MinutesModel

        outlooks = []
        for minutes in (0, 90):
            s = store(minutes)
            model = MinutesModel(gameweeks_played=2, recent=dict(s.recent_appearances(3)))
            player = {e.id: e for e in s.snapshot_before(3)}[target]
            prob = model.probability(player)
            outlooks.append((prob.p_start, prob.p_60, prob.reason))
        assert outlooks[0] == outlooks[1]
