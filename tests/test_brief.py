from __future__ import annotations

from datetime import UTC, datetime, timedelta
from zoneinfo import ZoneInfo

from fpl_agent.notify.telegram import escape
from fpl_agent.ops import brief
from fpl_agent.ops.brief import Round

UB = ZoneInfo("Asia/Ulaanbaatar")


def _rounds(*minutes: int) -> list[Round]:
    return [Round(gw, m, 2) for gw, m in enumerate(minutes, start=1)]


class TestRounds:
    def test_double_gameweek_rows_are_summed(self):
        history = [
            {"round": 3, "minutes": 90, "total_points": 6},
            {"round": 4, "minutes": 60, "total_points": 2},
            {"round": 4, "minutes": 30, "total_points": 1},
        ]
        assert brief.rounds(history) == [Round(3, 90, 6), Round(4, 90, 3)]


class TestMinutesFalling:
    def test_catches_cherki_before_gw10(self):
        # His real GW4–9 minutes: a regular, then dropped.
        assert (
            brief.minutes_falling(_rounds(45, 84, 90, 0, 45, 35)) == "minutes 0/45/35' after 73'/gw"
        )

    def test_a_nailed_starter_is_quiet(self):
        assert brief.minutes_falling(_rounds(90, 90, 90, 90, 85, 90)) is None

    def test_a_player_who_was_never_a_regular_is_quiet(self):
        assert brief.minutes_falling(_rounds(20, 30, 15, 10, 25, 30)) is None

    def test_one_short_week_is_not_a_trend(self):
        assert brief.minutes_falling(_rounds(90, 90, 90, 90, 30, 90)) is None

    def test_too_little_history_is_quiet(self):
        assert brief.minutes_falling(_rounds(90, 90, 0, 0)) is None


class TestWarnings:
    def _el(self, **kw):
        return {"status": "a", "yellow_cards": 0, "cost_change_event": 0, **kw}

    def test_one_yellow_from_a_ban_only_inside_the_window(self):
        assert brief.yellow_warning(4, next_gw=11) == "4 yellows, one more is a ban"
        assert brief.yellow_warning(4, next_gw=20) is None
        assert brief.yellow_warning(9, next_gw=30) == "9 yellows, one more is a ban"
        assert brief.yellow_warning(3, next_gw=11) is None

    def test_a_suspended_player_gets_the_flag_not_the_yellow_warning(self):
        el = self._el(status="s", yellow_cards=4, news="Suspended until 5 Dec")
        assert brief.warnings(el, [], 1, 11) == ["Suspended until 5 Dec"]

    def test_a_flag_after_the_last_deadline_is_marked_new(self):
        since = datetime(2026, 11, 6, 18, 30, tzinfo=UTC)
        el = self._el(
            status="d",
            chance_of_playing_next_round=75,
            news="Knock - 75%",
            news_added="2026-11-08T10:00:00Z",
        )
        assert brief.warnings(el, [], 1, 11, flags_since=since) == ["NEW: Knock - 75%"]
        old = {**el, "news_added": "2026-11-01T10:00:00Z"}
        assert brief.warnings(old, [], 1, 11, flags_since=since) == ["Knock - 75%"]

    def test_blank_double_and_price_fall(self):
        el = self._el(cost_change_event=-1)
        assert brief.warnings(el, [], 0, 11) == ["no fixture in GW11", "price down £0.1m this week"]
        assert brief.warnings(self._el(), [], 2, 11) == ["double gameweek 11"]

    def test_a_fit_player_with_nothing_on_is_quiet(self):
        assert brief.warnings(self._el(), _rounds(90, 90, 90, 90, 90, 90), 1, 11) == []


def _fx(h, a, hs, as_, finished=True, event=1):
    return {
        "event": event,
        "team_h": h,
        "team_a": a,
        "team_h_score": hs,
        "team_a_score": as_,
        "finished": finished,
        "team_h_difficulty": 3,
        "team_a_difficulty": 2,
        "kickoff_time": f"2026-11-0{event}T15:00:00Z",
    }


