"""The two messages worth reading on a phone: the pre-deadline brief and the
post-gameweek recap.

Everything here except :func:`fetch` and :func:`main` is pure, over the raw
FPL API JSON, so each rule is testable with a dict. The jobs that send these
are :mod:`deadlinewatch` and :mod:`settlewatch`; ``python -m
fpl_agent.ops.brief deadline|recap`` prints either one without sending.

The brief advises. The manager decides and makes every transfer.
"""

from __future__ import annotations

import asyncio
import json
import os
import re
import sys
import unicodedata
from collections import defaultdict
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any
from zoneinfo import ZoneInfo

from fpl_agent.api.client import FPLClient
from fpl_agent.notify.telegram import escape
from fpl_agent.ops import channelswatch
from fpl_agent.ops.common import LMSError, entry_id, fmt_local, parse_iso, post_lms, settings

TELEGRAM_LIMIT = 4096
NEWS_WINDOW = timedelta(hours=72)
POSITIONS = {1: "GK", 2: "DEF", 3: "MID", 4: "FWD"}
#: Ban thresholds: 5 yellows within the first 19 gameweeks, 10 within 32.
#: shortcut: uses the gameweek number for "matches played"; a postponed match
#: shifts it by one, so the warning can be a week early or late.
YELLOW_LIMITS = ((5, 19), (10, 32))


# --- one player -------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class Round:
    event: int
    minutes: int
    points: int


def rounds(history: list[dict[str, Any]]) -> list[Round]:
    """element-summary ``history`` (one row per fixture) as one row per
    gameweek, oldest first; a double gameweek's two matches are summed."""
    minutes: dict[int, int] = defaultdict(int)
    points: dict[int, int] = defaultdict(int)
    for row in history:
        gw = int(row["round"])
        minutes[gw] += int(row.get("minutes", 0))
        points[gw] += int(row.get("total_points", 0))
    return [Round(gw, minutes[gw], points[gw]) for gw in sorted(minutes)]


def minutes_falling(played: list[Round]) -> str | None:
    """``"0/45/35' after 73'/gw"`` when a regular (60+ a week over the three
    weeks before) has played under 60 in two of the last three. This is the
    rule that would have caught Cherki before GW10."""
    if len(played) < 5:
        return None
    last, before = played[-3:], played[-6:-3]
    usual = sum(r.minutes for r in before) / len(before)
    short = sum(1 for r in last if r.minutes < 60)
    if usual < 60 or short < 2:
        return None
    return f"minutes {'/'.join(str(r.minutes) for r in last)}' after {usual:.0f}'/gw"


def yellow_warning(yellows: int, next_gw: int) -> str | None:
    for limit, by_gw in YELLOW_LIMITS:
        if yellows == limit - 1 and next_gw <= by_gw:
            return f"{yellows} yellows, one more is a ban"
    return None


def flag_text(element: dict[str, Any]) -> str | None:
    """The game's own flag, or ``None`` for a fit player."""
    status = element.get("status", "a")
    chance = element.get("chance_of_playing_next_round")
    if status == "a" and chance in (None, 100):
        return None
    news = (element.get("news") or "").strip()
    return news or f"status {status}"


def warnings(
    element: dict[str, Any],
    played: list[Round],
    fixture_count: int,
    next_gw: int,
    *,
    flags_since: datetime | None = None,
) -> list[str]:
    """Why to look at this player before the deadline. Pure.

    ``flags_since`` marks a flag added after it as new."""
    out: list[str] = []
    flag = flag_text(element)
    if flag:
        added = element.get("news_added")
        new = flags_since is not None and added and parse_iso(added) >= flags_since
        out.append(("NEW: " if new else "") + flag)
    if fixture_count == 0:
        out.append(f"no fixture in GW{next_gw}")
    elif fixture_count > 1:
        out.append(f"double gameweek {next_gw}")
    # A suspended player's yellow count no longer threatens a ban he is serving.
    yellows = None if element.get("status") == "s" else int(element.get("yellow_cards", 0))
    for rule in (
        minutes_falling(played),
        yellow_warning(yellows, next_gw) if yellows is not None else None,
    ):
        if rule:
            out.append(rule)
    change = int(element.get("cost_change_event", 0))
    if change < 0:
        out.append(f"price down £{-change / 10:.1f}m this week")
    return out


