"""One consistent squad state for the planner (audit item 1)."""

from __future__ import annotations

from datetime import UTC, datetime

import pytest

from fpl_agent.agent.squad_state import (
    SquadStateError,
    load_snapshot,
    purchase_prices,
    reconstruct,
)
from fpl_agent.api.models import Element, EntryHistoryRow

DEADLINE_10 = datetime(2026, 11, 6, 18, 30, tzinfo=UTC)


def _history(*transfers: int) -> list[EntryHistoryRow]:
    return [
        EntryHistoryRow(event=i + 1, points=0, total_points=0, event_transfers=n)
        for i, n in enumerate(transfers)
    ]


def _transfer(event, out_id, in_id, out_cost, in_cost, time="2026-11-08T10:00:00Z"):
    return {
        "event": event,
        "element_out": out_id,
        "element_in": in_id,
        "element_out_cost": out_cost,
        "element_in_cost": in_cost,
        "time": time,
    }


def _state(transfers=(), chips=None, history=None, first=None, now=None):
    picks = list(range(1, 16))
    now_cost = {pid: 50 for pid in range(1, 30)}
    now_cost.update(now or {})
    return reconstruct(
        source_event=10,
        source_deadline=DEADLINE_10,
        for_event=11,
        picks=picks,
        source_bank=20,
        # One transfer every week after the first: exactly one free for GW11.
        history=history if history is not None else _history(0, 1, 1, 1, 1, 1, 1, 1, 1, 1),
        chips=chips or {},
        transfers=list(transfers),
        first_squad_price=first if first is not None else {pid: 50 for pid in picks},
        now_cost=now_cost,
    )


class TestPublicReconstruction:
    def test_transfers_after_the_locked_deadline_are_applied_in_time_order(self):
        state = _state(
            [
                _transfer(11, 2, 20, 48, 55, "2026-11-08T10:00:00Z"),
                _transfer(11, 20, 21, 55, 50, "2026-11-09T09:00:00Z"),
                _transfer(10, 1, 3, 50, 50, "2026-11-05T09:00:00Z"),  # before: ignored
            ]
        )
        assert 2 not in state.squad and 20 not in state.squad and 21 in state.squad
        assert state.bank_tenths == 20 + 48 - 55 + 55 - 50
        assert len(state.pending) == 2
        assert state.effective_at == datetime(2026, 11, 9, 9, 0, tzinfo=UTC)
        # One free transfer for GW11, two used: one hit already committed.
        assert state.free_transfers == 0 and state.pending_hits == 1
        assert state.source == "public" and state.source_event == 10

    def test_no_later_transfers_leaves_the_locked_state(self):
        state = _state()
        assert state.squad == frozenset(range(1, 16)) and state.bank_tenths == 20
        assert state.effective_at == DEADLINE_10 and state.pending == ()

    def test_a_free_hit_is_temporary_and_a_wildcard_is_free(self):
        free_hit = _state([_transfer(11, 2, 20, 50, 50)], chips={11: "freehit"})
        assert 2 in free_hit.squad and free_hit.pending == ()
        assert any("Free Hit" in u for u in free_hit.uncertain)
        wildcard = _state(
            [_transfer(11, 2, 20, 50, 50), _transfer(11, 3, 21, 50, 50)], chips={11: "wildcard"}
        )
        assert wildcard.pending_hits == 0 and wildcard.unlimited_week
        assert wildcard.free_transfers == _state().free_transfers

    def test_hidden_chip_and_pre_deadline_transfers_are_named_as_uncertain(self):
        uncertain = " ".join(_state().uncertain)
        assert "hide transfers" in uncertain and "chip" in uncertain

    def test_selling_prices_from_purchases_with_estimates_named(self):
        state = _state(
            [_transfer(11, 2, 20, 50, 46)],
            first={pid: 46 for pid in range(1, 15)},  # 15 has no known purchase
            now={20: 49, 1: 49, 15: 60},
        )
        assert state.selling_tenths[20] == 47  # bought 4.6, now 4.9: keeps 0.1 of 0.3
        assert state.selling_tenths[1] == 47
        assert state.selling_tenths[15] == 60 and state.estimated == {15}
        assert not state.exact_prices

    def test_a_transfer_that_does_not_follow_is_an_error_not_a_guess(self):
        with pytest.raises(SquadStateError, match="does not follow"):
            _state([_transfer(11, 99, 20, 50, 50)])

    def test_purchase_prices_latest_buy_wins_free_hit_excluded(self):
        transfers = [
            _transfer(3, 1, 7, 50, 50, "2026-09-01"),
            _transfer(6, 2, 7, 50, 55, "2026-10-01"),
            _transfer(8, 3, 9, 50, 70, "2026-10-20"),
        ]
        assert purchase_prices(transfers, {7, 9}, freehit_events={8}) == {7: 55}


