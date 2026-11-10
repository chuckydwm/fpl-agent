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


class TestCliReadsDotenv:
    """The README puts FPL_ENTRY_ID and FPL_TIMEZONE in .env; the CLI must see them."""

    def test_root_callback_loads_the_repo_env_without_overriding_real_vars(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ):
        from fpl_agent import cli

        (tmp_path / ".env").write_text("FPL_ENTRY_ID=8166161\nFPL_TIMEZONE=Asia/Ulaanbaatar\n")
        monkeypatch.setattr(common, "REPO_ROOT", tmp_path)
        # setenv first so pytest records the original and undoes what load_dotenv sets.
        monkeypatch.setenv("FPL_ENTRY_ID", "")
        monkeypatch.delenv("FPL_ENTRY_ID")
        monkeypatch.setenv("FPL_TIMEZONE", "Europe/London")

        cli._root(verbose=False)

        assert cli.Settings.from_env().entry_id == 8166161
        assert cli.Settings.from_env().timezone.key == "Europe/London"


class TestSettlementDelivery:
    """A recap that did not reach the phone must be retried, not marked done."""

    def _wire(self, monkeypatch, sent_ok: bool, notes: list):
        from types import SimpleNamespace
        from zoneinfo import ZoneInfo

        from fpl_agent.notify.telegram import SendResult
        from fpl_agent.ops import brief

        events = [{"id": 10, "finished": True, "data_checked": True, "average_entry_score": 55}]
        rows = [{"event": 10, "points": 60, "overall_rank": 3_041_885}]

        async def fake_fetch(eid):
            return events, rows

        async def recap_payload(*a, **kw):
            return {"bootstrap": {"big": "x" * 10_000}}

        class Bot:
            configured = True

            def send(self, title, body, key=None):
                return SendResult(sent_ok, "ok" if sent_ok else "timed out")

        monkeypatch.setattr(
            settlewatch, "settings", lambda: SimpleNamespace(timezone=ZoneInfo("Asia/Ulaanbaatar"))
        )
        monkeypatch.setattr(settlewatch, "entry_id", lambda job, s: 1)
        monkeypatch.setattr(settlewatch, "_fetch", fake_fetch)
        monkeypatch.setattr(settlewatch, "post_lms", lambda path, payload: {})
        monkeypatch.setattr(settlewatch, "notifier", lambda job: Bot())
        monkeypatch.setattr(settlewatch.telegram, "sent_recently", lambda key, within: False)
        monkeypatch.setattr(brief, "fetch", recap_payload)
        monkeypatch.setattr(brief, "load_news", lambda now: [])
        monkeypatch.setattr(brief, "recap_brief", lambda data, gw, tz: ("GW10 recap", "body"))
        monkeypatch.setattr(brief, "save_note", lambda title, body: notes.append(title) or True)

    def test_a_failed_send_leaves_the_gameweek_for_the_next_run(self, state_dir, monkeypatch):
        notes: list = []
        self._wire(monkeypatch, sent_ok=False, notes=notes)
        assert settlewatch.run() == 1
        saved = common.JobState("settlewatch").load()
        assert saved["last_handled"] < 10
        assert saved["gameweeks"]["10"]["synced"] and not saved["gameweeks"]["10"]["delivered"]
        assert notes == []

    def test_a_delivered_recap_is_handled_once_and_the_state_stays_small(
        self, state_dir, monkeypatch
    ):
        notes: list = []
        self._wire(monkeypatch, sent_ok=True, notes=notes)
        assert settlewatch.run() == 0
        assert common.JobState("settlewatch").load() == {"last_handled": 10, "gameweeks": {}}
        assert notes == ["GW10 recap"]


