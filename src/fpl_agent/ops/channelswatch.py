"""Team news from public Telegram channels, read without logging in.

A bot token can only read a channel it administers, so this does not use the
bot at all. Telegram publishes a web preview of every *public* channel at
``t.me/s/<handle>`` — the last twenty posts, no session, no credentials —
and that is all this job reads. Private or followed-only channels are out of
scope here on purpose; they would need a user session.

New posts become ``news`` notes in the LMS with the ``t.me`` link as source.
They are other people's claims: stored as text, shown as text, never treated
as instructions. The analyst still cites its own sources for verdicts.
"""

from __future__ import annotations

import html
import os
import re
import sys
from dataclasses import dataclass
from datetime import datetime
from typing import Any

import httpx

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

    @property
    def link(self) -> str:
        return f"https://t.me/{self.handle}/{self.msg_id}"


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


def excerpt(text: str, limit: int = EXCERPT_CHARS) -> str:
    one_line = " ".join(text.split())
    return one_line if len(one_line) <= limit else one_line[: limit - 1].rstrip() + "…"


def configured_handles() -> list[str]:
    load_dotenv()
    raw = os.environ.get("FPL_TELEGRAM_CHANNELS", "")
    return [h.strip().lstrip("@") for h in raw.split(",") if h.strip()]


def fetch_preview(handle: str) -> str:
    response = httpx.get(
        PREVIEW.format(handle=handle),
        headers={"User-Agent": USER_AGENT},
        timeout=30,
        follow_redirects=True,
    )
    response.raise_for_status()
    return response.text


def note_payload(post: Post, sport_id: int) -> dict[str, Any]:
    first_line = post.text.splitlines()[0]
    return {
        "sportId": sport_id,
        "title": f"{post.handle}: {excerpt(first_line, 80)}",
        "body": excerpt(post.text) + f"\n\n— {post.link} · {post.at:%Y-%m-%d %H:%M} UTC",
        "kind": "news",
        "tags": ["telegram", post.handle],
        "sourceKind": "telegram",
        "sourceUrl": post.link,
    }


def run() -> int:
    handles = configured_handles()
    if not handles:
        log(JOB, "FPL_TELEGRAM_CHANNELS is empty — nothing to read")
        mark_success(JOB)
        return 0

    sport_id = int(os.environ.get("FPL_LMS_SPORT_ID", SPORT_ID_DEFAULT))
    state = JobState(JOB)
    data = state.load()
    last_ids: dict[str, int] = {k: int(v) for k, v in data.get("last_ids", {}).items()}
    total = 0
    failures = 0

    for handle in handles:
        try:
            page = fetch_preview(handle)
        except httpx.HTTPError as exc:
            log(JOB, f"{handle}: fetch failed: {exc}")
            failures += 1
            continue
        posts = parse_preview(page, handle)
        last = last_ids.get(handle, 0)
        fresh = [p for p in posts if p.msg_id > last]
        # First run: record the current head rather than importing twenty old posts.
        if last == 0 and posts:
            last_ids[handle] = posts[-1].msg_id
            log(JOB, f"{handle}: first run, starting after post {posts[-1].msg_id}")
            continue
        for post in fresh:
            try:
                post_lms("/api/sports/notes", note_payload(post, sport_id))
                last_ids[handle] = post.msg_id
                total += 1
            except LMSError as exc:
                log(JOB, f"{handle}/{post.msg_id}: LMS refused: {exc}")
                failures += 1
                break  # keep order; retry from here next run
        log(JOB, f"{handle}: {len(fresh)} new, head {last_ids.get(handle, 0)}")

    data["last_ids"] = last_ids
    state.save(data)
    if failures and not total:
        return 1
    mark_success(JOB)
    log(JOB, f"done: {total} notes written, {failures} failures")
    return 0


if __name__ == "__main__":
    sys.exit(run())
