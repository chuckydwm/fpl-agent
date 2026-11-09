"""Pre-deadline prep: capture the desk so the snapshot reflects the latest
flags and prices, and log the deadline in the manager's timezone.

Runs hourly and captures once per gameweek, inside the 24 hours before the
deadline. A failed capture (the LMS down, the network out) is retried the
next hour instead of waiting a week, which is what happened before GW10.

The reminder that reaches the phone is :mod:`deadlinewatch`; this is the
capture it builds on.
"""

from __future__ import annotations

import asyncio
import sys
from datetime import UTC, datetime, timedelta

from fpl_agent.api.client import FPLClient
from fpl_agent.api.endpoints import get_bootstrap
from fpl_agent.api.models import Event
from fpl_agent.ops.common import (
    JobState,
    LMSError,
    entry_id,
    fmt_local,
    log,
    mark_success,
    post_lms,
    settings,
)

JOB = "prep"
WINDOW = timedelta(hours=24)


def capture_due(deadline: datetime, now: datetime, gw: int, captured_gw: int) -> bool:
    """Inside the window before an uncaptured gameweek's deadline. Pure."""
    return gw != captured_gw and timedelta(0) < deadline - now <= WINDOW


async def _next_event() -> Event | None:
    async with FPLClient(cache_ttl=0) as client:
        bootstrap = await get_bootstrap(client, ttl=0)
    return bootstrap.next_event


def run() -> int:
    s = settings()
    eid = entry_id(JOB, s)
    state = JobState(JOB)
    data = state.load()
    try:
        nxt = asyncio.run(_next_event())
    except OSError as exc:
        log(JOB, f"FAILED: {exc}")
        return 1
    if nxt is None:
        log(JOB, "no upcoming gameweek")
        mark_success(JOB)
        return 0

    line = f"GW{nxt.id} deadline {fmt_local(nxt.deadline_time, s.timezone)}"
    if not capture_due(
        nxt.deadline_time, datetime.now(UTC), nxt.id, int(data.get("captured_gw", 0))
    ):
        log(JOB, f"{line} — nothing to capture now")
        mark_success(JOB)
        return 0

    log(JOB, line)
    try:
        desk = post_lms("/api/sports/fpl/desk", {"entryId": eid})
    except LMSError as exc:
        log(JOB, f"FAILED: {exc} — retrying next hour")
        return 1
    log(JOB, f"desk captured for GW{desk.get('event')} at {desk.get('capturedAt')}")
    data["captured_gw"] = nxt.id
    state.save(data)
    mark_success(JOB)
    log(JOB, "done")
    return 0


if __name__ == "__main__":
    sys.exit(run())
