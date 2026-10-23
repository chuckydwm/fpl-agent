"""Dead-man's switch: alert when a job has stopped finishing.

Every other job writes a marker on a clean finish. This one, every ten
minutes, checks those markers against how often each job is meant to run,
and sends one alert per stale job every six hours until it recovers.

It exists because of two silences: the trading engine failed for 48 hours
after an OS update and nothing said so, and the FPL cron jobs failed on every
run for seven weeks while being described as a backstop. A monitor that is
not itself configured is the same silence again, so this job exits non-zero
when it has no Telegram bot — ``launchctl list`` then shows the problem.
"""

from __future__ import annotations

import subprocess
import sys
import time

from fpl_agent.notify import telegram
from fpl_agent.ops.common import log, marker_age_sec, notifier

JOB = "deadman"

#: Maximum age of each job's success marker before it counts as stale.
#: Watchers run every 30 minutes; three missed runs is a real outage.
#: Weekly jobs get a day of slack past their week.
LIMITS_SEC: dict[str, float] = {
    "settlewatch": 90 * 60,
    "deadlinewatch": 90 * 60,
    "channelswatch": 6 * 3600,
    "recap": 8 * 24 * 3600,
    "prep": 8 * 24 * 3600,
}
REPEAT_SEC = 6 * 3600
WAKE_GRACE_SEC = 30 * 60


def stale_jobs(
    ages: dict[str, float | None],
    limits: dict[str, float],
    installed_age: float | None,
) -> list[str]:
    """Jobs whose marker is older than their limit. A job that has *never*
    finished is stale only once the install itself is older than its limit,
    so a fresh install does not page about a weekly job on day one. Pure."""
    out: list[str] = []
    for job, limit in limits.items():
        age = ages.get(job)
        if age is None:
            if installed_age is not None and installed_age > limit:
                out.append(job)
            continue
        if age > limit:
            out.append(job)
    return out


def seconds_since_wake() -> float | None:
    """macOS only: seconds since the machine last woke. A job cannot run while
    the lid is shut, so a marker that went stale during sleep is not an outage."""
    try:
        raw = subprocess.run(
            ["/usr/sbin/sysctl", "-n", "kern.waketime"], capture_output=True, text=True, timeout=5
        ).stdout
    except (OSError, subprocess.SubprocessError):
        return None
    # "{ sec = 1761220000, usec = 123 } Thu Oct 23 ..."
    try:
        sec = int(raw.split("sec =")[1].split(",")[0].strip())
    except (IndexError, ValueError):
        return None
    return time.time() - sec


def run() -> int:
    n = notifier(JOB)
    if not n.configured:
        log(JOB, "refusing to run silently: no FPL_TELEGRAM_BOT_TOKEN / FPL_TELEGRAM_USER_ID")
        return 2

    since_wake = seconds_since_wake()
    if since_wake is not None and since_wake < WAKE_GRACE_SEC:
        log(JOB, f"woke {since_wake:.0f}s ago — giving the jobs a chance to run first")
        return 0

    ages = {job: marker_age_sec(job) for job in LIMITS_SEC}
    stale = stale_jobs(ages, LIMITS_SEC, marker_age_sec("installed"))
    if not stale:
        log(JOB, "all jobs fresh: " + ", ".join(
            f"{j} {int(a // 60)}m" if a is not None else f"{j} never" for j, a in ages.items()
        ))
        return 0

    for job in stale:
        key = f"deadman:{job}"
        if telegram.sent_recently(key, REPEAT_SEC):
            log(JOB, f"{job} stale, alert already sent within {REPEAT_SEC // 3600}h")
            continue
        age = ages.get(job)
        if age is not None:
            detail = f"last clean finish {int(age // 3600)}h ago"
        else:
            detail = "has never finished"
        result = n.send(
            f"FPL job stalled: {job}",
            f"{detail}. Check logs/{job}.log and launchctl list.",
            key=key,
        )
        log(JOB, f"{job}: {detail} — alert {'sent' if result.sent else result.reason}")
    return 0


if __name__ == "__main__":
    sys.exit(run())
