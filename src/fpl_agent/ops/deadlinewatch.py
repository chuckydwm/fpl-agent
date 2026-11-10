"""Deadline reminder: 24 hours and 3 hours before each deadline, carrying the
:mod:`brief` for the players actually owned (flags, minutes, fixtures, news,
the model's suggestion). If the brief cannot be built, the bare flag list goes
out instead, so a reminder is never lost to a bug in the extras.

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

from fpl_agent.agent import predictions
from fpl_agent.agent.planner import run_plan
from fpl_agent.api.client import FPLClient
from fpl_agent.config import Settings
from fpl_agent.notify import telegram
from fpl_agent.ops import brief, common
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
        if brief.flag_text(e) is None:  # the one rule for "flagged"
            continue
        status = e.get("status", "a")
        chance = e.get("chance_of_playing_next_round")
        news = (e.get("news") or "").strip()
        lines.append(
            f"{e['web_name']} ({teams.get(int(e['team']), '?')}) "
            f"[{status}/{chance if chance is not None else '?'}] {news}".rstrip()
        )
    return sorted(lines)


async def _fetch(eid: int) -> tuple[dict[str, Any], set[int]]:
    async with FPLClient(cache_ttl=0) as client:
        # No stale fallback: the reminder and the brief share this payload.
        bootstrap = await client.get_json("/bootstrap-static/", ttl=0, stale_ok=False)
        events = bootstrap.get("events", [])
        # Owned = the picks of the latest gameweek that has a lineup.
        current = next((e for e in events if e.get("is_current")), None)
        owned: set[int] = set()
        if current is not None:
            picks = await client.get_json(
                f"/entry/{eid}/event/{current['id']}/picks/", ttl=0, stale_ok=False
            )
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

    current = next((e for e in bootstrap.get("events", []) if e.get("is_current")), None)
    try:
        if current is None:
            raise LookupError("no gameweek has locked yet")
        data = asyncio.run(brief.fetch(eid, picks_event=int(current["id"]), bootstrap=bootstrap))
        data["news"] = brief.load_news(now)
        title, body = brief.deadline_brief(data, now, s.timezone, suggestion=_suggestion(eid, s))
    except Exception as exc:  # noqa: BLE001 - a broken brief must not cost the reminder
        log(JOB, f"brief failed ({exc}); sending the flag list instead")
        teams = {int(t["id"]): str(t["short_name"]) for t in bootstrap.get("teams", [])}
        flags = flagged(bootstrap.get("elements", []), owned, teams)
        title = f"GW{nxt['id']} deadline in {window}"
        body = f"Deadline {fmt_local(deadline, s.timezone)}.\n"
        body += ("Flagged: " + "; ".join(flags)) if flags else "No flags on your squad."

    bot = notifier(JOB)
    # Checked again under the lock: another run may have sent it meanwhile.
    result = telegram.send_once(bot, title, brief.fit(title, body), key=key, within_sec=48 * 3600)
    log(JOB, f"{key}: {'sent' if result.sent else result.reason}")
    # An unsent brief is retried every 30 minutes; saving each try would
    # leave one LMS note per attempt.
    if (result.sent or not bot.configured) and not brief.save_note(title, body):
        log(JOB, "LMS note not saved")
    mark_success(JOB)
    return 0


def _suggestion(eid: int, s: Settings) -> str | None:
    """The optimiser's next-gameweek move as one line, or ``None`` if it
    cannot run. The model has not beaten the average in backtests, which is
    why the brief labels it directional."""

    async def go() -> str | None:
        # A deadline window needs current evidence: no cache, no stale fallback.
        async with FPLClient(cache_ttl=0, stale_ok=False) as client:
            result = await run_plan(client, eid, horizon=s.horizon, max_transfers=2, decay=s.decay)
        if result is None:
            return None
        # Dated record of what the model predicted, scored once the week settles.
        predictions.append(
            common.STATE_DIR / "predictions.jsonl",
            predictions.capture_rows(result.projections, result.event.id, datetime.now(UTC)),
        )
        return brief.plan_summary(result, s.timezone)

    try:
        return asyncio.run(go())
    except Exception as exc:  # noqa: BLE001 - the brief is still useful without it
        log(JOB, f"model suggestion unavailable: {exc}")
        return None


if __name__ == "__main__":
    sys.exit(run())
