from __future__ import annotations

from dataclasses import dataclass, field, replace

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
        # Going into GW3 a transfer costs GW3's first-fixture price, not a sum.
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


@dataclass
class _Spy:
    """Wraps a strategy and records what the replay handed it."""

    inner: object
    name: str = "spy"
    horizon: int = 1
    uses_present_data: tuple[str, ...] = ()
    calls: list[dict] = field(default_factory=list)

    def decide(self, **kwargs):
        self.calls.append(kwargs)
        return self.inner.decide(**kwargs)


@dataclass
class _BuildThenTemplate:
    """Build with the optimiser, then follow the template from week two.

    Ownership is static in a reconstructed replay, so a template that builds
    its own squad never needs to transfer. Starting from someone else's squad
    forces it to, which is where its transfer accounting used to vanish.
    """

    name: str = "build-then-template"
    horizon: int = 1
    uses_present_data: tuple[str, ...] = ("ownership",)

    def decide(self, **kwargs):
        if not kwargs["current_squad"]:
            return OptimiserStrategy(max_transfers=2).decide(**kwargs)
        return TemplateStrategy().decide(**kwargs)


@pytest.fixture
def owned_store(store) -> HistoryStore:
    """The same history, with ownership deliberately unrelated to returns."""
    template = {
        pid: e.model_copy(update={"selected_by_percent": f"{(pid * 37) % 97:.1f}"})
        for pid, e in store.template.items()
    }
    return HistoryStore(rows=store.rows, template=template)


class TestTransferAccounting:
    def test_template_decision_keeps_the_solvers_transfers_and_hits(self, owned_store):
        pool = owned_store.snapshot_before(2)
        built = OptimiserStrategy(max_transfers=2).decide(
            event=1,
            pool=owned_store.snapshot_before(1),
            runs={},
            current_squad=set(),
            bank=100.0,
            free_transfers=0,
            gameweeks_played=0,
        )
        decision = TemplateStrategy().decide(
            event=2,
            pool=pool,
            runs={},
            current_squad=built.squad,
            bank=0.0,
            free_transfers=1,
            gameweeks_played=1,
        )
        assert decision.transfers_in, "the template squad differs, so it must buy"
        assert set(decision.transfers_in) == decision.squad - built.squad
        assert set(decision.transfers_out) == built.squad - decision.squad
        assert decision.hits == max(0, len(decision.transfers_in) - 1)
        assert decision.captain in decision.starting_xi

    def test_template_transfers_move_hits_bank_and_free_transfers(
        self, owned_store, fixtures, teams
    ):
        result = replay(
            owned_store, _BuildThenTemplate(), events=[1, 2, 3], fixtures=fixtures, teams=teams
        )
        build, week2, week3 = result.gameweeks

        bought = sorted(week2.squad - build.squad)
        sold = sorted(build.squad - week2.squad)
        assert bought and len(bought) == len(sold)
        assert week2.transfers == len(bought)
        assert week2.free_transfers == 1
        assert week2.hits == len(bought) - 1
        assert week2.net_points == week2.points - 4 * week2.hits

        pool = {e.id: e for e in owned_store.snapshot_before(2)}
        expected_bank = round(
            build.bank + sum(pool[p].price for p in sold) - sum(pool[p].price for p in bought), 1
        )
        assert week2.bank == pytest.approx(expected_bank)
        # All free transfers used, so exactly one rolls into GW3.
        assert week3.free_transfers == 1
        assert result.total_hits == week2.hits + week3.hits

    def test_initial_build_is_not_a_transfer(self, store, fixtures, teams):
        result = replay(store, HoldStrategy(), events=[1, 2, 3], fixtures=fixtures, teams=teams)
        build, week2, week3 = result.gameweeks

        assert build.initial_build and not week2.initial_build
        assert (build.transfers, build.hits, build.free_transfers) == (0, 0, 0)
        pool = {e.id: e for e in store.snapshot_before(1)}
        assert build.bank == pytest.approx(
            round(100.0 - sum(pool[p].price for p in build.squad), 1)
        )
        # The build banks nothing; one free transfer the week after, then they roll.
        assert week2.free_transfers == 1
        assert week3.free_transfers == 2
        assert week2.bank == build.bank == week3.bank

    def test_unreported_squad_changes_are_rejected(self, owned_store, fixtures, teams):
        """The original template bug: a new squad with no transfers on record."""

        @dataclass
        class Unaccounted(_BuildThenTemplate):
            def decide(self, **kwargs):
                decision = super().decide(**kwargs)
                if not kwargs["current_squad"]:
                    return decision
                return replace(decision, transfers_in=[], transfers_out=[], hits=0)

        with pytest.raises(ValueError, match="squad changed"):
            replay(owned_store, Unaccounted(), events=[1, 2], fixtures=fixtures, teams=teams)


