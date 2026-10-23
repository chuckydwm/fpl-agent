"""Typed endpoint wrappers.

Each function parses the raw payload into the models in :mod:`fpl_agent.api.models`
so nothing downstream indexes into raw dictionaries.
"""

from __future__ import annotations

from fpl_agent.api.client import FPLClient
from fpl_agent.api.models import Bootstrap, Entry, EntryHistoryRow, Fixture, Pick


async def get_bootstrap(client: FPLClient, *, ttl: int | None = None) -> Bootstrap:
    """Players, teams and the gameweek calendar. The spine of everything."""
    return Bootstrap.model_validate(await client.get_json("/bootstrap-static/", ttl=ttl))


async def get_fixtures(
    client: FPLClient, *, event: int | None = None, ttl: int | None = None
) -> list[Fixture]:
    path = "/fixtures/" if event is None else f"/fixtures/?event={event}"
    payload = await client.get_json(path, ttl=ttl)
    return [Fixture.model_validate(row) for row in payload]


async def get_entry(client: FPLClient, entry_id: int, *, ttl: int | None = None) -> Entry:
    return Entry.model_validate(await client.get_json(f"/entry/{entry_id}/", ttl=ttl))


async def get_entry_history(
    client: FPLClient, entry_id: int, *, ttl: int | None = None
) -> list[EntryHistoryRow]:
    payload = await client.get_json(f"/entry/{entry_id}/history/", ttl=ttl)
    return [EntryHistoryRow.model_validate(row) for row in payload.get("current", [])]


async def get_chips_used(client: FPLClient, entry_id: int, *, ttl: int | None = None) -> list[str]:
    payload = await client.get_json(f"/entry/{entry_id}/history/", ttl=ttl)
    return [c["name"] for c in payload.get("chips", [])]


async def get_chip_events(
    client: FPLClient, entry_id: int, *, ttl: int | None = None
) -> dict[int, str]:
    """Gameweek -> chip name, for the chips this entry has played."""
    payload = await client.get_json(f"/entry/{entry_id}/history/", ttl=ttl)
    return {int(c["event"]): str(c["name"]) for c in payload.get("chips", [])}


#: Chips that make every transfer free that week. Banked transfers are not
#: spent, and the usual +1 is still granted for the following gameweek.
UNLIMITED_TRANSFER_CHIPS = frozenset({"wildcard", "freehit"})


async def get_picks(
    client: FPLClient, entry_id: int, event: int, *, ttl: int | None = None
) -> list[Pick]:
    """The 15 picks for a gameweek, in slot order (12-15 is bench order)."""
    payload = await client.get_json(f"/entry/{entry_id}/event/{event}/picks/", ttl=ttl)
    return [Pick.model_validate(row) for row in payload.get("picks", [])]


def free_transfers_after(
    history: list[EntryHistoryRow],
    *,
    cap: int = 5,
    chip_events: dict[int, str] | None = None,
) -> int:
    """Reconstruct free transfers available for the next gameweek.

    FPL grants one transfer per gameweek and banks up to ``cap``. Transfers
    beyond the free allowance cost points but do not push the balance below one.
    The API never exposes this directly, so it has to be replayed from history.

    Start from zero, not one: changes before the first deadline are free and
    nothing banks out of that week, so the first row only grants the single
    transfer you get afterwards. Starting at one invented a second transfer —
    the same bug that told the GW4 desk "2 FT" when the entry had 1 and cost a
    four-point hit. ``chip_events`` marks wildcard / free-hit weeks, where no
    banked transfer is spent but the +1 is still granted.
    """
    chips = chip_events or {}
    available = 0
    for row in history:
        if chips.get(row.event) in UNLIMITED_TRANSFER_CHIPS:
            used_free = 0
        else:
            used_free = min(row.event_transfers, available)
        available = min(cap, available - used_free + 1)
    return available
