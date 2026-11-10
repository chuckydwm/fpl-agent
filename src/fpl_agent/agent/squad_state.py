"""One consistent picture of the squad the next deadline will judge.

The public API gives pieces from different moments: picks, bank and free
transfers as of the last locked gameweek, and a transfer list that can run
past it. Mixing them silently produces a plan for a squad that never existed.
This module builds a single state and says where every part came from:

1. **Source**: the picks and bank of the last locked gameweek (``source_event``),
   as at its deadline.
2. **Pending transfers**: public transfers for later gameweeks, applied in
   time order. Each moves the squad and the bank (the API records the price
   paid and the selling price received) and uses a free transfer; any beyond
   the free ones were hits, already committed.
3. **Chips**: a Free Hit is temporary, so its transfers are left out and the
   plan starts from the squad that comes back. A Wildcard makes the coming
   week unlimited. A chip set for the coming week is not always visible
   publicly; that is listed as uncertain.
4. **Selling prices**: purchase price from the latest non-Free-Hit buy, else
   the first-squad price; anything not found is an estimate at today's price
   and is named as such.

FPL may hide transfers made for the coming week until its deadline passes,
so a public state can miss them. A **manager-confirmed snapshot** (a local
JSON file) settles that: when it is valid and newer than the last deadline,
it replaces the public state entirely, never field by field. An invalid
snapshot is an error, not a silent fallback.

Snapshot format (``FPL_SQUAD_SNAPSHOT``, default ``<config>/squad.json``)::

    {
      "entry_id": 8166161,
      "event": 11,                       # the coming gameweek it describes
      "as_of": "2026-11-15T10:00:00+08:00",
      "bank": 23,                        # tenths of a million
      "free_transfers": 1,
      "chip": null,                      # "wildcard", "freehit" or null
      "squad": [{"element": 1, "selling_price": 62, "purchase_price": 60}, ...]
    }

Each squad entry needs ``selling_price`` or ``purchase_price`` (the selling
price is then computed); ``selling_price`` is what the transfer screen shows.
"""

from __future__ import annotations

import json
import os
from collections import Counter
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any

from platformdirs import user_config_dir

from fpl_agent.api.endpoints import UNLIMITED_TRANSFER_CHIPS, free_transfers_after
from fpl_agent.api.models import (
    MAX_FREE_TRANSFERS,
    MAX_PER_CLUB,
    SQUAD_QUOTA,
    SQUAD_SIZE,
    Element,
    EntryHistoryRow,
)
from fpl_agent.pricing import fmt_money, selling_price

ENV_SNAPSHOT = "FPL_SQUAD_SNAPSHOT"


class SquadStateError(ValueError):
    """The squad state cannot be established reliably (e.g. a bad snapshot)."""


@dataclass(frozen=True, slots=True)
class PendingTransfer:
    event: int
    time: str
    element_in: int
    element_out: int
    #: Paid for the incoming player and received for the outgoing one, tenths.
    cost_in: int
    received_out: int


@dataclass(frozen=True, slots=True)
class SquadState:
    """The squad, money and transfers going into ``for_event``."""

    source: str  # "public" or "snapshot"
    #: The locked gameweek whose picks the state starts from (public), or the
    #: snapshot's gameweek.
    source_event: int
    for_event: int
    effective_at: datetime
    squad: frozenset[int]
    bank_tenths: int
    #: Free transfers left for ``for_event`` after the pending ones.
    free_transfers: int
    #: ``element_id -> tenths`` FPL would pay today.
    selling_tenths: dict[int, int]
    pending: tuple[PendingTransfer, ...] = ()
    #: Hits already committed by pending transfers beyond the free ones.
    pending_hits: int = 0
    chip: str | None = None
    #: Players whose selling price is an estimate (purchase price not found).
    estimated: frozenset[int] = field(default_factory=frozenset)
    uncertain: tuple[str, ...] = ()

    @property
    def unlimited_week(self) -> bool:
        return self.chip == "wildcard"

    @property
    def exact_prices(self) -> bool:
        return not self.estimated

    def describe(self, names: dict[int, str], tz: Any = None) -> list[str]:
        """Plain lines for the CLI and the brief: source, time, pending, doubts."""
        when = self.effective_at.astimezone(tz) if tz else self.effective_at
        if self.source == "snapshot":
            head = f"Squad state: your snapshot for GW{self.for_event}, as of {when:%a %d %b %H:%M}"
        else:
            head = (
                f"Squad state: GW{self.source_event} picks"
                + (f" + {len(self.pending)} later transfer(s)" if self.pending else "")
                + f", as of {when:%a %d %b %H:%M}"
            )
        lines = [
            f"{head}. Bank {fmt_money(self.bank_tenths)}, "
            f"{self.free_transfers} free transfer(s) left for GW{self.for_event}"
            + (f", {self.pending_hits} hit(s) already taken" if self.pending_hits else "")
            + (f", {self.chip} active" if self.chip else "")
            + "."
        ]
        for p in self.pending:
            lines.append(
                f"  counted: {names.get(p.element_out, p.element_out)} -> "
                f"{names.get(p.element_in, p.element_in)} (GW{p.event}, {p.time[:16]})"
            )
        if self.estimated:
            who = ", ".join(sorted(names.get(p, str(p)) for p in self.estimated))
            lines.append(f"  estimated selling price (today's price used): {who}")
        lines.extend(f"  uncertain: {u}" for u in self.uncertain)
        return lines


