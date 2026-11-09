from __future__ import annotations

import time
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from fpl_agent.ops import channelswatch, common, deadlinewatch, deadman, settlewatch


@pytest.fixture
def state_dir(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    monkeypatch.setattr(common, "STATE_DIR", tmp_path)
    monkeypatch.setattr(common, "MARKER_DIR", tmp_path / "markers")
    return tmp_path


def _event(id_: int, finished: bool, checked: bool) -> dict:
    return {"id": id_, "finished": finished, "data_checked": checked}


class TestSettlement:
    def test_requires_both_flags(self):
        # `finished` flips before `data_checked`; bonus is provisional in between.
        events = [_event(7, True, False)]
        assert settlewatch.newly_settled(events, last_handled=6) is None

    def test_finds_the_new_settled_week(self):
        events = [_event(7, True, True), _event(8, False, False)]
        assert settlewatch.newly_settled(events, last_handled=6)["id"] == 7

    def test_already_handled_is_ignored(self):
        events = [_event(7, True, True)]
        assert settlewatch.newly_settled(events, last_handled=7) is None

    def test_picks_the_highest_when_several_are_pending(self):
        events = [_event(6, True, True), _event(7, True, True)]
        assert settlewatch.newly_settled(events, last_handled=0)["id"] == 7


class TestDeadlineWindow:
    now = datetime(2026, 10, 23, 12, 0, tzinfo=UTC)

    def test_more_than_a_day_out_is_quiet(self):
        assert deadlinewatch.window_due(self.now + timedelta(hours=30), self.now) is None

    def test_inside_a_day_is_the_24h_reminder(self):
        assert deadlinewatch.window_due(self.now + timedelta(hours=20), self.now) == "24h"

    def test_inside_three_hours_is_the_3h_reminder(self):
        assert deadlinewatch.window_due(self.now + timedelta(hours=2), self.now) == "3h"

    def test_past_the_deadline_is_quiet(self):
        assert deadlinewatch.window_due(self.now - timedelta(minutes=1), self.now) is None


class TestFlagged:
    teams = {1: "MCI", 2: "LIV"}

    def test_formats_owned_flagged_players_only(self):
        elements = [
            {
                "id": 1,
                "web_name": "Cherki",
                "team": 1,
                "status": "d",
                "chance_of_playing_next_round": 75,
                "news": "Knee injury - 75% chance of playing",
            },
            {
                "id": 2,
                "web_name": "Haaland",
                "team": 1,
                "status": "a",
                "chance_of_playing_next_round": None,
                "news": "",
            },
            {
                "id": 3,
                "web_name": "Szoboszlai",
                "team": 2,
                "status": "i",
                "chance_of_playing_next_round": 0,
                "news": "Hamstring injury",
            },
        ]
        out = deadlinewatch.flagged(elements, owned={1, 2}, teams=self.teams)
        assert out == ["Cherki (MCI) [d/75] Knee injury - 75% chance of playing"]

    def test_a_flag_with_no_chance_value_still_shows(self):
        elements = [
            {
                "id": 1,
                "web_name": "X",
                "team": 1,
                "status": "d",
                "chance_of_playing_next_round": None,
                "news": "Knock",
            }
        ]
        assert deadlinewatch.flagged(elements, {1}, self.teams) == ["X (MCI) [d/?] Knock"]


class TestDeadman:
    limits = {"settlewatch": 90 * 60, "recap": 8 * 24 * 3600}

    def test_fresh_jobs_are_not_stale(self):
        assert deadman.stale_jobs({"settlewatch": 60, "recap": 3600}, self.limits, 1e6) == []

    def test_old_marker_is_stale(self):
        ages = {"settlewatch": 2 * 3600, "recap": 60}
        assert deadman.stale_jobs(ages, self.limits, 1e6) == ["settlewatch"]

    def test_never_run_is_not_stale_right_after_install(self):
        # A weekly job should not page on day one.
        assert deadman.stale_jobs({"settlewatch": None, "recap": None}, self.limits, 600) == []

    def test_never_run_is_stale_once_the_install_is_old_enough(self):
        ages = {"settlewatch": None, "recap": None}
        assert deadman.stale_jobs(ages, self.limits, 2 * 3600) == ["settlewatch"]

    def test_no_install_marker_means_nothing_is_stale(self):
        assert deadman.stale_jobs({"settlewatch": None}, self.limits, None) == []


PREVIEW_HTML = """
<div class="tgme_widget_message_wrap"><div class="tgme_widget_message" data-post="fplnews/101">
<div class="tgme_widget_message_text js-message_text" dir="auto">Haaland &amp; Foden start.<br/>
Cherki benched &lt;again&gt;</div>
<time datetime="2026-10-23T10:00:00+00:00">10:00</time></div></div>
<div class="tgme_widget_message_wrap"><div class="tgme_widget_message" data-post="fplnews/102">
<div class="tgme_widget_message_photo_wrap"></div>
<time datetime="2026-10-23T10:05:00+00:00">10:05</time></div></div>
<div class="tgme_widget_message_wrap"><div class="tgme_widget_message" data-post="otherchannel/5">
<div class="tgme_widget_message_text js-message_text">not ours</div>
<time datetime="2026-10-23T10:06:00+00:00">10:06</time></div></div>
<div class="tgme_widget_message_wrap"><div class="tgme_widget_message" data-post="fplnews/103">
<div class="tgme_widget_message_text js-message_text"
>Press conference at <a href="x">13:30</a> BST.</div>
<time datetime="2026-10-23T11:00:00+00:00">11:00</time></div></div>
"""


class TestChannelPreview:
    def test_parses_text_posts_in_order_and_unescapes(self):
        posts = channelswatch.parse_preview(PREVIEW_HTML, "fplnews")
        assert [p.msg_id for p in posts] == [101, 103]
        assert posts[0].text == "Haaland & Foden start.\nCherki benched <again>"
        assert posts[0].at == datetime(2026, 10, 23, 10, 0, tzinfo=UTC)
        assert posts[0].link == "https://t.me/fplnews/101"

    def test_photo_only_posts_and_other_channels_are_skipped(self):
        ids = {p.msg_id for p in channelswatch.parse_preview(PREVIEW_HTML, "fplnews")}
        assert 102 not in ids and 5 not in ids

    def test_links_inside_text_are_flattened(self):
        posts = channelswatch.parse_preview(PREVIEW_HTML, "fplnews")
        assert posts[1].text == "Press conference at 13:30 BST."

    def test_garbage_parses_to_nothing(self):
        assert channelswatch.parse_preview("<html></html>", "fplnews") == []

    def test_excerpt_truncates_with_an_ellipsis(self):
        assert channelswatch.excerpt("a" * 300, 10) == "a" * 9 + "…"
        assert channelswatch.excerpt("short") == "short"

    def test_note_payload_shape(self):
        post = channelswatch.parse_preview(PREVIEW_HTML, "fplnews")[0]
        payload = channelswatch.note_payload(post, sport_id=1)
        assert payload["sportId"] == 1
        assert payload["kind"] == "news"
        assert payload["sourceKind"] == "telegram"
        assert payload["sourceUrl"] == "https://t.me/fplnews/101"
        assert payload["title"].startswith("fplnews: Haaland & Foden start.")
        assert "https://t.me/fplnews/101" in payload["body"]


FEED_XML = """<?xml version="1.0" encoding="UTF-8"?><rss version="2.0"><channel>
<title>Scout</title>
<item><title>Newer: Cherki benched again</title><link>https://ffs.example/b</link>
<pubDate>Mon, 09 Nov 2026 06:00:00 +0000</pubDate>
<category><![CDATA[Cherki]]></category><category><![CDATA[Haaland]]></category>
<description><![CDATA[<p>City&#8217;s defence crumbles</p>
<p>The post <a href="x">Newer</a> appeared first on Scout.</p>]]></description></item>
<item><title>Older: press conference</title><link>https://ffs.example/a</link>
<pubDate>Sun, 08 Nov 2026 12:00:00 +0000</pubDate></item>
<item><title>Undated</title><link>https://ffs.example/c</link></item>
</channel></rss>"""


class TestFeeds:
    URL = "https://www.ffs.example/feed/"

    def test_items_oldest_first_with_tags_and_without_the_footer(self):
        posts = channelswatch.parse_feed(FEED_XML, self.URL)
        assert [p.url for p in posts] == ["https://ffs.example/a", "https://ffs.example/b"]
        newer = posts[1]
        assert newer.handle == "ffs.example"
        assert newer.at == datetime(2026, 11, 9, 6, 0, tzinfo=UTC)
        assert newer.msg_id == int(newer.at.timestamp())
        assert (
            newer.text
            == "Newer: Cherki benched again\nCity’s defence crumbles\nTags: Cherki, Haaland"
        )

    def test_bad_xml_and_undated_items_are_skipped(self):
        assert channelswatch.parse_feed("<rss><channel>", self.URL) == []
        assert all(
            p.url != "https://ffs.example/c" for p in channelswatch.parse_feed(FEED_XML, self.URL)
        )

    def test_feed_note_is_marked_rss(self):
        post = channelswatch.parse_feed(FEED_XML, self.URL)[0]
        payload = channelswatch.note_payload(post, sport_id=1)
        assert payload["sourceKind"] == "rss"
        assert payload["sourceUrl"] == "https://ffs.example/a"

    def test_news_log_appends_one_line_per_post(self, state_dir: Path):
        posts = channelswatch.parse_feed(FEED_XML, self.URL)
        channelswatch.append_news(posts)
        channelswatch.append_news(posts[:1])
        lines = (state_dir / "news.jsonl").read_text().splitlines()
        assert len(lines) == 3
        assert '"link": "https://ffs.example/a"' in lines[0]

    def test_feeds_default_when_unset_and_can_be_switched_off(self, monkeypatch):
        monkeypatch.setattr(channelswatch, "load_dotenv", lambda: None)
        monkeypatch.delenv("FPL_NEWS_FEEDS", raising=False)
        assert channelswatch.configured_feeds() == list(channelswatch.DEFAULT_FEEDS)
        monkeypatch.setenv("FPL_NEWS_FEEDS", "")
        assert channelswatch.configured_feeds() == []


class TestStateAndMarkers:
    def test_job_state_round_trips(self, state_dir: Path):
        state = common.JobState("x")
        assert state.load() == {}
        state.save({"last_handled": 7})
        assert common.JobState("x").load() == {"last_handled": 7}

    def test_marker_age(self, state_dir: Path):
        assert common.marker_age_sec("recap") is None
        common.mark_success("recap")
        age = common.marker_age_sec("recap", now=time.time() + 120)
        assert age is not None and 119 <= age <= 122

    def test_fmt_local_uses_the_managers_zone(self):
        from zoneinfo import ZoneInfo

        when = datetime(2026, 10, 23, 17, 30, tzinfo=UTC)
        assert common.fmt_local(when, ZoneInfo("Asia/Ulaanbaatar")).startswith("Sat 24 Oct 01:30")


class TestBriefFallback:
    """A bug in the brief must never cost the reminder itself."""

    def test_deadline_reminder_falls_back_to_the_flag_list(self, state_dir, monkeypatch):
        from types import SimpleNamespace
        from zoneinfo import ZoneInfo

        from fpl_agent.notify.telegram import SendResult
        from fpl_agent.ops import brief

        deadline = datetime.now(UTC) + timedelta(hours=2)
        bootstrap = {
            "events": [
                {"id": 10, "is_current": True, "deadline_time": "2026-11-06T18:30:00Z"},
                {"id": 11, "is_next": True, "deadline_time": deadline.isoformat()},
            ],
            "teams": [{"id": 1, "short_name": "ARS"}],
            "elements": [
                {
                    "id": 7,
                    "web_name": "Raya",
                    "team": 1,
                    "status": "d",
                    "chance_of_playing_next_round": 75,
                    "news": "Knock",
                },
            ],
        }

        async def fake_fetch(eid):
            return bootstrap, {7}

        async def broken(*a, **kw):
            raise RuntimeError("boom")

        sent = []

        class FakeNotifier:
            def send(self, title, body, key=None):
                sent.append((title, body, key))
                return SendResult(True, "ok")

        monkeypatch.setattr(
            deadlinewatch,
            "settings",
            lambda: SimpleNamespace(timezone=ZoneInfo("Asia/Ulaanbaatar")),
        )
        monkeypatch.setattr(deadlinewatch, "entry_id", lambda job, s: 1)
        monkeypatch.setattr(deadlinewatch, "_fetch", fake_fetch)
        monkeypatch.setattr(deadlinewatch, "notifier", lambda job: FakeNotifier())
        monkeypatch.setattr(deadlinewatch.telegram, "sent_recently", lambda key, within: False)
        monkeypatch.setattr(brief, "fetch", broken)
        monkeypatch.setattr(brief, "save_note", lambda title, body: True)

        assert deadlinewatch.run() == 0
        [(title, body, key)] = sent
        assert title == "GW11 deadline in 3h"
        assert "Raya (ARS) [d/75] Knock" in body
        assert key == "deadline:11:3h"


class TestPrepWindow:
    DEADLINE = datetime(2026, 11, 21, 11, 0, tzinfo=UTC)

    def test_captures_once_inside_the_day_before(self):
        from fpl_agent.ops import prep

        now = self.DEADLINE - timedelta(hours=20)
        assert prep.capture_due(self.DEADLINE, now, gw=11, captured_gw=10)
        assert not prep.capture_due(self.DEADLINE, now, gw=11, captured_gw=11)

    def test_quiet_too_early_and_after_the_deadline(self):
        from fpl_agent.ops import prep

        assert not prep.capture_due(self.DEADLINE, self.DEADLINE - timedelta(hours=30), 11, 10)
        assert not prep.capture_due(self.DEADLINE, self.DEADLINE + timedelta(minutes=1), 11, 10)
