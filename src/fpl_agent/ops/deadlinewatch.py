"""Deadline reminder: 24 hours and 3 hours before each deadline, with the
flags on the players actually owned.

Runs every 30 minutes; each window for each gameweek is sent once, keyed in the
Telegram record. The flag list comes from the live API at send time — the
point of the 3-hour nudge is to catch a Friday press-conference flag that was
not there on Thursday.
"""

from __future__ import annotations

import asyncio
import sys
from datetime import UTC, datetime, timedelta
from typing import Any

from fpl_agent.api.client import FPLClient
from fpl_agent.notify import telegram
from fpl_agent.ops.common import (
    entry_id,
    fmt_local,
    log,
    mark_success,
    notifier,
    parse_iso,
    settings,
)

JOB = "deadlinewatch"
WINDOWS: tuple[tuple[str, timedelta], ...] = (
    ("24h", timedelta(hours=24)),
    ("3h", timedelta(hours=3)),
)


def window_due(deadline: datetime, now: datetime) -> str | None:
    """Which reminder is due now: the tightest window the deadline is inside.

    Pure. ``None`` once the deadline has passed or while it is more than 24h away.
    """
    remaining = deadline - now
    if remaining <= timedelta(0):
        return None
    for name, width in reversed(WINDOWS):  # tightest first
        if remaining <= width:
            return name
    return None


def flagged(elements: list[dict[str, Any]], owned: set[int], teams: dict[int, str]) -> list[str]:
    """``"Cherki (MCI) [d/75] Knee injury - 75% chance"`` for each owned player
    the game has flagged. Pure."""
    lines: list[str] = []
    for e in elements:
        if int(e["id"]) not in owned:
            continue
        status = e.get("status", "a")
        chance = e.get("chance_of_playing_next_round")
        if status == "a" and chance in (None, 100):
            continue
        news = (e.get("news") or "").strip()
        lines.append(
            f"{e['web_name']} ({teams.get(int(e['team']), '?')}) "
            f"[{status}/{chance if chance is not None else '?'}] {news}".rstrip()
        )
    return sorted(lines)


async def _fetch(eid: int) -> tuple[dict[str, Any], set[int]]:
    async with FPLClient(cache_ttl=0) as client:
        bootstrap = await client.get_json("/bootstrap-static/", ttl=0)
        events = bootstrap.get("events", [])
        # Owned = the picks of the latest gameweek that has a lineup.
        current = next((e for e in events if e.get("is_current")), None)
        owned: set[int] = set()
        if current is not None:
            picks = await client.get_json(f"/entry/{eid}/event/{current['id']}/picks/", ttl=0)
            owned = {int(p["element"]) for p in picks.get("picks", [])}
    return bootstrap, owned


def run() -> int:
    s = settings()
    eid = entry_id(JOB, s)
    try:
        bootstrap, owned = asyncio.run(_fetch(eid))
    except Exception as exc:  # noqa: BLE001 - retry on the next run
        log(JOB, f"FPL API unreachable: {exc}")
        return 1

    nxt = next((e for e in bootstrap.get("events", []) if e.get("is_next")), None)
    if nxt is None:
        log(JOB, "no upcoming gameweek")
        mark_success(JOB)
        return 0

    deadline = parse_iso(nxt["deadline_time"])
    now = datetime.now(UTC)
    window = window_due(deadline, now)
    if window is None:
        log(JOB, f"GW{nxt['id']} deadline {fmt_local(deadline, s.timezone)} — not in a window")
        mark_success(JOB)
        return 0

    key = f"deadline:{nxt['id']}:{window}"
    if telegram.sent_recently(key, 48 * 3600):
        log(JOB, f"{key} already sent")
        mark_success(JOB)
        return 0

    teams = {int(t["id"]): str(t["short_name"]) for t in bootstrap.get("teams", [])}
    flags = flagged(bootstrap.get("elements", []), owned, teams)
    body = f"Deadline {fmt_local(deadline, s.timezone)}.\n"
    body += ("Flagged: " + "; ".join(flags)) if flags else "No flags on your squad."
    result = notifier(JOB).send(f"GW{nxt['id']} deadline in {window}", body, key=key)
    log(JOB, f"{key}: {'sent' if result.sent else result.reason} ({len(flags)} flagged)")
    mark_success(JOB)
    return 0


if __name__ == "__main__":
    sys.exit(run())