class TestHeadline:
    """Note titles come from the first line; a split Reddit post's later parts
    start mid-sentence with a dangling markdown link."""

    def test_a_continuation_loses_its_link_and_is_marked(self):
        text = "goalie](https://www.reddit.com/r/x/comments/1) then a clean shot)\n6. Bruno"
        assert channelswatch.headline(text) == "…goalie then a clean shot)"

    def test_markdown_links_keep_their_words_and_bare_urls_go(self):
        text = "[Do not](https://reddit.com/x) trust https://i.redd.it/a.png pressers"
        assert channelswatch.headline(text) == "Do not trust pressers"

    def test_a_url_only_line_is_skipped_for_the_next(self):
        assert channelswatch.headline("https://redd.it/abc\n# Price Changes") == "Price Changes"

    def test_a_normal_headline_and_a_source_tag_are_untouched(self):
        text = "[Kinsella] Chelsea send warning email to staff.\nhttps://redd.it/1"
        assert channelswatch.headline(text) == "[Kinsella] Chelsea send warning email to staff."

    def test_the_note_title_uses_it(self):
        post = channelswatch.Post(
            "r_FantasyPL",
            41590,
            datetime(2026, 11, 10, tzinfo=UTC),
            "skills](https://i.redd.it/2.png) of anyone in the league.",
        )
        assert channelswatch.note_payload(post, 1)["title"] == (
            "r_FantasyPL: …skills of anyone in the league."
        )


