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
            bank_tenths=1000,
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
            bank_tenths=1000,
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
            bank_tenths=1000,
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
            bank_tenths=0,
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
            bank_tenths=1000,
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
            bank_tenths=1000,
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
                bank_tenths=1000,
                free_transfers=1,
            )
        )


class TestSellingPrices:
    def test_a_rise_is_halved_and_rounded_down_and_a_fall_is_taken_in_full(self):
        from fpl_agent.pricing import selling_price

        assert selling_price(54, 55) == 54  # +0.1 -> keeps nothing
        assert selling_price(54, 56) == 55
        assert selling_price(64, 67) == 65  # Tavernier-style +0.3 -> +0.1
        assert selling_price(60, 57) == 57

    def test_latest_buy_wins_and_free_hit_buys_do_not_count(self):
        from fpl_agent.agent.squad_state import purchase_prices

        transfers = [
            {"event": 3, "time": "2026-09-01", "element_in": 7, "element_in_cost": 50},
            {"event": 6, "time": "2026-10-01", "element_in": 7, "element_in_cost": 55},
            {"event": 8, "time": "2026-10-20", "element_in": 9, "element_in_cost": 70},
            {"event": 9, "time": "2026-10-30", "element_in": 11, "element_in_cost": 40},
        ]
        assert purchase_prices(transfers, owned={7, 9, 11}, freehit_events={8}) == {7: 55, 11: 40}

    def test_the_budget_uses_selling_prices(self, elements, fixtures, teams):
        events = [1]
        xp = _xp(elements, fixtures, teams, events)
        built = optimise(
            OptimiserInput(
                elements=elements,
                xp=xp,
                events=events,
                current_squad=set(),
                bank_tenths=1000,
                free_transfers=15,
                max_transfers_per_event=15,
            )
        ).first
        squad = set(built.starting_xi + built.bench)
        dud = built.captain
        xp = {k: (0.0 if k[0] == dud else v) for k, v in xp.items()}

        def plan(sell_price):
            return optimise(
                OptimiserInput(
                    elements=elements,
                    xp=xp,
                    events=events,
                    current_squad=squad,
                    bank_tenths=0,
                    free_transfers=1,
                    sell_price_tenths=sell_price,
                )
            ).first

        # At today's price the dud is swapped out for free.
        assert dud in plan({}).transfers_out
        # If he sells for nothing, no replacement is affordable from a £0 bank.
        assert plan({pid: 0 for pid in squad}).transfers_out == []


# ---------------------------------------------------------------------------
# Solver output is validated, never trusted (audit item 2).
# ---------------------------------------------------------------------------


def _build(elements, fixtures, teams, events=(1,)):
    events = list(events)
    xp = _xp(elements, fixtures, teams, events)
    spec = OptimiserInput(
        elements=elements,
        xp=xp,
        events=events,
        current_squad=set(),
        bank_tenths=1000,
        free_transfers=15,
        max_transfers_per_event=15,
    )
    return spec, optimise(spec)


class TestSolverOutcomes:
    def test_a_time_limited_incumbent_is_validated_and_labelled(
        self, elements, fixtures, teams, monkeypatch
    ):
        import pulp

        real_solve = pulp.LpProblem.solve

        def timed_out(self, solver=None, **kw):
            real_solve(self, solver, **kw)
            self.status = pulp.LpStatusNotSolved
            self.sol_status = pulp.LpSolutionIntegerFeasible
            return self.status

        monkeypatch.setattr(pulp.LpProblem, "solve", timed_out)
        _, plan = _build(elements, fixtures, teams)
        assert not plan.proven_optimal
        assert plan.quality == "feasible, not proven optimal"

    def test_no_incumbent_is_a_clear_failure(self, elements, fixtures, teams, monkeypatch):
        import pulp

        from fpl_agent.optimiser.squad import SolverError

        real_solve = pulp.LpProblem.solve

        def nothing(self, solver=None, **kw):
            real_solve(self, solver, **kw)
            self.status = pulp.LpStatusNotSolved
            self.sol_status = pulp.LpSolutionNoSolutionFound
            return self.status

        monkeypatch.setattr(pulp.LpProblem, "solve", nothing)
        with pytest.raises(SolverError, match="no usable plan"):
            _build(elements, fixtures, teams)

    def test_a_fractional_value_is_rejected_not_rounded(
        self, elements, fixtures, teams, monkeypatch
    ):
        import pulp

        from fpl_agent.optimiser.squad import SolverError

        real_solve = pulp.LpProblem.solve

        def fractional(self, solver=None, **kw):
            real_solve(self, solver, **kw)
            var = next(v for v in self.variables() if v.name.startswith("start_"))
            var.varValue = 0.5
            return self.status

        monkeypatch.setattr(pulp.LpProblem, "solve", fractional)
        with pytest.raises(SolverError, match="fractional"):
            _build(elements, fixtures, teams)

    def test_an_infeasible_model_is_a_clear_failure(self, elements, fixtures, teams):
        from fpl_agent.optimiser.squad import SolverError

        events = [1]
        xp = _xp(elements, fixtures, teams, events)
        four_from_one_club = {e.id for e in elements if e.team == 1 and e.element_type == 2}
        with pytest.raises(SolverError):
            optimise(
                OptimiserInput(
                    elements=elements,
                    xp=xp,
                    events=events,
                    current_squad=set(),
                    bank_tenths=1000,
                    free_transfers=15,
                    max_transfers_per_event=15,
                    locked=set(list(four_from_one_club)[:4]),
                )
            )


