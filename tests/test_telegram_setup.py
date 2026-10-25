from __future__ import annotations

from fpl_agent.ops import telegram_setup as setup


class TestChatIdFromUpdates:
    def test_takes_the_most_recent_private_chat(self):
        updates = [
            {"message": {"chat": {"id": 111, "type": "private"}}},
            {"message": {"chat": {"id": 222, "type": "private"}}},
        ]
        assert setup.chat_id_from_updates(updates) == 222

    def test_ignores_groups(self):
        # A bot dropped into a group must not route every alert there.
        updates = [
            {"message": {"chat": {"id": 111, "type": "private"}}},
            {"message": {"chat": {"id": -100999, "type": "supergroup"}}},
        ]
        assert setup.chat_id_from_updates(updates) == 111

    def test_edited_messages_count(self):
        updates = [{"edited_message": {"chat": {"id": 5, "type": "private"}}}]
        assert setup.chat_id_from_updates(updates) == 5

    def test_nothing_yet(self):
        assert setup.chat_id_from_updates([]) is None
        assert setup.chat_id_from_updates([{"message": {}}]) is None


class TestSetEnvValue:
    body = (
        "FPL_ENTRY_ID=1\nFPL_TELEGRAM_BOT_TOKEN=abc\n"
        "FPL_TELEGRAM_USER_ID=\nFPL_TELEGRAM_CHANNELS=\n"
    )

    def test_fills_only_the_empty_key(self):
        out = setup.set_env_value(self.body, "FPL_TELEGRAM_USER_ID", "42")
        assert "FPL_TELEGRAM_USER_ID=42\n" in out
        assert "FPL_TELEGRAM_BOT_TOKEN=abc\n" in out
        assert "FPL_TELEGRAM_CHANNELS=\n" in out

    def test_never_overwrites_a_set_value(self):
        out = setup.set_env_value(self.body, "FPL_TELEGRAM_BOT_TOKEN", "new")
        assert out == self.body

    def test_missing_key_leaves_the_file_alone(self):
        assert setup.set_env_value(self.body, "NOPE", "1") == self.body

    def test_everything_else_is_byte_identical(self):
        out = setup.set_env_value(self.body, "FPL_TELEGRAM_USER_ID", "42")
        assert out.replace("FPL_TELEGRAM_USER_ID=42", "FPL_TELEGRAM_USER_ID=") == self.body