# --- fixtures and the table -------------------------------------------------


def fixtures_ahead(
    fixtures: list[dict[str, Any]], team: int, events: list[int], short: dict[int, str]
) -> str:
    """``"SUN(A)3 LEE(H)3 NFO(A)2"``: opponent, venue, FPL difficulty."""
    out: list[str] = []
    for f in sorted(fixtures, key=lambda f: (f.get("event") or 99, f.get("kickoff_time") or "")):
        if f.get("event") not in events:
            continue
        if f["team_h"] == team:
            out.append(f"{short[f['team_a']]}(H){f['team_h_difficulty']}")
        elif f["team_a"] == team:
            out.append(f"{short[f['team_h']]}(A){f['team_a_difficulty']}")
    return " ".join(out) or "-"


def fixture_count(fixtures: list[dict[str, Any]], team: int, event: int) -> int:
    return sum(
        1 for f in fixtures if f.get("event") == event and team in (f["team_h"], f["team_a"])
    )


@dataclass(frozen=True, slots=True)
class TableRow:
    team: int
    played: int
    won: int
    drawn: int
    lost: int
    scored: int
    conceded: int

    @property
    def points(self) -> int:
        return 3 * self.won + self.drawn

    @property
    def goal_difference(self) -> int:
        return self.scored - self.conceded


def pl_table(fixtures: list[dict[str, Any]], team_ids: list[int]) -> list[TableRow]:
    """The Premier League table from finished fixtures, best first.

    shortcut: ranks by points, goal difference, goals scored, then team id;
    ignores head-to-head and any points deduction. Fine for a phone summary,
    not as the official table.
    """
    rec: dict[int, list[int]] = {t: [0, 0, 0, 0, 0, 0] for t in team_ids}  # P W D L F A
    for f in fixtures:
        if not f.get("finished") or f.get("team_h_score") is None or f.get("team_a_score") is None:
            continue
        for team, scored, conceded in (
            (f["team_h"], f["team_h_score"], f["team_a_score"]),
            (f["team_a"], f["team_a_score"], f["team_h_score"]),
        ):
            r = rec.setdefault(team, [0, 0, 0, 0, 0, 0])
            r[0] += 1
            r[1 if scored > conceded else 2 if scored == conceded else 3] += 1
            r[4] += scored
            r[5] += conceded
    rows = [TableRow(t, *r) for t, r in rec.items()]
    return sorted(rows, key=lambda r: (-r.points, -r.goal_difference, -r.scored, r.team))


def table_lines(
    table: list[TableRow], short: dict[int, str], mine: set[int], top: int = 6
) -> list[str]:
    """The top ``top`` plus every club the manager owns players from."""
    lines: list[str] = []
    for pos, row in enumerate(table, start=1):
        if pos > top and row.team not in mine:
            continue
        lines.append(
            f"{pos}. {short[row.team]} {row.points}pts ({row.played}P {row.goal_difference:+d})"
            + (" ◀" if row.team in mine else "")
        )
    return lines


# --- news -------------------------------------------------------------------

_FOLD = str.maketrans({"ø": "o", "ł": "l", "đ": "d", "æ": "ae"})


def fold(text: str) -> str:
    """Lower case without accents, so "Odegaard" finds "Ødegaard"."""
    decomposed = unicodedata.normalize("NFKD", text.casefold().translate(_FOLD))
    return "".join(c for c in decomposed if not unicodedata.combining(c))


def load_news(now: datetime, window: timedelta = NEWS_WINDOW) -> list[dict[str, Any]]:
    path = channelswatch.news_log_path()
    if not path.exists():
        return []
    out: list[dict[str, Any]] = []
    for line in path.read_text(encoding="utf-8").splitlines():
        try:
            row = json.loads(line)
            if now - parse_iso(row["at"]) <= window:
                out.append(row)
        except (ValueError, KeyError, TypeError):
            continue
    return out


