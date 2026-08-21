from __future__ import annotations

import pytest

from fpl_agent.api.models import (
    FORMATION_BOUNDS,
    MAX_PER_CLUB,
    SQUAD_QUOTA,
    SQUAD_SIZE,
    XI_SIZE,
    Position,
)
from fpl_agent.models.expected_points import project_squad
from fpl_agent.models.fixtures import build_fixture_runs
from fpl_agent.models.minutes import MinutesModel
from fpl_agent.optimiser.squad import OptimiserInput, optimise

pytest.importorskip("pulp")


def _xp(elements, fixtures, teams, events):
    runs = build_fixture_runs(fixtures, teams, events=events)
    model = MinutesModel(gameweeks_played=3)
    projections = project_squad(elements, runs, model, events)
    return {k: v.xp for k, v in projections.items()}


def test_optimiser_builds_a_legal_squad_from_scratch(elements, fixtures, teams):
    events = [1]
    plan = optimise(
        OptimiserInput(
            elements=elements,
            xp=_xp(elements, fixtures, teams, events),
            events=events,
            current_squad=set(),
            bank=100.0,
            free_transfers=15,
            max_transfers_per_event=15,
        )
    )
    gw = plan.first
    by_id = {e.id: e for e in elements}
    squad = gw.starting_xi + gw.bench

    assert len(squad) == SQUAD_SIZE
    assert len(gw.starting_xi) == XI_SIZE

    for position, quota in SQUAD_QUOTA.items():
        assert sum(1 for p in squad if by_id[p].position == position) == quota

    for position, (lo, hi) in FORMATION_BOUNDS.items():
        n = sum(1 for p in gw.starting_xi if by_id[p].position == position)
        assert lo <= n <= hi

    for club in {by_id[p].team for p in squad}:
        assert sum(1 for p in squad if by_id[p].team == club) <= MAX_PER_CLUB

    assert sum(by_id[p].price for p in squad) <= 100.0 + 1e-6
    assert gw.captain in gw.starting_xi


def test_bench_is_ordered_keeper_first_then_by_expected_points(elements, fixtures, teams):
    events = [1]
    xp = _xp(elements, fixtures, teams, events)
    plan = optimise(
        OptimiserInput(
            elements=elements,
            xp=xp,
            events=events,
            current_squad=set(),
            bank=100.0,
            free_transfers=15,
            max_transfers_per_event=15,
        )
    )
    gw = plan.first
    by_id = {e.id: e for e in elements}

    assert by_id[gw.bench[0]].position is Position.GKP
    outfield = gw.bench[1:]
    values = [xp.get((p, 1), 0.0) for p in outfield]
    assert values == sorted(values, reverse=True), "sub 1 must be the likeliest returner"


def test_no_free_transfer_means_no_pointless_churn(elements, fixtures, teams):
    """With a settled squad and one free transfer, a marginal move is not worth -4."""
    events = [1]
    xp = _xp(elements, fixtures, teams, events)
    build = optimise(
        OptimiserInput(
            elements=elements,
            xp=xp,
            events=events,
            current_squad=set(),
            bank=100.0,
            free_transfers=15,
            max_transfers_per_event=15,
        )
    )
    settled = set(build.first.starting_xi + build.first.bench)

    plan = optimise(
        OptimiserInput(
            elements=elements,
            xp=xp,
            events=events,
            current_squad=settled,
            bank=0.0,
            free_transfers=1,
            max_transfers_per_event=2,
        )
    )
    assert plan.first.hits == 0


def test_locked_player_is_always_retained(elements, fixtures, teams):
    events = [1]
    keep = elements[0].id
    plan = optimise(
        OptimiserInput(
            elements=elements,
            xp=_xp(elements, fixtures, teams, events),
            events=events,
            current_squad=set(),
            bank=100.0,
            free_transfers=15,
            locked={keep},
            max_transfers_per_event=15,
        )
    )
    assert keep in plan.first.starting_xi + plan.first.bench


def test_horizon_spans_multiple_gameweeks(elements, fixtures, teams):
    events = [1, 2]
    plan = optimise(
        OptimiserInput(
            elements=elements,
            xp=_xp(elements, fixtures, teams, events),
            events=events,
            current_squad=set(),
            bank=100.0,
            free_transfers=15,
            max_transfers_per_event=15,
        )
    )
    assert [g.event for g in plan.gameweeks] == [1, 2]


def test_empty_horizon_is_rejected(elements):
    with pytest.raises(ValueError, match="at least one gameweek"):
        optimise(
            OptimiserInput(
                elements=elements,
                xp={},
                events=[],
                current_squad=set(),
                bank=100.0,
                free_transfers=1,
            )
        )
