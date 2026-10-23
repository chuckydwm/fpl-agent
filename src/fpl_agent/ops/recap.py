"""Weekly recap: pull every settled gameweek into the LMS and export the vault.

Belt and braces behind :mod:`settlewatch`, which does the same thing within
half an hour of a gameweek settling. This runs once a week regardless, so a
missed watcher window still leaves the history complete by Wednesday evening.
"""

from __future__ import annotations

import sys

from fpl_agent.ops.common import LMSError, entry_id, log, mark_success, notifier, post_lms, settings

JOB = "recap"


def run() -> int:
    s = settings()
    eid = entry_id(JOB, s)
    notifier(JOB)  # only to log whether alerts are configured
    try:
        synced = post_lms("/api/sports/fpl/sync", {"entryId": eid})
        log(
            JOB,
            f"synced gameweeks {synced.get('synced', [])}, "
            f"last settled {synced.get('lastSettled')}",
        )
        # The desk capture is also what exports the vault.
        desk = post_lms("/api/sports/fpl/desk", {"entryId": eid})
        log(JOB, f"desk captured for GW{desk.get('event')} and vault exported")
    except LMSError as exc:
        log(JOB, f"FAILED: {exc}")
        return 1
    mark_success(JOB)
    log(JOB, "done")
    return 0


if __name__ == "__main__":
    sys.exit(run())
