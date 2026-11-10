"""Team news from public Telegram channels, read without logging in.

A bot token can only read a channel it administers, so this does not use the
bot at all. Telegram publishes a web preview of every *public* channel at
``t.me/s/<handle>`` — the last twenty posts, no session, no credentials —
and that is all this job reads. Private or followed-only channels are out of
scope here on purpose; they would need a user session.

RSS feeds (``FPL_NEWS_FEEDS``, defaulting to Fantasy Football Scout and BBC
Sport's Premier League feed) are read the same way: public, no login.

New posts become ``news`` notes in the LMS with the post's link as source,
and a line in ``news.jsonl`` that :mod:`fpl_agent.ops.brief` searches for
the manager's players. They are other people's claims: stored as text, shown
as text, never treated as instructions. The analyst still cites its own
sources for verdicts.
"""

from __future__ import annotations

import html
import json
import os
import re
import sys
import xml.etree.ElementTree as ET
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime
from email.utils import parsedate_to_datetime
from functools import partial
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

import httpx

from fpl_agent.ops import common
from fpl_agent.ops.common import (
    JobState,
    LMSError,
    load_dotenv,
    log,
    mark_success,
    parse_iso,
    post_lms,
)

JOB = "channelswatch"
PREVIEW = "https://t.me/s/{handle}"
USER_AGENT = "Mozilla/5.0 (fpl-agent channelswatch; read-only public preview)"
EXCERPT_CHARS = 280
SPORT_ID_DEFAULT = 1  # football in the LMS
DEFAULT_FEEDS = (
    "https://www.fantasyfootballscout.co.uk/feed/",
    "https://feeds.bbci.co.uk/sport/football/premier-league/rss.xml",
)

# One chunk per message. Matching across the whole page with a lazy regex was
# wrong: a photo-only post has no text block, so the match ran on into the
# NEXT post's text and attributed it to the wrong id. Split first, then match
# inside each chunk, where "no text block" simply means "skip".
_POST = re.compile(r'data-post="(?P<post>[^"]+)"')
_TEXT = re.compile(r"tgme_widget_message_text[^>]*>(?P<text>.*?)</div>", re.DOTALL)
_WHEN = re.compile(r'<time[^>]*datetime="(?P<when>[^"]+)"')
_TAG = re.compile(r"<[^>]+>")


@dataclass(frozen=True, slots=True)
class Post:
    handle: str
    msg_id: int
    at: datetime
    text: str
    #: Set for feed items; a channel post's link is built from handle and id.
    url: str = ""

    @property
    def link(self) -> str:
        return self.url or f"https://t.me/{self.handle}/{self.msg_id}"

    @property
    def kind(self) -> str:
        return "rss" if self.url else "telegram"


def parse_preview(page: str, handle: str) -> list[Post]:
    """Posts from a ``t.me/s/<handle>`` page, oldest first. Pure.

    Tolerant by design: a post without a text body (a bare photo) is skipped,
    and anything that does not parse is skipped rather than raised — a layout
    change should degrade to "no new posts", not crash the job.
    """
    posts: list[Post] = []
    heads = list(_POST.finditer(page))
    for i, head in enumerate(heads):
        chunk = page[head.end() : heads[i + 1].start() if i + 1 < len(heads) else len(page)]
        who, _, raw_id = head.group("post").rpartition("/")
        if who.lower() != handle.lower() or not raw_id.isdigit():
            continue
        text_m = _TEXT.search(chunk)
        when_m = _WHEN.search(chunk)
        if text_m is None or when_m is None:
            continue
        raw = text_m.group("text").replace("<br/>", "\n").replace("<br>", "\n")
        text = html.unescape(_TAG.sub("", raw))
        text = re.sub(r"[ \t]+", " ", text)
        text = re.sub(r"\n\s*\n+", "\n", text).strip()
        if not text:
            continue
        try:
            when = parse_iso(when_m.group("when"))
        except ValueError:
            continue
        posts.append(Post(handle=handle, msg_id=int(raw_id), at=when, text=text))
    posts.sort(key=lambda p: p.msg_id)
    return posts


def parse_feed(page: str, url: str) -> list[Post]:
    """Items from an RSS 2.0 feed, oldest first. Pure, and tolerant the same
    way :func:`parse_preview` is: bad XML or an undated item is skipped.

    The id is the publish time in epoch seconds, so "newer than the last
    one seen" works per feed like a channel's message id. Categories are
    kept in the text because Scout tags each post with the players it covers.
    """
    try:
        root = ET.fromstring(page)
    except ET.ParseError:
        return []
    source = urlparse(url).netloc.removeprefix("www.")
    posts: list[Post] = []
    for item in root.iter("item"):
        title = (item.findtext("title") or "").strip()
        link = (item.findtext("link") or "").strip()
        try:
            when = parsedate_to_datetime(item.findtext("pubDate") or "")
        except (TypeError, ValueError):
            continue
        if not title or not link:
            continue
        summary = _TAG.sub("", html.unescape(item.findtext("description") or ""))
        summary = summary.split("The post ")[0]  # WordPress footer
        tags = [c.text.strip() for c in item.findall("category") if c.text]
        text = "\n".join(
            part
            for part in (
                title,
                " ".join(summary.split()),
                ("Tags: " + ", ".join(tags)) if tags else "",
            )
            if part
        )
        posts.append(
            Post(handle=source, msg_id=int(when.timestamp()), at=when, text=text, url=link)
        )
    posts.sort(key=lambda p: p.msg_id)
    return posts