class TestTable:
    def test_ranks_by_points_then_goal_difference_then_goals(self):
        fixtures = [
            _fx(1, 2, 2, 0),  # 1 beats 2
            _fx(3, 4, 1, 0),  # 3 beats 4
            _fx(1, 3, 1, 1),  # draw
            _fx(2, 4, 3, 3),  # draw
            _fx(4, 1, 5, 0, finished=False),  # not played: ignored
        ]
        table = brief.pl_table(fixtures, [1, 2, 3, 4])
        # 1 and 3 both 4pts: GD +2 beats +1. 2 and 4 both 1pt: GD -1 beats -2.
        assert [r.team for r in table] == [1, 3, 4, 2]
        assert (table[0].points, table[0].played, table[0].goal_difference) == (4, 2, 2)

    def test_equal_on_everything_falls_back_to_team_id(self):
        table = brief.pl_table([_fx(2, 1, 1, 1)], [1, 2])
        assert [r.team for r in table] == [1, 2]

    def test_lines_show_the_top_and_every_owned_club(self):
        table = brief.pl_table([_fx(1, 2, 1, 0), _fx(3, 4, 1, 0)], [1, 2, 3, 4])
        short = {1: "ARS", 2: "CHE", 3: "LIV", 4: "SUN"}
        lines = brief.table_lines(table, short, mine={4}, top=1)
        assert lines[0].startswith("1. ARS 3pts")
        assert lines[1].startswith("4. SUN 0pts") and lines[1].endswith("◀")
        assert len(lines) == 2

    def test_fixtures_ahead(self):
        fixtures = [_fx(1, 2, None, None, False, event=11), _fx(3, 1, None, None, False, event=12)]
        assert brief.fixtures_ahead(fixtures, 1, [11, 12], {1: "ARS", 2: "CHE", 3: "LIV"}) == (
            "CHE(H)3 LIV(A)2"
        )
        assert brief.fixture_count(fixtures, 1, 11) == 1


class TestNews:
    def _item(self, text, at="2026-11-09T06:00:00+00:00", link="https://x/1"):
        return {"source": "ffs", "at": at, "link": link, "text": text}

    def test_accent_blind_and_whole_word(self):
        items = [
            self._item("Odegaard back in training", link="https://x/1"),
            self._item("Halloween special: no news", link="https://x/2"),
            self._item("Dewsbury-Hall price rise", link="https://x/3"),
        ]
        out = brief.news_mentions(items, ["Ødegaard", "Hall"])
        assert out == ["Ødegaard: Odegaard back in training [[ffs|https://x/1]]"]

    def test_caps_per_player_newest_first(self):
        items = [
            self._item(
                f"Haaland note {i}", at=f"2026-11-0{i}T06:00:00+00:00", link=f"https://x/{i}"
            )
            for i in range(1, 5)
        ]
        out = brief.news_mentions(items, ["Haaland"], per_player=2)
        assert [o.rsplit(" ", 1)[1] for o in out] == ["[[ffs|https://x/4]]", "[[ffs|https://x/3]]"]

    def test_known_sources_get_short_labels(self):
        item = {**self._item("Thiago sent off"), "source": "feeds.bbci.co.uk"}
        assert brief.news_mentions([item], ["Thiago"]) == [
            "Thiago: Thiago sent off [[BBC|https://x/1]]"
        ]


