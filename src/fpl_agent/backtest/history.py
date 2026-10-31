"""Point-in-time player data.

This module exists to limit **lookahead bias**.

``/bootstrap-static/`` reports season-to-date totals as of *now*. Feeding those
into a projection for gameweek 5 means projecting gameweek 5 with knowledge of
gameweek 38. A backtest built that way scores beautifully and tells you nothing.

``/element-summary/{id}/`` returns one row per player per *fixture*, including
the price that week. Summing the rows from rounds ``1..g-1`` reconstructs the
player's season-to-date statistics as they stood before gameweek ``g``.

That removes the largest leak, but not every leak. Identity fields (club,
position), ownership, team strengths and the fixture-to-gameweek assignment
still come from today's data, because the public API keeps no dated copy of
them. Every store built here is therefore a **reconstructed, approximate**
replay, and says so through :attr:`HistoryStore.provenance`. A replay backed by
dated pre-deadline snapshots would need a snapshot source this project does not
have yet.
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Iterable
from dataclasses import dataclass, field, replace
from typing import Literal

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

#: Inputs a reconstructed replay takes from *today's* data. Shown with every
#: report so nobody reads a reconstructed run as lookahead-free.
RECONSTRUCTED_APPROXIMATIONS: tuple[str, ...] = (
    "club, position and name are today's (a mid-season transfer is backdated)",
    "ownership is today's selected_by_percent, so the template benchmark uses hindsight",
    "team strengths are today's ratings",
    "fixture-to-gameweek assignments are today's (postponed games appear in their new round)",
    "a player with no history row at or before a round is priced at today's cost",
    "deadline prices are read from the round's first fixture, so a change between "
    "deadline and kickoff is included",
    "availability flags cannot be reconstructed and are set to available",
)


@dataclass(frozen=True, slots=True)
class DataProvenance:
    """Where a replay's inputs came from, and what that permits it to claim.

    ``kind`` is ``"reconstructed"`` for anything built from per-fixture history
    plus today's player, team and fixture data — the only kind this project
    can currently produce. ``"dated-snapshot"`` is reserved for replays whose
    every input was captured before each deadline; no such source exists yet.
    """

    kind: Literal["reconstructed", "dated-snapshot"] = "reconstructed"
    approximations: tuple[str, ...] = RECONSTRUCTED_APPROXIMATIONS

    @property
    def is_approximate(self) -> bool:
        return self.kind != "dated-snapshot" or bool(self.approximations)

    def with_note(self, note: str) -> DataProvenance:
        """The same provenance with one more approximation declared."""
        return replace(self, approximations=(*self.approximations, note))

    def describe(self) -> str:
        if not self.is_approximate:
            return "Inputs come from dated pre-deadline snapshots."
        return (
            "Reconstructed replay, not lookahead-free. Approximate inputs: "
            + "; ".join(self.approximations)
            + "."
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

    ``opening_value`` is the price at the round's first fixture, the closest
    available read on the deadline price: all eight of a real entry's 2026/27
    transfers cost exactly this. ``closing_value`` is the price at its last
    fixture, the most recent price once the round is over.
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
        (name, club, position) and ownership. These are *today's* values; see
        :attr:`provenance`.
    provenance:
        What the replay may claim about its inputs. Defaults to a reconstructed,
        approximate replay, which is what every store in this project is.
    """

    rows: list[PlayerGameweek]
    template: dict[int, Element]
    provenance: DataProvenance = field(default_factory=DataProvenance)
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
        """Price in millions at ``event``, read from its first fixture; None if no row."""
        totals = self._by_event.get(event, {}).get(element_id)
        return totals.opening_value / 10.0 if totals else None

    def snapshot_before(self, event: int) -> list[Element]:
        """Rebuild the player pool as it was known *before* ``event``.

        Cumulative stats cover rounds strictly earlier than ``event``. Price is
        the one exception that reads ``event``'s own row: it is public before the
        deadline, and is what a transfer that week really costs. Without a row
        that week it falls back to the latest earlier price, then to today's.
        """
        earlier = self.events_before(event)
        this_round = self._by_event.get(event, {})

        def deadline_price(element: Element, latest: int | None = None) -> int:
            totals = this_round.get(element.id)
            if totals is not None:
                return totals.opening_value
            return latest if latest is not None else element.now_cost

        if not earlier:
            # Nothing is known yet. Return the identity fields with stats zeroed
            # so callers cannot accidentally read future totals.
            return [_zeroed(e, deadline_price(e)) for e in self.template.values()]

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
                snapshot.append(_zeroed(element, deadline_price(element)))
                continue
            snapshot.append(
                element.model_copy(
                    update={
                        **summed,
                        "now_cost": deadline_price(element, latest_price.get(element_id)),
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


def _zeroed(element: Element, price: int) -> Element:
    return element.model_copy(
        update={
            "now_cost": price,
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
    return HistoryStore(
        rows=rows,
        template={e.id: e for e in elements},
        provenance=DataProvenance(kind="reconstructed"),
    )