# --- public reconstruction ---------------------------------------------------


def purchase_prices(
    transfers: list[dict[str, Any]], owned: set[int], freehit_events: set[int]
) -> dict[int, int]:
    """Tenths of £m paid for each owned player that came in by transfer.

    The latest buy wins (a player sold and bought back is priced at the
    rebuy). Free-hit buys revert after the week, so they never count."""
    out: dict[int, int] = {}
    for t in sorted(transfers, key=lambda t: str(t.get("time", ""))):
        if int(t["event"]) in freehit_events:
            continue
        if int(t["element_in"]) in owned:
            out[int(t["element_in"])] = int(t["element_in_cost"])
    return out


def reconstruct(
    *,
    source_event: int,
    source_deadline: datetime,
    for_event: int,
    picks: list[int],
    source_bank: int,
    history: list[EntryHistoryRow],
    chips: dict[int, str],
    transfers: list[dict[str, Any]],
    first_squad_price: dict[int, int],
    now_cost: dict[int, int],
) -> SquadState:
    """The public state going into ``for_event``. Pure.

    ``picks`` and ``source_bank`` are as at ``source_event``'s deadline;
    ``history`` covers gameweeks up to and including it. ``first_squad_price``
    holds the opening price of players who may have been in the first squad.
    """
    uncertain = [
        f"FPL may hide transfers for GW{for_event} until its deadline; "
        "use a squad snapshot if you have made any",
    ]
    chip = chips.get(for_event)
    if chip is None:
        uncertain.append(f"a chip set for GW{for_event} is not visible until its deadline")

    squad = set(picks)
    bank = source_bank
    later = sorted(
        (t for t in transfers if int(t["event"]) > source_event),
        key=lambda t: str(t.get("time", "")),
    )
    pending: list[PendingTransfer] = []
    for t in later:
        event = int(t["event"])
        if chips.get(event) == "freehit":
            continue  # temporary: the squad reverts after the week
        out_id, in_id = int(t["element_out"]), int(t["element_in"])
        if out_id not in squad or in_id in squad:
            raise SquadStateError(
                f"transfer {out_id} -> {in_id} (GW{event}) does not follow from the GW"
                f"{source_event} squad"
            )
        squad = (squad - {out_id}) | {in_id}
        bank += int(t["element_out_cost"]) - int(t["element_in_cost"])
        pending.append(
            PendingTransfer(
                event=event,
                time=str(t.get("time", "")),
                element_in=in_id,
                element_out=out_id,
                cost_in=int(t["element_in_cost"]),
                received_out=int(t["element_out_cost"]),
            )
        )
    if bank < 0:
        raise SquadStateError(f"public data gives a negative bank ({bank} tenths)")

    free = free_transfers_after(
        [row for row in history if row.event <= source_event], chip_events=chips
    )
    if chip in UNLIMITED_TRANSFER_CHIPS:
        remaining, hits = free, 0
    else:
        remaining = max(0, free - len(pending))
        hits = max(0, len(pending) - free)
    if chip == "freehit":
        uncertain.append(
            f"Free Hit active for GW{for_event}: its temporary squad is not planned; "
            "the plan starts from the squad you revert to"
        )

    freehits = {gw for gw, c in chips.items() if c == "freehit"}
    paid = purchase_prices(transfers, squad, freehits)
    estimated: set[int] = set()
    for pid in squad - set(paid):
        if pid in first_squad_price:
            paid[pid] = first_squad_price[pid]
        else:
            paid[pid] = now_cost[pid]
            estimated.add(pid)
    selling = {pid: selling_price(paid[pid], now_cost[pid]) for pid in squad}

    effective = source_deadline
    if pending:
        effective = max(effective, datetime.fromisoformat(pending[-1].time.replace("Z", "+00:00")))
    return SquadState(
        source="public",
        source_event=source_event,
        for_event=for_event,
        effective_at=effective,
        squad=frozenset(squad),
        bank_tenths=bank,
        free_transfers=min(remaining, MAX_FREE_TRANSFERS),
        selling_tenths=selling,
        pending=tuple(pending),
        pending_hits=hits,
        chip=chip,
        estimated=frozenset(estimated),
        uncertain=tuple(uncertain),
    )