class TestSnippet:
    def test_quotes_the_sentence_naming_the_player_not_the_headline(self):
        text = (
            "Brighton breeze past 10-man Brentford\n"
            "Diego Gomez fires Brighton to a 2-0 win. Igor Thiago was shown a straight red card. "
            "Hurzeler's side move up to second."
        )
        assert brief.snippet(text, "Thiago") == (
            "Brighton breeze past 10-man Brentford: Igor Thiago was shown a straight red card."
        )

    def test_a_markdown_table_row_is_cleaned_and_given_its_headline(self):
        text = "Predicted Price Changes (8 Nov)\n| **Gvardiol** | 5.7m | MCI | -118.6% |"
        assert brief.snippet(text, "Gvardiol") == (
            "Predicted Price Changes (8 Nov): Gvardiol 5.7m MCI -118.6%"
        )

    def test_a_tag_only_match_falls_back_to_the_headline(self):
        text = "FPL notes: Gross haul\nFantasy notes from Brighton 2-0 Brentford\nTags: Igor Thiago"
        assert brief.snippet(text, "Thiago") == "FPL notes: Gross haul"

    def test_long_sentences_are_cut(self):
        assert (
            brief.snippet("Haaland " + "x" * 300, "Haaland", limit=20)
            == "Haaland " + "x" * 11 + "…"
        )


class TestFit:
    def test_short_body_is_untouched(self):
        assert brief.fit("t", "hello") == "hello"

    def test_long_body_is_cut_at_a_line_and_fits_once_escaped(self):
        body = "\n".join(f"line {i}: a.b-c (d)" for i in range(400))
        out = brief.fit("GW11 brief", body)
        assert out.endswith("the rest is on the LMS desk.")
        assert len(escape("GW11 brief")) + 3 + len(escape(out)) <= brief.TELEGRAM_LIMIT
        assert out.splitlines()[0] == "line 0: a.b-c (d)"


class TestDeadlineBrief:
    def _data(self):
        deadline = "2026-11-21T11:00:00Z"
        return {
            "bootstrap": {
                "teams": [{"id": 1, "short_name": "ARS"}, {"id": 2, "short_name": "CHE"}],
                "events": [
                    {"id": 10, "is_current": True, "deadline_time": "2026-11-06T18:30:00Z"},
                    {"id": 11, "is_next": True, "deadline_time": deadline},
                ],
                "elements": [
                    {
                        "id": 7,
                        "web_name": "Raya",
                        "team": 1,
                        "now_cost": 62,
                        "status": "a",
                        "yellow_cards": 4,
                        "cost_change_event": 0,
                    }
                ],
            },
            "fixtures": [_fx(1, 2, None, None, False, event=11)],
            "picks": [{"element": 7, "position": 1}],
            "histories": {7: [{"round": 10, "minutes": 90, "total_points": 6}]},
            "news": [],
        }

    def test_title_shows_the_real_time_left_when_the_window_was_entered_late(self):
        now = datetime(2026, 11, 21, 11, 0, tzinfo=UTC) - timedelta(hours=1, minutes=40)
        title, body = brief.deadline_brief(
            self._data(), now, UB, suggestion="no transfer; captain Raya."
        )
        assert title == "GW11 brief — deadline in 1h40m"
        assert "Squad as of the GW10 deadline" in body
        assert "Raya: 4 yellows, one more is a ban" in body
        assert "Raya ARS £6.2 | 90'·6 | CHE(H)3" in body
        assert "Model, directional only: no transfer; captain Raya." in body


class TestFetchIsNeverStale:
    async def test_every_request_refuses_the_stale_cache(self, monkeypatch):
        calls: list[tuple[str, dict]] = []

        class FakeClient:
            def __init__(self, **kw):
                pass

            async def __aenter__(self):
                return self

            async def __aexit__(self, *exc):
                return None

            async def get_json(self, path, **kw):
                calls.append((path, kw))
                if "picks" in path:
                    return {"picks": [{"element": 7, "position": 1}], "automatic_subs": []}
                if "live" in path:
                    return {"elements": []}
                return {}

        monkeypatch.setattr(brief, "FPLClient", FakeClient)
        data = await brief.fetch(1, picks_event=10, live_event=10, bootstrap={"given": True})
        assert data["bootstrap"] == {"given": True}
        assert not any(p == "/bootstrap-static/" for p, _ in calls)
        assert calls and all(kw.get("stale_ok") is False for _, kw in calls)


