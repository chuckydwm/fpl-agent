"""Pre-deadline prep: capture the desk so the snapshot reflects the latest
flags and prices, and log the deadline in the manager's timezone.

The reminder that reaches the phone is :mod:`deadlinewatch`; this is the
weekly capture it builds on.
"""

from __future__ import annotations

import asyncio
import sys
from zoneinfo import ZoneInfo

from fpl_agent.api.client import FPLClient
from fpl_agent.api.endpoints import get_bootstrap
from fpl_agent.ops.common import (
    LMSError,
    entry_id,
    fmt_local,
    log,
    mark_success,
    post_lms,
    settings,
)

JOB = "prep"


async def _deadline_line(s_tz: ZoneInfo) -> str:
    async with FPLClient(cache_ttl=0) as client:
        bootstrap = await get_bootstrap(client, ttl=0)
    nxt = bootstrap.next_event
    if nxt is None:
        return "no upcoming gameweek"
    return f"GW{nxt.id} deadline {fmt_local(nxt.deadline_time, s_tz)}"


def run() -> int:
    s = settings()
    eid = entry_id(JOB, s)
    try:
        log(JOB, asyncio.run(_deadline_line(s.timezone)))
        desk = post_lms("/api/sports/fpl/desk", {"entryId": eid})
        log(JOB, f"desk captured for GW{desk.get('event')} at {desk.get('capturedAt')}")
    except (LMSError, OSError) as exc:
        log(JOB, f"FAILED: {exc}")
        return 1
    mark_success(JOB)
    log(JOB, "done")
    return 0


if __name__ == "__main__":
    sys.exit(run())
