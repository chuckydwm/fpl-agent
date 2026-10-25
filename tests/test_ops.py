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
