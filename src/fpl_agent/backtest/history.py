"""Point-in-time player data.

This module exists to limit **lookahead bias**.

``/bootstrap-static/`` reports season-to-date totals as of *now*. Feeding those
into a projection for gameweek 5 means projecting gameweek 5 with knowledge of
gameweek 38. A backtest built that way scores beautifully and tells you nothing.

``/element-summary/{id}/`` returns one row per player per *fixture*, including
the price that week. Summing the rows from rounds ``1..g-1`` reconstructs the
player's season-to-date statistics as they stood before gameweek ``g``.
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Iterable
from dataclasses import dataclass, field

from pydantic import BaseModel, ConfigDict

from fpl_agent.api.client import FPLClient
from fpl_agent.api.models import Element

log = logging.getLogger(__name__)

#: element-summary is one request per player, so fetch politely.
DEFAULT_CONCURRENCY = 8


#: Statistics that add up across the fixtures of a double gameweek. Price is
#: deliberately absent: it is a snapshot, not a quantity to be summed.
ADDITIVE_STATS: tuple[str, ...] = (
    "total_points",
    "minutes",
    "goals_scored",
    "assists",
    "clean_sheets",
    "bonus",
    "bps",
    "defensive_contribution",
)


class PlayerGameweek(BaseModel):
    """One player's actual return in one fixture.

    A double gameweek produces two rows with the same ``round`` and different
    ``fixture`` ids; :class:`HistoryStore` adds them up.
    """

    model_config = ConfigDict(extra="ignore", frozen=True)

    element: int
    round: int
    #: FPL fixture id. ``None`` in hand-built data: each such row then counts
    #: as a separate appearance rather than being de-duplicated.
    fixture: int | None = None
    kickoff_time: str | None = None
    total_points: int = 0
    minutes: int = 0
    goals_scored: int = 0
    assists: int = 0
    clean_sheets: int = 0
    bonus: int = 0
    bps: int = 0
    defensive_contribution: int = 0
    #: Price at that fixture, in tenths of a million. A snapshot, never summed.
    value: int = 0


@dataclass(frozen=True, slots=True)
class RoundTotals:
    """One player's whole gameweek: every fixture's statistics summed.

    ``opening_value`` is the price at the round's first fixture (what it cost at
    the deadline); ``closing_value`` is the price at its last fixture (the most
    recent price once the round is over).
    """

    stats: dict[str, int]
    opening_value: int
    closing_value: int
    fixtures: int


def _fixture_order(row: PlayerGameweek) -> tuple[str, int]:
    return (row.kickoff_time or "", row.fixture if row.fixture is not None else -1)


def _aggregate(rows: Iterable[PlayerGameweek]) -> RoundTotals:
    ordered = sorted(rows, key=_fixture_order)
    stats = {name: sum(getattr(row, name) for row in ordered) for name in ADDITIVE_STATS}
    return RoundTotals(
        stats=stats,
        opening_value=ordered[0].value,
        closing_value=ordered[-1].value,
        fixtures=len(ordered),
    )


@dataclass(slots=True)
class HistoryStore:
    """Per-fixture history for every player, aggregated per gameweek for replay.

    Parameters
    ----------
    rows:
        Every ``PlayerGameweek`` available, one per player per fixture, in any
        order. Rows sharing ``(element, fixture)`` are the same appearance and
        the later one replaces the earlier.
    template:
        The current ``Element`` for each player, used for identity fields
        (name, club, position) and ownership. These are *today's* values.
    """

    rows: list[PlayerGameweek]
    template: dict[int, Element]
    #: ``event -> element_id -> totals``, built once in __post_init__. Declared as
    #: a field because ``slots=True`` forbids attributes set outside the class body.
    _by_event: dict[int, dict[int, RoundTotals]] = field(
        init=False, repr=False, default_factory=dict
    )

    def __post_init__(self) -> None:
        grouped: dict[tuple[int, int], dict[object, PlayerGameweek]] = {}
        for index, row in enumerate(self.rows):
            # Rows without a fixture id cannot be matched to one another, so each
            # is kept; rows with one de-duplicate on (element, fixture).
            key: object = row.fixture if row.fixture is not None else ("row", index)
            grouped.setdefault((row.round, row.element), {})[key] = row

        self._by_event = {}
        for (round_id, element_id), by_fixture in grouped.items():
            self._by_event.setdefault(round_id, {})[element_id] = _aggregate(by_fixture.values())

    @property
    def events(self) -> list[int]:
        return sorted(self._by_event)

    def events_before(self, event: int) -> list[int]:
        """Gameweeks with history strictly before ``event``."""
        return [e for e in self.events if e < event]

    def fixtures_played(self, element_id: int, event: int) -> int:
        totals = self._by_event.get(event, {}).get(element_id)
        return totals.fixtures if totals else 0

    def actual(self, element_id: int, event: int, stat: str) -> int:
        """A summed statistic for the whole gameweek; 0 if the player has no row."""
        if stat not in ADDITIVE_STATS:
            raise KeyError(f"{stat!r} is not an additive statistic")
        totals = self._by_event.get(event, {}).get(element_id)
        return totals.stats[stat] if totals else 0

    def actual_points(self, element_id: int, event: int) -> int:
        """Points the player actually scored, across every fixture of the round."""
        return self.actual(element_id, event, "total_points")

    def actual_minutes(self, element_id: int, event: int) -> int:
        return self.actual(element_id, event, "minutes")

    def price_at(self, element_id: int, event: int) -> float | None:
        """Price in millions at ``event``'s deadline, or None if there is no row."""
        totals = self._by_event.get(event, {}).get(element_id)
        return totals.opening_value / 10.0 if totals else None

    def snapshot_before(self, event: int) -> list[Element]:
        """Rebuild the player pool as it was known *before* ``event``.

        Cumulative stats cover rounds strictly earlier than ``event``. Price is
        taken from the most recent earlier round.
        """
        earlier = self.events_before(event)
        if not earlier:
            # Nothing is known yet. Return the identity fields with stats zeroed
            # so callers cannot accidentally read future totals.
            return [_zeroed(element) for element in self.template.values()]

        totals: dict[int, dict[str, int]] = {}
        latest_price: dict[int, int] = {}

        for round_id in earlier:  # ascending, so the last price written is the latest
            for element_id, round_totals in self._by_event[round_id].items():
                bucket = totals.setdefault(element_id, dict.fromkeys(ADDITIVE_STATS, 0))
                for name in ADDITIVE_STATS:
                    bucket[name] += round_totals.stats[name]
                latest_price[element_id] = round_totals.closing_value

        snapshot: list[Element] = []
        for element_id, element in self.template.items():
            summed = totals.get(element_id)
            if summed is None:
                snapshot.append(_zeroed(element))
                continue
            snapshot.append(
                element.model_copy(
                    update={
                        **summed,
                        "now_cost": latest_price.get(element_id, element.now_cost),
                        # Availability flags are "now" state and cannot be
                        # reconstructed historically. Neutralise them rather
                        # than leak today's injury list into the past.
                        "status": "a",
                        "chance_of_playing_next_round": None,
                        "ep_next": None,
                        "event_points": 0,
                    }
                )
            )
        return snapshot