class TestSettlementBacklog:
    """Pending gameweeks oldest first; delivery, sync and archive retried
    separately (audit item 5)."""

    def _events(self, *ids):
        return [
            {"id": i, "finished": True, "data_checked": True, "average_entry_score": 50}
            for i in ids
        ]

    def _wire(self, monkeypatch, events, *, send_ok, notes, sent, syncs, archive_ok=True):
        from types import SimpleNamespace
        from zoneinfo import ZoneInfo

        from fpl_agent.notify import telegram
        from fpl_agent.notify.telegram import SendResult
        from fpl_agent.ops import brief

        # Never read or lock the real alert record.
        monkeypatch.setattr(telegram, "record_path", lambda: common.STATE_DIR / "telegram.jsonl")

        async def fake_fetch(eid):
            return events, []

        class Bot:
            configured = True

            def send(self, title, body, key=None):
                if not send_ok(title):
                    return SendResult(False, "timed out")
                sent.append(title)
                return SendResult(True, "ok")

        monkeypatch.setattr(
            settlewatch, "settings", lambda: SimpleNamespace(timezone=ZoneInfo("Asia/Ulaanbaatar"))
        )
        monkeypatch.setattr(settlewatch, "entry_id", lambda job, s: 1)
        monkeypatch.setattr(settlewatch, "_fetch", fake_fetch)
        monkeypatch.setattr(settlewatch, "post_lms", lambda path, payload: syncs.append(path) or {})
        monkeypatch.setattr(settlewatch, "notifier", lambda job: Bot())
        monkeypatch.setattr(
            settlewatch, "_recap", lambda eid, gw, event, rows, s: (f"GW{gw} recap", "body")
        )
        monkeypatch.setattr(
            brief, "save_note", lambda title, body: archive_ok and (notes.append(title) or True)
        )

    def test_a_failed_recap_stays_pending_when_a_newer_week_settles(self, state_dir, monkeypatch):
        common.JobState("settlewatch").save({"last_handled": 9})
        sent: list = []
        notes: list = []
        syncs: list = []
        # GW10 settles and Telegram is down.
        self._wire(
            monkeypatch,
            self._events(9, 10),
            send_ok=lambda t: False,
            notes=notes,
            sent=sent,
            syncs=syncs,
        )
        assert settlewatch.run() == 1
        # GW11 settles too; Telegram still down for GW10 only.
        self._wire(
            monkeypatch,
            self._events(9, 10, 11),
            send_ok=lambda t: "GW10" not in t,
            notes=notes,
            sent=sent,
            syncs=syncs,
        )
        assert settlewatch.run() == 1
        assert sent == [], "GW11 must not jump ahead of the undelivered GW10"
        # Telegram is back: both arrive, oldest first.
        self._wire(
            monkeypatch,
            self._events(9, 10, 11),
            send_ok=lambda t: True,
            notes=notes,
            sent=sent,
            syncs=syncs,
        )
        assert settlewatch.run() == 0
        assert sent == ["GW10 recap", "GW11 recap"]
        assert notes == ["GW10 recap", "GW11 recap"]
        assert common.JobState("settlewatch").load() == {"last_handled": 11, "gameweeks": {}}

    def test_sync_is_not_repeated_once_done(self, state_dir, monkeypatch):
        common.JobState("settlewatch").save({"last_handled": 9})
        sent: list = []
        notes: list = []
        syncs: list = []
        self._wire(
            monkeypatch,
            self._events(10),
            send_ok=lambda t: False,
            notes=notes,
            sent=sent,
            syncs=syncs,
        )
        settlewatch.run()
        settlewatch.run()
        assert syncs.count("/api/sports/fpl/sync") == 1

    def test_an_archive_failure_is_retried_without_resending(self, state_dir, monkeypatch):
        common.JobState("settlewatch").save({"last_handled": 9})
        sent: list = []
        notes: list = []
        syncs: list = []
        self._wire(
            monkeypatch,
            self._events(10),
            send_ok=lambda t: True,
            notes=notes,
            sent=sent,
            syncs=syncs,
            archive_ok=False,
        )
        settlewatch.run()
        assert sent == ["GW10 recap"] and notes == []
        assert common.JobState("settlewatch").load()["last_handled"] == 9
        self._wire(
            monkeypatch,
            self._events(10),
            send_ok=lambda t: True,
            notes=notes,
            sent=sent,
            syncs=syncs,
            archive_ok=True,
        )
        settlewatch.run()
        assert sent == ["GW10 recap"] and notes == ["GW10 recap"]
        assert common.JobState("settlewatch").load()["last_handled"] == 10

    def test_a_legacy_state_file_is_read_as_done_up_to_last_handled(self, state_dir, monkeypatch):
        common.JobState("settlewatch").save({"last_handled": 10})
        sent: list = []
        self._wire(
            monkeypatch, self._events(9, 10), send_ok=lambda t: True, notes=[], sent=sent, syncs=[]
        )
        assert settlewatch.run() == 0 and sent == []

    def test_a_failed_fetch_is_no_decision_not_nothing_new(self, state_dir, monkeypatch):
        from fpl_agent.api.client import FPLAPIError

        common.JobState("settlewatch").save({"last_handled": 9})

        async def down(eid):
            raise FPLAPIError("503")

        self._wire(
            monkeypatch, self._events(10), send_ok=lambda t: True, notes=[], sent=[], syncs=[]
        )
        monkeypatch.setattr(settlewatch, "_fetch", down)
        assert settlewatch.run() == 1
        assert common.marker_age_sec("settlewatch") is None, "a failed fetch is not a healthy run"

    def test_a_second_run_waits_for_the_lock(self, state_dir, monkeypatch):
        state = common.JobState("settlewatch")
        with state.locked() as mine:
            assert mine
            with common.JobState("settlewatch").locked() as other:
                assert not other

    def test_advance_stops_at_the_first_unfinished_week(self):
        done = {"synced": True, "delivered": True, "archived": True}
        weeks = {"10": done, "11": {**done, "archived": False}, "12": done}
        assert settlewatch.advance(9, weeks) == (10, {"11": weeks["11"], "12": done})


class TestSendOnce:
    def test_a_key_already_sent_is_not_sent_again(self, state_dir, monkeypatch):
        from fpl_agent.notify import telegram

        record = state_dir / "telegram.jsonl"
        monkeypatch.setattr(telegram, "record_path", lambda: record)
        calls: list = []

        class Bot:
            def send(self, title, body, key=None):
                calls.append(key)
                telegram._record(title, body, key, telegram.SendResult(True, "ok"), path=record)
                return telegram.SendResult(True, "ok")

        first = telegram.send_once(Bot(), "t", "b", key="settled:10", within_sec=3600)
        second = telegram.send_once(Bot(), "t", "b", key="settled:10", within_sec=3600)
        assert calls == ["settled:10"]
        assert first.reason == "ok" and second.reason == "already sent"