def news_log_path() -> Path:
    return common.STATE_DIR / "news.jsonl"


def append_news(posts: list[Post]) -> None:
    """One JSON line per post, for the briefs to search.

    shortcut: append-only, a few hundred lines a week; prune if it passes a
    few MB."""
    if not posts:
        return
    path = news_log_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as fh:
        for p in posts:
            row = {"source": p.handle, "at": p.at.isoformat(), "link": p.link, "text": p.text}
            fh.write(json.dumps(row, ensure_ascii=False) + "\n")


def excerpt(text: str, limit: int = EXCERPT_CHARS) -> str:
    one_line = " ".join(text.split())
    return one_line if len(one_line) <= limit else one_line[: limit - 1].rstrip() + "…"


def configured_handles() -> list[str]:
    load_dotenv()
    raw = os.environ.get("FPL_TELEGRAM_CHANNELS", "")
    return [h.strip().lstrip("@") for h in raw.split(",") if h.strip()]


def configured_feeds() -> list[str]:
    load_dotenv()
    raw = os.environ.get("FPL_NEWS_FEEDS")
    if raw is None:
        return list(DEFAULT_FEEDS)
    return [u.strip() for u in raw.split(",") if u.strip()]


def _get(url: str) -> str:
    response = httpx.get(url, headers={"User-Agent": USER_AGENT}, timeout=30, follow_redirects=True)
    response.raise_for_status()
    return response.text


_MD_LINK = re.compile(r"\[([^\]]*)\]\([^)]*\)")  # [label](url) -> label
_DANGLING_LINK = re.compile(r"^([^\[\]]*)\]\([^)]*\)")  # label](url) cut from its "["
_URL = re.compile(r"https?://\S+")


def headline(text: str) -> str:
    """A readable title from a post: links reduced to their words, bare URLs
    and markdown marks dropped, from the first line that has any words left.

    A long Reddit post reaches the relay channel split over several messages,
    so a later part starts mid-sentence ("goalie](https://...) then ...").
    That part gets a leading "…" rather than posing as a headline."""
    for line in text.splitlines():
        continued = bool(_DANGLING_LINK.match(line))
        clean = _DANGLING_LINK.sub(r"\1", line)
        clean = _URL.sub("", _MD_LINK.sub(r"\1", clean))
        clean = " ".join(clean.strip(" #*>-").split())
        if not clean:
            continue
        if continued or clean[0].islower():
            clean = "…" + clean
        return clean
    return ""


def note_payload(post: Post, sport_id: int) -> dict[str, Any]:
    return {
        "sportId": sport_id,
        "title": f"{post.handle}: {excerpt(headline(post.text) or post.text, 80)}",
        "body": excerpt(post.text) + f"\n\n— {post.link} · {post.at:%Y-%m-%d %H:%M} UTC",
        "kind": "news",
        "tags": [post.kind, post.handle],
        "sourceKind": post.kind,
        "sourceUrl": post.link,
    }


def run() -> int:
    handles = configured_handles()
    feeds = configured_feeds()
    if not handles and not feeds:
        log(JOB, "FPL_TELEGRAM_CHANNELS and FPL_NEWS_FEEDS are empty — nothing to read")
        mark_success(JOB)
        return 0

    sport_id = int(os.environ.get("FPL_LMS_SPORT_ID", SPORT_ID_DEFAULT))
    state = JobState(JOB)
    data = state.load()
    last_ids: dict[str, int] = {k: int(v) for k, v in data.get("last_ids", {}).items()}
    total = 0
    failures = 0

    # (state key, url to fetch, parser) — a channel's key is its handle, a feed's its url.
    sources: list[tuple[str, str, Callable[[str], list[Post]]]] = [
        (h, PREVIEW.format(handle=h), partial(parse_preview, handle=h)) for h in handles
    ]
    sources += [(u, u, partial(parse_feed, url=u)) for u in feeds]

    for key, url, parse in sources:
        try:
            page = _get(url)
        except httpx.HTTPError as exc:
            log(JOB, f"{key}: fetch failed: {exc}")
            failures += 1
            continue
        posts = parse(page)
        last = last_ids.get(key, 0)
        fresh = [p for p in posts if p.msg_id > last]
        # First run: record the current head rather than importing twenty old
        # posts into the LMS — but keep them locally so the next brief has context.
        if last == 0 and posts:
            last_ids[key] = posts[-1].msg_id
            append_news(posts)
            log(JOB, f"{key}: first run, starting after {posts[-1].msg_id}")
            continue
        for post in fresh:
            try:
                post_lms("/api/sports/notes", note_payload(post, sport_id))
                last_ids[key] = post.msg_id
                append_news([post])
                total += 1
            except LMSError as exc:
                log(JOB, f"{key}/{post.msg_id}: LMS refused: {exc}")
                failures += 1
                break  # keep order; retry from here next run
        log(JOB, f"{key}: {len(posts)} parsed, {len(fresh)} new, head {last_ids.get(key, 0)}")

    data["last_ids"] = last_ids
    state.save(data)
    if failures and not total:
        return 1
    mark_success(JOB)
    log(JOB, f"done: {total} notes written, {failures} failures")
    return 0


if __name__ == "__main__":
    sys.exit(run())