class TestRecapAutoSubs:
    def _el(self, id_, name):
        return {
            "id": id_,
            "web_name": name,
            "team": 1,
            "selected_by_percent": "1.0",
            "cost_change_event": 0,
            "status": "a",
        }

    def test_a_bench_player_who_came_on_is_ranked_and_the_starter_he_replaced_is_not(self):
        data = {
            "bootstrap": {
                "teams": [{"id": 1, "short_name": "ARS"}],
                "events": [{"id": 10, "deadline_time": "2026-11-06T18:30:00Z"}],
                "elements": [self._el(1, "Starter"), self._el(2, "Benchy"), self._el(3, "Keeper")],
            },
            "fixtures": [],
            "history": [],
            "picks": [
                {"element": 3, "position": 1, "multiplier": 1},
                {"element": 1, "position": 2, "multiplier": 1},
                {"element": 2, "position": 12, "multiplier": 1},
            ],
            "automatic_subs": [{"element_in": 2, "element_out": 1}],
            "live": {
                1: {"total_points": 0, "minutes": 0},
                2: {"total_points": 6, "minutes": 90},
                3: {"total_points": 2, "minutes": 90},
            },
        }
        _, body = brief.recap_brief(data, 10, UB)
        assert "Best: Benchy 6 (90') (sub), Keeper 2 (90')." in body
        assert "Starter" not in body.split("Premier League")[0]


class TestTeammateFlags:
    SHORT = {1: "LIV", 2: "ARS"}

    def _el(self, id_, name, team, minutes, **kw):
        return {"id": id_, "web_name": name, "team": team, "minutes": minutes, "status": "a", **kw}

    def _elements(self):
        return [
            self._el(1, "Araujo", 1, 663),  # owned
            self._el(
                2,
                "Virgil",
                1,
                900,
                status="d",
                chance_of_playing_next_round=75,
                news="Unspecified injury - 75% chance of playing",
                news_added="2026-11-09T18:00:00Z",
            ),
            self._el(3, "Benchwarmer", 1, 120, status="i", news="Knee injury"),  # not a regular
            self._el(4, "Fit", 1, 880),  # regular but not flagged
            self._el(5, "Saliba", 2, 900, status="d", news="Knock"),  # club you don't own
        ]

    def test_flags_regulars_at_your_clubs_and_names_your_player(self):
        since = datetime(2026, 11, 6, 18, 30, tzinfo=UTC)
        out = brief.teammate_flags(self._elements(), {1}, 10, self.SHORT, flags_since=since)
        assert out == ["Virgil (LIV, your Araujo): NEW: Unspecified injury - 75% chance of playing"]

    def test_quiet_before_any_gameweek_has_settled(self):
        assert brief.teammate_flags(self._elements(), {1}, 0, self.SHORT) == []

    def test_capped(self):
        many = [self._el(1, "Araujo", 1, 900)] + [
            self._el(i, f"P{i}", 1, 900, status="d", news="Knock") for i in range(2, 20)
        ]
        assert len(brief.teammate_flags(many, {1}, 10, self.SHORT, limit=3)) == 3


class TestSuggestionLine:
    def test_names_captain_and_vice(self):
        from types import SimpleNamespace

        from fpl_agent.optimiser.squad import GameweekPlan, TransferPlan

        gw = GameweekPlan(
            event=11,
            transfers_in=[2],
            transfers_out=[1],
            starting_xi=[],
            bench=[],
            captain=3,
            vice_captain=4,
            hits=0,
            expected_points=0.0,
        )
        names = {
            i: SimpleNamespace(web_name=n)
            for i, n in {1: "Thiago", 2: "Isak", 3: "Haaland", 4: "Ødegaard"}.items()
        }
        line = brief.suggestion_line(TransferPlan([gw], 0.0, "Optimal"), names)
        assert line == "Thiago → Isak; captain Haaland, vice Ødegaard."