class TestReplayState:
    def test_replay_starting_after_gw1(self, store, fixtures, teams):
        spy = _Spy(HoldStrategy())
        result = replay(store, spy, events=[2, 3], fixtures=fixtures, teams=teams)

        # History available, not position in the requested range.
        assert [c["gameweeks_played"] for c in spy.calls] == [1, 2]
        first, second = result.gameweeks
        assert first.event == 2 and first.initial_build
        assert (first.transfers, first.hits, first.free_transfers) == (0, 0, 0)
        assert second.free_transfers == 1 and not second.initial_build
        # The pool for the GW2 build already knows GW1.
        assert sum(e.total_points for e in spy.calls[0]["pool"]) > 0

    def test_horizon_longer_than_four_gameweeks(self, elements, teams):
        from fpl_agent.api.models import Fixture

        pairs = [(1, 2), (3, 4), (5, 6)]
        fixtures = [
            Fixture(
                id=event * 10 + i,
                event=event,
                team_h=h,
                team_a=a,
                team_h_difficulty=2,
                team_a_difficulty=4,
            )
            for event in range(1, 8)
            for i, (h, a) in enumerate(pairs)
        ]
        rows = [
            PlayerGameweek(
                element=e.id, round=1, total_points=e.id % 5, minutes=90, value=e.now_cost
            )
            for e in elements
        ]
        hist = HistoryStore(rows=rows, template={e.id: e for e in elements})

        spy = _Spy(OptimiserStrategy(horizon=6, max_transfers=2), horizon=6)
        replay(hist, spy, events=[1], fixtures=fixtures, teams=teams)
        covered = {event for (_team, event) in spy.calls[0]["runs"]}
        assert covered == {1, 2, 3, 4, 5, 6}


class TestProvenanceDisclosure:
    def test_reconstructed_inputs_are_declared(self, store, fixtures, teams, elements):
        assert store.provenance.kind == "reconstructed"
        assert store.provenance.is_approximate
        assert any("ownership" in item for item in store.provenance.approximations)

        optimiser = replay(
            store, OptimiserStrategy(), events=[1, 2], fixtures=fixtures, teams=teams
        )
        template = replay(store, TemplateStrategy(), events=[1, 2], fixtures=fixtures, teams=teams)
        assert optimiser.provenance.is_approximate
        assert not optimiser.uses_hindsight
        assert template.uses_hindsight
        assert template.uses_present_data == ("ownership",)

    def test_report_never_claims_lookahead_free(self, store, fixtures, teams, elements):
        from rich.console import Console

        from fpl_agent.backtest.metrics import render_report

        by_id = {e.id: e for e in elements}
        reports = [
            compare(replay(store, s, events=[1, 2], fixtures=fixtures, teams=teams), store, by_id)
            for s in (OptimiserStrategy(), TemplateStrategy())
        ]
        console = Console(record=True, width=200)
        render_report(reports, console=console)
        text = console.export_text()

        assert "Reconstructed replay, not lookahead-free" in text
        assert "template †" in text
        assert "hindsight benchmark" in text
        assert "optimiser †" not in text

    def test_a_dated_snapshot_store_makes_no_disclaimer(self, elements):
        from fpl_agent.backtest.history import DataProvenance

        dated = DataProvenance(kind="dated-snapshot", approximations=())
        assert not dated.is_approximate
        assert "not lookahead-free" not in dated.describe()
        # Declaring the kind alone is not enough: approximations must be cleared.
        assert DataProvenance(kind="dated-snapshot").is_approximate


@dataclass
class _ScriptedSwaps:
    """Build with the optimiser, then make a scripted number of random swaps.

    Each swap replaces a player the squad started the week with by an
    affordable, unowned player in the same position, respecting the club limit.
    Hits are reported by FPL's rule from the free transfers it was given.
    """

    plan: list[int]
    seed: int
    name: str = "scripted"
    horizon: int = 1
    uses_present_data: tuple[str, ...] = ()

    def decide(self, **kw):
        import random

        from fpl_agent.backtest.strategies import Decision, _legal_xi

        if not kw["current_squad"]:
            return OptimiserStrategy().decide(**kw)
        assert kw["free_transfers"] >= 1, (
            f"GW{kw['event']}: an existing squad with {kw['free_transfers']} free transfers "
            "would make the optimiser infeasible"
        )

        rng = random.Random(self.seed * 100 + kw["event"])
        pool = {e.id: e for e in kw["pool"]}
        start = sorted(kw["current_squad"])
        squad, bank = set(start), kw["bank"]
        outs: list[int] = []
        ins: list[int] = []
        want = self.plan.pop(0)

        for pid in rng.sample(start, len(start)):
            if len(ins) == want:
                break
            out = pool[pid]
            options = [
                c
                for c in pool.values()
                if c.position is out.position
                and c.id not in squad
                and c.id not in outs
                and c.price <= out.price + bank
                and (c.team == out.team or sum(pool[p].team == c.team for p in squad) < 3)
            ]
            if not options:
                continue
            buy = rng.choice(options)
            bank += out.price - buy.price
            squad.discard(pid)
            squad.add(buy.id)
            outs.append(pid)
            ins.append(buy.id)

        xi = _legal_xi(sorted(squad), pool, {p: pool[p].total_points for p in squad})
        hits = max(0, len(ins) - kw["free_transfers"])
        return Decision(xi.squad, xi.starting_xi, xi.bench, xi.captain, ins, outs, hits)


