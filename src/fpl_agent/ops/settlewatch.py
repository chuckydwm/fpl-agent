"""Settlement watcher: the moment a gameweek is final, record it and say so.

``finished`` flips before ``data_checked``; syncing between the two records
provisional bonus that later moves. So nothing is written until both are true,
and each gameweek is handled once, tracked in the job state. Runs every
30 minutes from launchd — it does not depend on any terminal staying open,
which is exactly what the three previous watchers did and why they died.
"""

from __future__ import annotations

import asyncio
import sys
from typing import Any

from fpl_agent.api.client import FPLClient
from fpl_agent.notify import telegram
from fpl_agent.ops.common import (
    JobState,
    LMSError,
    entry_id,
    log,
    mark_success,
    notifier,
    post_lms,
    settings,
)

JOB = "settlewatch"


def newly_settled(events: list[dict[str, Any]], last_handled: int) -> dict[str, Any] | None:
    """The highest settled event not yet handled, or ``None``.

    Pure, over the raw ``bootstrap.events`` rows, so it is trivially testable.
    """
    settled = [e for e in events if e.get("finished") and e.get("data_checked")]
    pending = [e for e in settled if int(e["id"]) > last_handled]
    return max(pending, key=lambda e: int(e["id"])) if pending else None


async def _fetch(eid: int) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    async with FPLClient(cache_ttl=0) as client:
        bootstrap = await client.get_json("/bootstrap-static/", ttl=0)
        history = await client.get_json(f"/entry/{eid}/history/", ttl=0)
    return bootstrap.get("events", []), history.get("current", [])


def run() -> int:
    s = settings()
    eid = entry_id(JOB, s)
    state = JobState(JOB)
    data = state.load()
    last = int(data.get("last_handled", 0))

    try:
        events, rows = asyncio.run(_fetch(eid))
    except Exception as exc:  # noqa: BLE001 - the API is flaky; retry next run
        log(JOB, f"FPL API unreachable: {exc}")
        return 1

    event = newly_settled(events, last)
    if event is None:
        log(JOB, f"nothing new (last handled GW{last})")
        mark_success(JOB)
        return 0

    gw = int(event["id"])
    log(JOB, f"GW{gw} is settled — syncing")
    try:
        post_lms("/api/sports/fpl/sync", {"entryId": eid})
        post_lms("/api/sports/fpl/desk", {"entryId": eid})  # also exports the vault
    except LMSError as exc:
        log(JOB, f"FAILED: {exc}")
        return 1

    mine = next((r for r in rows if int(r.get("event", 0)) == gw), None)
    points = int(mine["points"]) if mine else None
    avg = event.get("average_entry_score")
    rank = mine.get("overall_rank") if mine else None
    body = (
        f"{points} vs {avg} ({points - int(avg):+d}). Overall rank {rank:,}. Recap is on the desk."
        if points is not None and avg is not None and rank is not None
        else "Synced and exported. Recap is on the desk."
    )
    key = f"settled:{gw}"
    if telegram.sent_recently(key, 7 * 24 * 3600):
        log(JOB, f"alert for GW{gw} already sent")
    else:
        result = notifier(JOB).send(f"GW{gw} settled", body, key=key)
        log(JOB, f"alert: {'sent' if result.sent else result.reason}")

    data["last_handled"] = gw
    state.save(data)
    mark_success(JOB)
    log(JOB, "done")
    return 0


if __name__ == "__main__":
    sys.exit(run())