def _elements() -> dict[int, Element]:
    out = {}
    pid = 1
    for team in range(1, 6):
        for element_type in (1, 2, 2, 3, 3, 4):
            out[pid] = Element(
                id=pid, web_name=f"P{pid}", team=team, element_type=element_type, now_cost=50
            )
            pid += 1
    return out


def _snapshot(**overrides):
    els = _elements()

    def first(element_type: int, team: int) -> int:
        return next(e.id for e in els.values() if e.element_type == element_type and e.team == team)

    # Three per club: keepers from clubs 1-2, one defender and one midfielder
    # from each club, forwards from clubs 3-5.
    squad = (
        [first(1, t) for t in (1, 2)]
        + [first(2, t) for t in range(1, 6)]
        + [first(3, t) for t in range(1, 6)]
        + [first(4, t) for t in (3, 4, 5)]
    )
    raw = {
        "entry_id": 1,
        "event": 11,
        "as_of": "2026-11-15T10:00:00+08:00",
        "bank": 23,
        "free_transfers": 1,
        "chip": None,
        "squad": [{"element": p, "selling_price": 49} for p in squad],
    }
    raw.update(overrides)
    return raw, els


class TestSnapshot:
    def _load(self, raw, els):
        return load_snapshot(raw, entry_id=1, for_event=11, last_deadline=DEADLINE_10, elements=els)

    def test_a_valid_snapshot_replaces_the_public_state(self):
        raw, els = _snapshot()
        state = self._load(raw, els)
        assert state is not None and state.source == "snapshot"
        assert state.bank_tenths == 23 and state.free_transfers == 1 and state.exact_prices

    def test_purchase_price_alone_is_turned_into_a_selling_price(self):
        raw, els = _snapshot()
        raw["squad"] = [{"element": e["element"], "purchase_price": 46} for e in raw["squad"]]
        els = {pid: e.model_copy(update={"now_cost": 49}) for pid, e in els.items()}
        state = self._load(raw, els)
        assert state is not None and set(state.selling_tenths.values()) == {47}

    @pytest.mark.parametrize(
        "field, value", [("event", 10), ("as_of", "2026-11-06T10:00:00+00:00")]
    )
    def test_one_for_another_week_or_before_the_deadline_no_longer_applies(self, field, value):
        raw, els = _snapshot(**{field: value})
        assert self._load(raw, els) is None

    @pytest.mark.parametrize(
        "change, message",
        [
            ({"entry_id": 2}, "entry_id"),
            ({"bank": -1}, "bank"),
            ({"free_transfers": 6}, "free_transfers"),
            ({"chip": "bboost"}, "chip"),
            ({"as_of": "2026-11-15T10:00:00"}, "timezone"),
        ],
    )
    def test_malformed_fields_are_errors(self, change, message):
        raw, els = _snapshot(**change)
        with pytest.raises(SquadStateError, match=message):
            self._load(raw, els)

    def test_squad_problems_are_errors(self):
        raw, els = _snapshot()
        raw["squad"][0]["selling_price"] = 51  # above today's 50
        raw["squad"][1] = {"element": 999, "selling_price": 50}
        raw["squad"].append(dict(raw["squad"][2]))
        with pytest.raises(SquadStateError) as info:
            self._load(raw, els)
        text = str(info.value)
        assert "above today's price" in text and "unknown player 999" in text
        assert "listed twice" in text


class TestFirstWeekRules:
    """The optimiser side: a week can start with no free transfers left, and a
    wildcard week is unlimited and keeps the banked ones."""

    def test_ledger(self):
        from fpl_agent.optimiser.squad import transfer_ledger

        assert transfer_ledger(False, 0, [1, 0, 1]) == [(0, 1), (1, 0), (2, 0)]
        assert transfer_ledger(False, 2, [6, 1], unlimited_first_week=True) == [(2, 0), (3, 0)]
        assert transfer_ledger(True, 0, [15, 1]) == [(0, 0), (1, 0)]