class TestValidatePlan:
    def test_a_solved_plan_is_valid(self, elements, fixtures, teams):
        from fpl_agent.optimiser.squad import validate_plan

        spec, plan = _build(elements, fixtures, teams)
        assert validate_plan(spec, plan) == []
        gw = plan.first
        assert gw.vice_captain in gw.starting_xi and gw.vice_captain != gw.captain

    @pytest.mark.parametrize(
        "mutation, expected",
        [
            (lambda gw, by: {"captain": gw.bench[-1]}, "captain is not starting"),
            (lambda gw, by: {"vice_captain": gw.captain}, "vice-captain"),
            (lambda gw, by: {"bank_tenths": gw.bank_tenths + 1}, "bank"),
            (lambda gw, by: {"hits": 1}, "hits"),
            (
                lambda gw, by: {"bench": gw.bench[:-1] + [gw.starting_xi[0]]},
                "squad",
            ),
        ],
    )
    def test_each_broken_rule_is_reported(self, elements, fixtures, teams, mutation, expected):
        from dataclasses import replace

        from fpl_agent.optimiser.squad import validate_plan

        spec, plan = _build(elements, fixtures, teams)
        by = {e.id: e for e in elements}
        broken = replace(plan, gameweeks=[replace(plan.first, **mutation(plan.first, by))])
        problems = validate_plan(spec, broken)
        assert any(expected in p for p in problems), problems

    def test_over_budget_is_reported(self, elements, fixtures, teams):
        from dataclasses import replace

        from fpl_agent.optimiser.squad import validate_plan

        spec, plan = _build(elements, fixtures, teams)
        assert any("bank goes to" in p for p in validate_plan(replace(spec, bank_tenths=100), plan))