def news_mentions(
    items: list[dict[str, Any]], names: list[str], *, per_player: int = 2, total: int = 8
) -> list[str]:
    """``"Cherki — ffs: FPL notes: ... https://..."`` for news naming an owned
    player, newest first. Whole-word, accent-blind match on ``web_name``;
    shared names ("Gabriel") can match the wrong player, hence the link."""
    seen: set[str] = set()
    out: list[str] = []
    counts: dict[str, int] = defaultdict(int)
    for item in sorted(items, key=lambda r: r["at"], reverse=True):
        text = fold(item.get("text", ""))
        for name in names:
            if counts[name] >= per_player or item["link"] in seen:
                continue
            if re.search(rf"(?<!\w){re.escape(fold(name))}(?!\w)", text):
                headline = item["text"].splitlines()[0][:90]
                out.append(f"{name} — {item['source']}: {headline} {item['link']}")
                counts[name] += 1
                seen.add(item["link"])
        if len(out) >= total:
            break
    return out[:total]


# --- composing ----------------------------------------------------------------


def fit(title: str, body: str, limit: int = TELEGRAM_LIMIT) -> str:
    """``body`` cut at a line so the escaped message fits one Telegram message."""
    tail = "\n… the rest is on the LMS desk."

    def size(text: str) -> int:
        return len(escape(title)) + 3 + len(escape(text))

    if size(body) <= limit:
        return body
    lines = body.splitlines()
    while lines and size("\n".join(lines) + tail) > limit:
        lines.pop()
    return "\n".join(lines) + tail


def _short(bootstrap: dict[str, Any]) -> dict[int, str]:
    return {int(t["id"]): str(t["short_name"]) for t in bootstrap["teams"]}