# --- manager-confirmed snapshot ------------------------------------------------


def snapshot_path() -> Path:
    return Path(os.environ.get(ENV_SNAPSHOT) or Path(user_config_dir("fpl-agent")) / "squad.json")


def load_snapshot(
    raw: dict[str, Any],
    *,
    entry_id: int,
    for_event: int,
    last_deadline: datetime,
    elements: dict[int, Element],
) -> SquadState | None:
    """A validated snapshot, or ``None`` when it describes another gameweek or
    predates the last deadline (it no longer applies). Raises
    :class:`SquadStateError` on anything malformed: a snapshot that is meant
    to apply must be right, never half-used."""
    problems: list[str] = []
    if int(raw.get("entry_id", -1)) != entry_id:
        problems.append(f"entry_id {raw.get('entry_id')} is not {entry_id}")
    try:
        as_of = datetime.fromisoformat(str(raw["as_of"]))
        if as_of.tzinfo is None:
            problems.append("as_of needs a timezone offset")
    except (KeyError, ValueError):
        raise SquadStateError("snapshot: as_of is missing or not an ISO timestamp") from None
    if problems:
        raise SquadStateError("snapshot: " + "; ".join(problems))
    if int(raw.get("event", -1)) != for_event or as_of <= last_deadline:
        return None

    bank = raw.get("bank")
    free = raw.get("free_transfers")
    chip = raw.get("chip")
    if not isinstance(bank, int) or bank < 0:
        problems.append("bank must be a non-negative integer of tenths")
    if not isinstance(free, int) or not 0 <= free <= MAX_FREE_TRANSFERS:
        problems.append(f"free_transfers must be an integer 0-{MAX_FREE_TRANSFERS}")
    if chip not in (None, "wildcard", "freehit"):
        problems.append("chip must be wildcard, freehit or null")

    entries = raw.get("squad")
    selling: dict[int, int] = {}
    if not isinstance(entries, list):
        problems.append("squad must be a list")
        entries = []
    for entry in entries:
        pid = entry.get("element") if isinstance(entry, dict) else None
        if not isinstance(pid, int) or pid not in elements:
            problems.append(f"unknown player {pid}")
            continue
        if pid in selling:
            problems.append(f"player {pid} listed twice")
            continue
        now = elements[pid].now_cost
        sell, buy = entry.get("selling_price"), entry.get("purchase_price")
        if sell is None and isinstance(buy, int) and buy > 0:
            sell = selling_price(buy, now)
        if not isinstance(sell, int) or sell <= 0:
            problems.append(f"player {pid} needs a selling_price or purchase_price in tenths")
            continue
        if sell > now:
            problems.append(f"player {pid} sells for {sell}, above today's price {now}")
        selling[pid] = sell
    if len(selling) != SQUAD_SIZE:
        problems.append(f"squad has {len(selling)} valid players, needs {SQUAD_SIZE}")
    for position, quota in SQUAD_QUOTA.items():
        count = sum(1 for p in selling if elements[p].position is position)
        if count != quota:
            problems.append(f"{count} {position.value}, needs {quota}")
    clubs = Counter(elements[p].team for p in selling)
    if any(n > MAX_PER_CLUB for n in clubs.values()):
        problems.append("more than three players from one club")
    if problems:
        raise SquadStateError("snapshot: " + "; ".join(problems))

    assert isinstance(bank, int) and isinstance(free, int)
    return SquadState(
        source="snapshot",
        source_event=for_event,
        for_event=for_event,
        effective_at=as_of,
        squad=frozenset(selling),
        bank_tenths=bank,
        free_transfers=free,
        selling_tenths=selling,
        chip=chip,
    )


def read_snapshot(path: Path | None = None) -> dict[str, Any] | None:
    path = path or snapshot_path()
    if not path.exists():
        return None
    try:
        data = json.loads(path.read_text())
    except (OSError, ValueError) as exc:
        raise SquadStateError(f"snapshot {path}: unreadable ({exc})") from None
    if not isinstance(data, dict):
        raise SquadStateError(f"snapshot {path}: expected a JSON object")
    return data