class TestFreeTransferLedger:
    """The replay's ledger must agree with ``free_transfers_after``, the
    independently written rule that reconstructs a real entry's free
    transfers from its history (the GW4 "2 FT" fix)."""

    def test_replay_ledger_matches_the_history_rule(self):
        import random
        from collections import Counter

        from fpl_agent.api.endpoints import free_transfers_after
        from fpl_agent.api.models import EntryHistoryRow

        store, fixtures, teams, gameweeks = TestSignalDetection._universe(gameweeks=12)
        entered_with: Counter[int] = Counter()
        hit_weeks = late_builds = checked = 0

        for seed in range(200):
            rng = random.Random(seed)
            events = list(range(rng.randint(1, 6), gameweeks + 1))
            plan = [rng.choice([0, 0, 0, 1, 1, 2, 3, 6]) for _ in events[1:]]
            result = replay(
                store, _ScriptedSwaps(plan, seed), events=events, fixtures=fixtures, teams=teams
            )

            history: list[EntryHistoryRow] = []
            for gw in result.gameweeks:
                expected = free_transfers_after(history) if history else 0
                assert gw.free_transfers == expected, (seed, gw.event)
                if not gw.initial_build:
                    assert gw.hits == max(0, gw.transfers - gw.free_transfers), (seed, gw.event)
                history.append(
                    EntryHistoryRow(
                        event=gw.event,
                        points=0,
                        total_points=0,
                        event_transfers=gw.transfers,
                        event_transfers_cost=gw.hits * 4,
                    )
                )
                checked += 1
                entered_with[gw.free_transfers] += 1
                hit_weeks += gw.hits > 0
                late_builds += gw.initial_build and gw.event > 1

        # Guard against a vacuous pass: the random runs must reach the edges.
        assert checked > 1500
        assert entered_with[0] == 200, "every replay has exactly one build week"
        assert entered_with[5] > 0, "the five-transfer cap was never reached"
        assert hit_weeks > 0
        assert late_builds > 0


class TestDeadlinePrices:
    """A transfer costs this round's price, never last round's or today's."""

    @staticmethod
    def _store(element, rows):
        today = element.model_copy(update={"now_cost": 99})
        return HistoryStore(rows=rows, template={element.id: today})

    def test_this_rounds_first_fixture_wins_over_the_previous_close(self, elements):
        pid = elements[0].id
        store = self._store(
            elements[0],
            [
                PlayerGameweek(element=pid, round=1, fixture=1, value=60),
                PlayerGameweek(element=pid, round=2, fixture=2, value=61),
            ],
        )
        assert {e.id: e for e in store.snapshot_before(2)}[pid].now_cost == 61

    def test_the_opening_build_is_not_priced_at_today(self, elements):
        pid = elements[0].id
        store = self._store(
            elements[0],
            [PlayerGameweek(element=pid, round=1, fixture=1, total_points=9, value=60)],
        )
        before = {e.id: e for e in store.snapshot_before(1)}[pid]
        assert before.now_cost == 60
        assert before.total_points == 0, "the price may read the round; the stats may not"

    def test_a_blank_round_falls_back_to_the_latest_earlier_price(self, elements):
        pid = elements[0].id
        store = self._store(elements[0], [PlayerGameweek(element=pid, round=1, value=60)])
        assert {e.id: e for e in store.snapshot_before(2)}[pid].now_cost == 60

    def test_no_row_at_or_before_the_round_uses_todays_price(self, elements):
        pid = elements[0].id
        store = self._store(elements[0], [PlayerGameweek(element=pid, round=5, value=60)])
        assert {e.id: e for e in store.snapshot_before(2)}[pid].now_cost == 99

    def test_a_pool_limit_is_declared(self):
        from fpl_agent.backtest.history import DataProvenance

        noted = DataProvenance().with_note("the pool is the 150 most-owned players today")
        assert noted.approximations[-1] in noted.describe()
        assert noted.is_approximate


class TestTransferGuard:
    def test_buying_and_selling_the_same_player_is_rejected(self, owned_store, fixtures, teams):
        @dataclass
        class RoundTrip(_BuildThenTemplate):
            def decide(self, **kwargs):
                decision = super().decide(**kwargs)
                if not kwargs["current_squad"]:
                    return decision
                held = sorted(decision.squad & kwargs["current_squad"])[0]
                return replace(
                    decision,
                    transfers_in=[*decision.transfers_in, held],
                    transfers_out=[*decision.transfers_out, held],
                )

        with pytest.raises(ValueError, match="squad changed"):
            replay(owned_store, RoundTrip(), events=[1, 2], fixtures=fixtures, teams=teams)
