from __future__ import annotations

import json
import time
from pathlib import Path

import httpx
import pytest
import respx

from fpl_agent.notify import telegram


@pytest.fixture
def record_file(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Send the attempt log to a temp file so tests never touch real state."""
    p = tmp_path / "telegram.jsonl"
    monkeypatch.setattr(telegram, "record_path", lambda: p)
    return p


class TestEscape:
    def test_every_markdown_v2_special_is_escaped(self):
        raw = r"_*[]()~`>#+-=|{}.!"
        out = telegram.escape(raw)
        for ch in raw:
            assert f"\\{ch}" in out

    def test_plain_text_is_untouched(self):
        assert telegram.escape("Haaland 13 pts") == "Haaland 13 pts"

    def test_a_real_flag_string_survives(self):
        # The case that matters: punctuation in a news string must not
        # make the API reject the alert that carries it.
        text = "Cherki [d/75] Knee injury - 75% chance of playing (Fri)."
        out = telegram.escape(text)
        assert out.count("\\") == sum(text.count(c) for c in r"_*[]()~`>#+-=|{}.!")


class TestEnvFile:
    def test_repo_env_path_is_the_repo_root(self):
        """The engine this is copied from once pointed one level too high and
        read a file that did not exist, silently. Pin the path here."""
        root = telegram.repo_env_path().parent
        assert (root / "pyproject.toml").exists(), root
        assert (root / ".env.example").exists(), root

    def test_parses_quotes_comments_and_blanks(self, tmp_path: Path):
        env = tmp_path / ".env"
        env.write_text(
            '\n# comment\nFPL_TELEGRAM_BOT_TOKEN="123:abc"\n'
            "FPL_TELEGRAM_USER_ID='42'\nNOT_A_PAIR\n"
        )
        values = telegram.read_env_file(env)
        assert values == {"FPL_TELEGRAM_BOT_TOKEN": "123:abc", "FPL_TELEGRAM_USER_ID": "42"}

    def test_missing_file_is_empty_not_an_error(self, tmp_path: Path):
        assert telegram.read_env_file(tmp_path / "nope") == {}

    def test_real_environment_wins_over_the_file(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ):
        env = tmp_path / ".env"
        env.write_text("FPL_TELEGRAM_BOT_TOKEN=file\nFPL_TELEGRAM_USER_ID=1\n")
        monkeypatch.setenv("FPL_TELEGRAM_BOT_TOKEN", "shell")
        monkeypatch.delenv("FPL_TELEGRAM_USER_ID", raising=False)
        n = telegram.from_env(env)
        assert n.token == "shell"
        assert n.user_id == "1"


class TestSend:
    def test_unconfigured_says_so_and_does_not_raise(self, record_file: Path):
        result = telegram.Notifier().send("GW8 settled", "61 vs 55")
        assert result == telegram.SendResult(False, "not configured")
        row = json.loads(record_file.read_text().splitlines()[-1])
        assert row["sent"] is False and row["reason"] == "not configured"

    @respx.mock
    def test_sends_markdown_v2_to_the_configured_user(self, record_file: Path):
        route = respx.post("https://api.telegram.org/bot123:abc/sendMessage").mock(
            return_value=httpx.Response(200, json={"ok": True})
        )
        n = telegram.Notifier(token="123:abc", user_id="42")
        result = n.send("GW8 settled", "61 vs 55 (+6).", key="settled:8")
        assert result.sent and result.reason == "ok"
        payload = json.loads(route.calls.last.request.content)
        assert payload["chat_id"] == "42"
        assert payload["parse_mode"] == "MarkdownV2"
        assert payload["text"] == "*GW8 settled*\n61 vs 55 \\(\\+6\\)\\."

    @respx.mock
    def test_api_rejection_is_reported_not_raised(self, record_file: Path):
        respx.post("https://api.telegram.org/bot123:abc/sendMessage").mock(
            return_value=httpx.Response(400, text="Bad Request: can't parse entities")
        )
        result = telegram.Notifier(token="123:abc", user_id="42").send("x")
        assert not result.sent
        assert result.reason.startswith("http 400")

    @respx.mock
    def test_network_failure_is_reported_not_raised(self, record_file: Path):
        respx.post("https://api.telegram.org/bot123:abc/sendMessage").mock(
            side_effect=httpx.ConnectError("boom")
        )
        result = telegram.Notifier(token="123:abc", user_id="42").send("x")
        assert not result.sent and "boom" in result.reason


class TestSentRecently:
    def _write(self, p: Path, rows: list[dict]) -> None:
        p.write_text("".join(json.dumps(r) + "\n" for r in rows))

    def test_recent_sent_alert_is_found(self, record_file: Path):
        self._write(record_file, [{"ts": time.time() - 60, "key": "deadman", "sent": True}])
        assert telegram.sent_recently("deadman", 600)

    def test_old_alert_does_not_count(self, record_file: Path):
        self._write(record_file, [{"ts": time.time() - 7200, "key": "deadman", "sent": True}])
        assert not telegram.sent_recently("deadman", 600)

    def test_unsent_attempts_do_not_count(self, record_file: Path):
        # Otherwise an unconfigured notifier would "suppress" the alert it
        # never managed to send.
        self._write(record_file, [{"ts": time.time() - 60, "key": "deadman", "sent": False}])
        assert not telegram.sent_recently("deadman", 600)

    def test_missing_file_means_never(self, record_file: Path):
        assert not telegram.sent_recently("deadman", 600)