class TestHoldingPrices:
    """Each holding sells at its own price: an original at its selling price,
    a rebuy at what was paid for it (audit item 4)."""

    def _swap(self, gw, out_id, in_id, by_id):
        from dataclasses import replace

        def sub(ids):
            return [in_id if p == out_id else p for p in ids]

        return replace(
            gw,
            starting_xi=sub(gw.starting_xi),
            bench=sub(gw.bench),
            captain=in_id if gw.captain == out_id else gw.captain,
            vice_captain=in_id if gw.vice_captain == out_id else gw.vice_captain,
        )

    def test_sell_rebuy_sell_credits_the_rebuy_price(self, elements, fixtures, teams):
        from collections import Counter
        from dataclasses import replace

        from fpl_agent.optimiser.squad import TransferPlan, validate_plan

        _, built = _build(elements, fixtures, teams)
        base = built.first
        squad = base.squad
        by_id = {e.id: e for e in elements}
        clubs = Counter(by_id[p].team for p in squad)

        # x: an outfield player who was bought cheaper and has risen (sells 2
        # below his price); y: a same-position player the squad can take.
        def replacement(x: int) -> int | None:
            return next(
                (
                    e.id
                    for e in sorted(elements, key=lambda e: e.now_cost)
                    if e.element_type == by_id[x].element_type
                    and e.id not in squad
                    and (e.team == by_id[x].team or clubs[e.team] < 3)
                ),
                None,
            )

        x = next(
            p
            for p in sorted(squad, key=lambda p: -by_id[p].now_cost)
            if by_id[p].element_type != 1 and replacement(p) is not None
        )
        y = replacement(x)
        assert y is not None
        sp_x = by_id[x].now_cost - 2
        spec = OptimiserInput(
            elements=elements,
            xp={},
            events=[1, 2, 3],
            current_squad=squad,
            bank_tenths=100,
            free_transfers=1,
            sell_price_tenths={x: sp_x},
        )
        out1 = self._swap(base, x, y, by_id)
        back = self._swap(out1, y, x, by_id)
        out3 = self._swap(back, x, y, by_id)
        now_x, now_y = by_id[x].now_cost, by_id[y].now_cost
        bank1 = 100 + sp_x - now_y  # the original holding sells at its selling price
        bank2 = bank1 + now_y - now_x  # x bought back at today's price
        bank3 = bank2 + now_x - now_y  # the rebuy sells at what was paid for it

        def gw(plan_gw, event, ins, outs, bank):
            return replace(
                plan_gw,
                event=event,
                transfers_in=ins,
                transfers_out=outs,
                hits=0,
                free_transfers=1,
                bank_tenths=bank,
            )

        plan = TransferPlan(
            gameweeks=[
                gw(out1, 1, [y], [x], bank1),
                gw(back, 2, [x], [y], bank2),
                gw(out3, 3, [y], [x], bank3),
            ],
            objective=0.0,
            status="Optimal",
        )
        assert validate_plan(spec, plan) == []
        # Round trip x -> y -> x -> y: only the first sale loses FPL's share
        # of the rise; the rebuy sells for exactly what it cost.
        assert bank3 == bank1
        # Pricing the third sale at the original holding's selling price is the bug.
        wrong = replace(
            plan, gameweeks=[*plan.gameweeks[:2], replace(plan.gameweeks[2], bank_tenths=bank3 - 2)]
        )
        assert any("GW3" in p and "plan says bank" in p for p in validate_plan(spec, wrong))

    def test_a_selling_price_above_todays_price_is_rejected(self, elements, fixtures, teams):
        _, built = _build(elements, fixtures, teams)
        squad = built.first.squad
        pid = next(iter(squad))
        by_id = {e.id: e for e in elements}
        with pytest.raises(ValueError, match="above today's price"):
            optimise(
                OptimiserInput(
                    elements=elements,
                    xp={},
                    events=[2],
                    current_squad=squad,
                    bank_tenths=0,
                    free_transfers=1,
                    sell_price_tenths={pid: by_id[pid].now_cost + 1},
                )
            )

    def test_the_solver_keeps_an_unaffordable_riser(self, elements, fixtures, teams):
        """A riser sells for less than his price; the solver must not spend
        money it would not get."""
        _, built = _build(elements, fixtures, teams)
        squad = built.first.squad
        by_id = {e.id: e for e in elements}
        x = next(p for p in sorted(squad) if by_id[p].element_type == 3)
        z = next(
            e
            for e in elements
            if e.element_type == 3 and e.id not in squad and e.team == by_id[x].team
        )
        xp = {(p, 2): 1.0 for p in squad}
        xp[(z.id, 2)] = 50.0
        xp[(x, 2)] = 0.0

        def plan(sp):
            return optimise(
                OptimiserInput(
                    elements=elements,
                    xp=xp,
                    events=[2],
                    current_squad=squad,
                    bank_tenths=z.now_cost - by_id[x].now_cost,
                    free_transfers=1,
                    max_transfers_per_event=1,
                    sell_price_tenths={x: sp},
                )
            ).first

        assert z.id in plan(by_id[x].now_cost).transfers_in
        assert z.id not in plan(by_id[x].now_cost - 1).transfers_in


class TestFirstWeekState:
    def _dud_squad(self, elements, fixtures, teams):
        _, built = _build(elements, fixtures, teams)
        squad = built.first.squad
        xp = {(p, 2): 1.0 for p in squad}
        for e in elements:
            if e.id not in squad and e.element_type == 3:
                xp[(e.id, 2)] = 20.0  # every unowned midfielder is far better
        return squad, xp

    def test_with_no_free_transfers_left_a_move_costs_a_hit(self, elements, fixtures, teams):
        squad, xp = self._dud_squad(elements, fixtures, teams)
        plan = optimise(
            OptimiserInput(
                elements=elements,
                xp=xp,
                events=[2],
                current_squad=squad,
                bank_tenths=200,
                free_transfers=0,
                max_transfers_per_event=1,
            )
        ).first
        assert plan.free_transfers == 0
        assert plan.hits == len(plan.transfers_in) == 1

    def test_a_wildcard_week_is_unlimited_and_keeps_the_bank_of_transfers(
        self, elements, fixtures, teams
    ):
        squad, xp = self._dud_squad(elements, fixtures, teams)
        plan = optimise(
            OptimiserInput(
                elements=elements,
                xp=xp,
                events=[2, 3],
                current_squad=squad,
                bank_tenths=200,
                free_transfers=2,
                max_transfers_per_event=1,
                unlimited_first_week=True,
            )
        )
        first, second = plan.gameweeks
        assert len(first.transfers_in) > 1 and first.hits == 0
        assert second.free_transfers == 3
