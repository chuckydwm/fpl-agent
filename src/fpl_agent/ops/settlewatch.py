"""Settlement watcher: the moment a gameweek is final, record it and say so.

``finished`` flips before ``data_checked``; syncing between the two records
provisional bonus that later moves. So nothing is written until both are true.
Runs every 30 minutes from launchd — it does not depend on any terminal
staying open, which is exactly what the three previous watchers did and why
they died.

**Backlog.** Every settled gameweek after ``last_handled`` is pending, and
pending weeks are handled oldest first. Each has three steps, recorded
separately so a retry repeats only what failed:

1. ``synced``: the LMS sync and desk capture (one call covers every pending
   week, and is safe to repeat);
2. ``delivered``: the recap reached Telegram (or no bot is configured);
3. ``archived``: the recap was saved as an LMS note.

A week whose recap was not delivered stays pending and blocks newer weeks'
recaps, so they arrive in order once Telegram is back. An archive failure is
retried on its own without resending. ``last_handled`` moves up only past
weeks that are fully done.

**Delivery is at least once, not exactly once.** A timeout after Telegram
accepted the message looks like a failure, so the retry can send it again.
The send itself is deduplicated by key under a lock (``telegram.send_once``),
which stops two overlapping runs from both sending.

**Freshness.** Settlement decisions use only live data: a failed fetch is a
failed run, never an old copy read as "nothing new".

State file: ``{"last_handled": N, "gameweeks": {"11": {"synced": true,
"delivered": false, "archived": false, "attempts": 2, "last_error": "..."}}}``.
A legacy file holding only ``last_handled`` reads as everything up to it done;
with no file at all, the first run starts at the newest settled week.
"""

from __future__ import annotations

import asyncio
import sys
from datetime import UTC, datetime
from typing import Any

from fpl_agent.api.client import FPLClient
from fpl_agent.notify import telegram
from fpl_agent.ops import brief
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
STEPS = ("synced", "delivered", "archived")
DEDUPE_SEC = 30 * 24 * 3600


def pending_settled(events: list[dict[str, Any]], last_handled: int) -> list[dict[str, Any]]:
    """Settled events after ``last_handled``, oldest first. Pure."""
    settled = [e for e in events if e.get("finished") and e.get("data_checked")]
    return sorted((e for e in settled if int(e["id"]) > last_handled), key=lambda e: int(e["id"]))


def newly_settled(events: list[dict[str, Any]], last_handled: int) -> dict[str, Any] | None:
    """The highest settled event not yet handled, or ``None``. Kept for callers
    that only need to know whether anything is new."""
    pending = pending_settled(events, last_handled)
    return pending[-1] if pending else None


def advance(
    last_handled: int, weeks: dict[str, dict[str, Any]]
) -> tuple[int, dict[str, dict[str, Any]]]:
    """Move ``last_handled`` past every consecutive fully done week and drop
    their records. Pure."""
    remaining = dict(weeks)
    while True:
        record = remaining.get(str(last_handled + 1))
        if record is None or not all(record.get(step) for step in STEPS):
            return last_handled, remaining
        last_handled += 1
        del remaining[str(last_handled)]


async def _fetch(eid: int) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    async with FPLClient(cache_ttl=0, stale_ok=False) as client:
        bootstrap = await client.get_json("/bootstrap-static/", ttl=0)
        history = await client.get_json(f"/entry/{eid}/history/", ttl=0)
    return bootstrap.get("events", []), history.get("current", [])


def run() -> int:
    state = JobState(JOB)
    with state.locked() as mine:
        if not mine:
            log(JOB, "another run holds the lock; nothing to do")
            return 0
        return _run_locked(state)