def _zeroed(element: Element) -> Element:
    return element.model_copy(
        update={
            "minutes": 0,
            "total_points": 0,
            "goals_scored": 0,
            "assists": 0,
            "clean_sheets": 0,
            "bonus": 0,
            "bps": 0,
            "defensive_contribution": 0,
            "event_points": 0,
            "status": "a",
            "chance_of_playing_next_round": None,
            "ep_next": None,
        }
    )


async def fetch_history(
    client: FPLClient,
    elements: list[Element],
    *,
    concurrency: int = DEFAULT_CONCURRENCY,
    ttl: int = 86_400,
) -> HistoryStore:
    """Fetch per-gameweek history for ``elements``.

    One request per player, so this is the slow part of any backtest. Results
    are cached on disk for a day by default — historical rows do not change.
    """
    semaphore = asyncio.Semaphore(concurrency)

    async def one(element: Element) -> list[PlayerGameweek]:
        async with semaphore:
            try:
                payload = await client.get_json(
                    f"/element-summary/{element.id}/", ttl=ttl, stale_ok=True
                )
            except Exception:  # noqa: BLE001 - one bad player must not kill a run
                log.warning("No history for element %s", element.id, exc_info=True)
                return []
        return [PlayerGameweek.model_validate(row) for row in payload.get("history", [])]

    batches = await asyncio.gather(*(one(element) for element in elements))
    rows = [row for batch in batches for row in batch]
    log.info("Fetched %d gameweek rows for %d players", len(rows), len(elements))
    return HistoryStore(rows=rows, template={e.id: e for e in elements})