def deadline_brief(
    data: dict[str, Any], now: datetime, tz: ZoneInfo, *, suggestion: str | None
) -> tuple[str, str]:
    """(title, body) for the pre-deadline brief. Pure over :func:`fetch` output."""
    bootstrap, fixtures = data["bootstrap"], data["fixtures"]
    short = _short(bootstrap)
    nxt = next(e for e in bootstrap["events"] if e.get("is_next"))
    gw = int(nxt["id"])
    deadline = parse_iso(nxt["deadline_time"])
    left = deadline - now
    hours, minutes = divmod(max(int(left.total_seconds()), 0) // 60, 60)
    title = f"GW{gw} brief — deadline in {hours}h{minutes:02d}m"

    by_id = {int(e["id"]): e for e in bootstrap["elements"]}
    current = next((e for e in bootstrap["events"] if e.get("is_current")), None)
    flags_since = parse_iso(current["deadline_time"]) if current else None
    ahead = [e["id"] for e in bootstrap["events"] if gw <= e["id"] < gw + 3]

    watch: list[str] = []
    squad: list[str] = []
    for pick in sorted(data["picks"], key=lambda p: p["position"]):
        el = by_id[int(pick["element"])]
        played = rounds(data["histories"].get(int(el["id"]), []))
        for w in warnings(
            el, played, fixture_count(fixtures, el["team"], gw), gw, flags_since=flags_since
        ):
            watch.append(f"{el['web_name']}: {w}")
        recent = " ".join(f"{r.minutes}'·{r.points}" for r in played[-3:]) or "-"
        bench = " (bench)" if pick["position"] > 11 else ""
        squad.append(
            f"{el['web_name']} {short[el['team']]} £{el['now_cost'] / 10:.1f}{bench} | "
            f"{recent} | {fixtures_ahead(fixtures, el['team'], ahead, short)}"
        )

    names = [by_id[int(p["element"])]["web_name"] for p in data["picks"]]
    news = news_mentions(data.get("news", []), names)
    parts = [
        f"Deadline {fmt_local(deadline, tz)}.",
        "",
        "Watch:" if watch else "Nothing flagged on your squad.",
        *watch,
        "",
        "Squad — last 3 GWs mins·pts | next 3:",
        *squad,
    ]
    if news:
        parts += ["", "News naming your players (72h):", *news]
    if suggestion:
        parts += ["", f"Model, directional only: {suggestion}"]
    parts += ["", "Sources: FPL API, plus the linked posts. You make the transfers."]
    return title, "\n".join(parts)


def suggestion_line(plan: Any, elements: dict[int, Any]) -> str:
    """One line from a :class:`TransferPlan`'s first gameweek."""
    first = plan.first
    moves = ", ".join(
        f"{elements[o].web_name} → {elements[i].web_name}"
        for o, i in zip(first.transfers_out, first.transfers_in, strict=False)
    )
    captain = elements[first.captain].web_name
    hits = f" (−{first.hits * 4} hit)" if first.hits else ""
    return f"{moves or 'no transfer'}{hits}; captain {captain}."


def recap_brief(data: dict[str, Any], gw: int, tz: ZoneInfo) -> tuple[str, str]:
    """(title, body) for the recap of settled gameweek ``gw``. Pure."""
    bootstrap, fixtures = data["bootstrap"], data["fixtures"]
    short = _short(bootstrap)
    by_id = {int(e["id"]): e for e in bootstrap["elements"]}
    event = next(e for e in bootstrap["events"] if int(e["id"]) == gw)
    rows = {int(r["event"]): r for r in data["history"]}
    mine, prev = rows.get(gw), rows.get(gw - 1)
    live = data["live"]

    parts: list[str] = []
    if mine:
        avg = int(event.get("average_entry_score") or 0)
        rank = mine.get("overall_rank")
        line = f"{mine['points']} pts vs {avg} average ({mine['points'] - avg:+d})."
        if rank and prev and prev.get("overall_rank"):
            arrow = (
                "▲" if rank < prev["overall_rank"] else "▼" if rank > prev["overall_rank"] else "="
            )
            line += f" Rank {prev['overall_rank']:,} → {rank:,} {arrow}"
        parts.append(line)
        if mine.get("event_transfers_cost"):
            parts.append(f"Hit taken: −{mine['event_transfers_cost']}.")
        parts.append(f"Bench left {mine.get('points_on_bench', 0)} pts.")

    scored: list[tuple[int, str]] = []
    for pick in data["picks"]:
        pid = int(pick["element"])
        pts = live.get(pid, {}).get("total_points", 0)
        mins = live.get(pid, {}).get("minutes", 0)
        name = by_id[pid]["web_name"]
        if pick.get("multiplier", 1) > 1:
            parts.append(
                f"Captain {name}: {pts} × {pick['multiplier']} = {pts * pick['multiplier']}."
            )
        if pick["position"] <= 11:
            scored.append((pts, f"{name} {pts} ({mins}')"))
    if scored:
        scored.sort(key=lambda s: s[0], reverse=True)
        parts.append("Best: " + ", ".join(s for _, s in scored[:3]) + ".")
        parts.append("Worst: " + ", ".join(s for _, s in scored[-2:]) + ".")

    owned_teams = {by_id[int(p["element"])]["team"] for p in data["picks"]}
    table = pl_table(fixtures, sorted(short))
    parts += [
        "",
        "Premier League table (◀ = clubs you own):",
        *table_lines(table, short, owned_teams),
    ]

    owned = {int(p["element"]) for p in data["picks"]}
    popular = {
        int(e["id"])
        for e in sorted(bootstrap["elements"], key=lambda e: -float(e["selected_by_percent"]))[:20]
    }
    since = parse_iso(event["deadline_time"])
    flags = []
    for pid in sorted(owned | popular, key=lambda i: by_id[i]["web_name"]):
        el = by_id[pid]
        added = el.get("news_added")
        text = flag_text(el)
        if text and added and parse_iso(added) >= since:
            who = "yours" if pid in owned else f"{el['selected_by_percent']}% owned"
            flags.append(f"{el['web_name']} ({short[el['team']]}, {who}): {text}")
    prices = [
        f"{by_id[i]['web_name']} {'+' if by_id[i]['cost_change_event'] > 0 else '−'}"
        f"£{abs(by_id[i]['cost_change_event']) / 10:.1f}m"
        for i in sorted(owned | popular, key=lambda i: by_id[i]["web_name"])
        if by_id[i].get("cost_change_event")
    ]
    parts += [
        "",
        "New flags since the deadline:" if flags else "No new flags on your or popular players.",
        *flags,
    ]
    if prices:
        parts += ["Price moves (yours + top 20 owned): " + ", ".join(prices) + "."]

    names = [by_id[i]["web_name"] for i in owned]
    news = news_mentions(data.get("news", []), names, total=5)
    if news:
        parts += ["", "News naming your players:", *news]

    nxt = next((e for e in bootstrap["events"] if e.get("is_next")), None)
    if nxt:
        parts += [
            "",
            f"Next: GW{nxt['id']} deadline {fmt_local(parse_iso(nxt['deadline_time']), tz)}.",
        ]
    parts += ["Sources: FPL API (table computed from results), plus the linked posts."]
    return f"GW{gw} recap", "\n".join(parts)


# --- I/O ----------------------------------------------------------------------


async def fetch(eid: int, *, picks_event: int, live_event: int | None = None) -> dict[str, Any]:
    """Everything both briefs read, from the live API (no cache)."""
    async with FPLClient(cache_ttl=0) as client:
        bootstrap = await client.get_json("/bootstrap-static/", ttl=0)
        fixtures = await client.get_json("/fixtures/", ttl=0)
        history = await client.get_json(f"/entry/{eid}/history/", ttl=0)
        picks = (await client.get_json(f"/entry/{eid}/event/{picks_event}/picks/", ttl=0))["picks"]
        summaries = await asyncio.gather(
            *(client.get_json(f"/element-summary/{p['element']}/", ttl=0) for p in picks)
        )
        live: dict[int, dict[str, Any]] = {}
        if live_event is not None:
            rows = (await client.get_json(f"/event/{live_event}/live/", ttl=0))["elements"]
            live = {int(r["id"]): r["stats"] for r in rows}
    return {
        "bootstrap": bootstrap,
        "fixtures": fixtures,
        "history": history.get("current", []),
        "picks": picks,
        "histories": {
            int(p["element"]): s.get("history", []) for p, s in zip(picks, summaries, strict=True)
        },
        "live": live,
    }


def main(argv: list[str]) -> int:
    """``deadline`` or ``recap [GW]``: print a brief, send nothing."""
    from datetime import UTC

    s = settings()
    eid = entry_id("brief", s)
    now = datetime.now(UTC)
    kind = argv[0] if argv else "deadline"
    boot_events = asyncio.run(_events())
    current = next((e for e in boot_events if e.get("is_current")), None)
    if kind == "recap":
        gw = (
            int(argv[1])
            if len(argv) > 1
            else max(int(e["id"]) for e in boot_events if e.get("finished"))
        )
        data = asyncio.run(fetch(eid, picks_event=gw, live_event=gw))
        data["news"] = load_news(now)
        title, body = recap_brief(data, gw, s.timezone)
    else:
        if current is None:
            print("no gameweek has locked yet")
            return 1
        data = asyncio.run(fetch(eid, picks_event=int(current["id"])))
        data["news"] = load_news(now)
        title, body = deadline_brief(data, now, s.timezone, suggestion=None)
    print(title)
    print(fit(title, body))
    return 0


def save_note(title: str, body: str) -> bool:
    """The full, untrimmed brief as an LMS news note. Never raises: the phone
    message matters more than the archive copy."""
    payload = {
        "sportId": int(os.environ.get("FPL_LMS_SPORT_ID", channelswatch.SPORT_ID_DEFAULT)),
        "title": title,
        "body": body,
        "kind": "news",
        "tags": ["brief", "fpl-agent"],
        "sourceKind": "fpl-agent",
    }
    try:
        post_lms("/api/sports/notes", payload)
    except LMSError:
        return False
    return True


async def _events() -> list[dict[str, Any]]:
    async with FPLClient(cache_ttl=0) as client:
        return list((await client.get_json("/bootstrap-static/", ttl=0))["events"])


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