def _run_locked(state: JobState) -> int:
    s = settings()
    eid = entry_id(JOB, s)
    data = state.load()
    last = int(data.get("last_handled", 0))
    weeks: dict[str, dict[str, Any]] = dict(data.get("gameweeks", {}))

    try:
        events, rows = asyncio.run(_fetch(eid))
    except Exception as exc:  # noqa: BLE001 - only live data decides settlement
        log(JOB, f"FPL API unreachable, no decision made: {exc}")
        return 1

    if "last_handled" not in data:
        # First run: start at the newest settled week, not with a recap for
        # every week of the season so far.
        settled = pending_settled(events, 0)
        last = int(settled[-1]["id"]) - 1 if settled else 0
        log(JOB, f"first run: starting after GW{last}")
    pending = pending_settled(events, last)
    if not pending:
        log(JOB, f"nothing new (last handled GW{last})")
        mark_success(JOB)
        return 0
    for event in pending:
        weeks.setdefault(str(event["id"]), {step: False for step in STEPS} | {"attempts": 0})

    def save() -> None:
        new_last, remaining = advance(last, weeks)
        state.save({"last_handled": new_last, "gameweeks": remaining})

    ids = [int(e["id"]) for e in pending]
    if not all(weeks[str(gw)]["synced"] for gw in ids):
        log(JOB, f"GW{', GW'.join(map(str, ids))} settled — syncing")
        try:
            post_lms("/api/sports/fpl/sync", {"entryId": eid})
            post_lms("/api/sports/fpl/desk", {"entryId": eid})  # also exports the vault
        except LMSError as exc:
            log(JOB, f"FAILED: {exc}")
            save()
            return 1
        for gw in ids:
            weeks[str(gw)]["synced"] = True
        save()

    bot = notifier(JOB)
    failed = False
    for event in pending:
        gw = int(event["id"])
        record = weeks[str(gw)]
        if not record["delivered"]:
            title, body = _recap(eid, gw, event, rows, s)
            record["attempts"] = int(record.get("attempts", 0)) + 1
            result = telegram.send_once(
                bot, title, brief.fit(title, body), key=f"settled:{gw}", within_sec=DEDUPE_SEC
            )
            log(JOB, f"GW{gw} recap: {'sent' if result.sent else result.reason}")
            if bot.configured and not result.sent:
                record["last_error"] = result.reason
                save()
                failed = True
                break  # keep recaps in order: newer weeks wait for this one
            record["delivered"] = True
            record["pending_archive"] = [title, body]
            save()
        if not record["archived"]:
            title, body = record.get("pending_archive") or _recap(eid, gw, event, rows, s)
            if brief.save_note(title, body):
                record["archived"] = True
                record.pop("pending_archive", None)
            else:
                log(JOB, f"GW{gw} LMS note not saved; will retry")
            save()

    if failed:
        return 1
    mark_success(JOB)
    log(JOB, "done")
    return 0


def _recap(
    eid: int, gw: int, event: dict[str, Any], rows: list[dict[str, Any]], s: Any
) -> tuple[str, str]:
    """The full recap, or the one-liner if it cannot be built."""
    try:
        recap = asyncio.run(brief.fetch(eid, picks_event=gw, live_event=gw))
        recap["news"] = brief.load_news(datetime.now(UTC))
        return brief.recap_brief(recap, gw, s.timezone)
    except Exception as exc:  # noqa: BLE001 - a broken recap must not cost the alert
        log(JOB, f"recap failed ({exc}); sending the one-liner instead")
        return f"GW{gw} settled", _one_liner(event, rows, gw)


def _one_liner(event: dict[str, Any], rows: list[dict[str, Any]], gw: int) -> str:
    mine = next((r for r in rows if int(r.get("event", 0)) == gw), None)
    points = int(mine["points"]) if mine else None
    avg = event.get("average_entry_score")
    rank = mine.get("overall_rank") if mine else None
    if points is None or avg is None or rank is None:
        return "Synced and exported. Recap is on the desk."
    return (
        f"{points} vs {avg} ({points - int(avg):+d}). Overall rank {rank:,}. Recap is on the desk."
    )


if __name__ == "__main__":
    sys.exit(run())
