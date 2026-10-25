"""One-time check after the bot token is pasted into ``.env``.

Run it yourself, once:

    .venv/bin/python -m fpl_agent.ops.telegram_setup

It reads the token from the repo ``.env`` (never from an argument, never
printed), confirms the bot answers and which one it is, finds your chat id
from the ``/start`` you pressed on it, writes that id into ``.env`` if the
line is empty, then sends one test message. The token itself is the one
thing this file never touches beyond reading it.
"""

from __future__ import annotations

import os
import re
import sys
from pathlib import Path
from typing import Any

import httpx

from fpl_agent.notify import telegram
from fpl_agent.ops.common import REPO_ROOT, load_dotenv, log

JOB = "telegram_setup"
TIMEOUT = 20.0


def chat_id_from_updates(updates: list[dict[str, Any]]) -> int | None:
    """The chat id of the most recent private message to the bot. Pure.

    Only private chats count: a bot added to a group would otherwise write a
    group id into ``.env`` and every alert would land there.
    """
    for update in reversed(updates):
        msg = update.get("message") or update.get("edited_message") or {}
        chat = msg.get("chat") or {}
        if chat.get("type") == "private" and isinstance(chat.get("id"), int):
            return int(chat["id"])
    return None


def set_env_value(text: str, key: str, value: str) -> str:
    """Fill ``KEY=`` in a ``.env`` body. Only an *empty* line is replaced; a
    key that already has a value is left exactly as it is. Pure."""
    pattern = re.compile(rf"^(\s*{re.escape(key)}\s*=)\s*$", re.MULTILINE)
    if pattern.search(text):
        return pattern.sub(lambda m: f"{m.group(1)}{value}", text, count=1)
    return text


def _api(token: str, method: str) -> dict[str, Any]:
    response = httpx.get(f"{telegram.API}/bot{token}/{method}", timeout=TIMEOUT)
    response.raise_for_status()
    payload = response.json()
    if not payload.get("ok"):
        raise RuntimeError(f"{method}: {payload.get('description', 'not ok')}")
    return payload.get("result") or {}


def run(env_path: Path | None = None) -> int:
    env = env_path or (REPO_ROOT / ".env")
    load_dotenv()
    token = os.environ.get(telegram.ENV_TOKEN, "")
    if not token:
        log(JOB, f"{telegram.ENV_TOKEN} is empty in {env} — paste the BotFather token there first")
        return 2

    try:
        me = _api(token, "getMe")
    except (httpx.HTTPError, RuntimeError) as exc:
        log(JOB, f"token rejected by Telegram: {exc}")
        return 1
    log(JOB, f"bot answers as @{me.get('username')} ({me.get('first_name')})")

    user_id = os.environ.get(telegram.ENV_USER, "")
    if not user_id:
        try:
            updates = _api(token, "getUpdates")
        except (httpx.HTTPError, RuntimeError) as exc:
            log(JOB, f"getUpdates failed: {exc}")
            return 1
        found = chat_id_from_updates(updates if isinstance(updates, list) else [])
        if found is None:
            log(
                JOB,
                f"no private message to the bot yet — open t.me/{me.get('username')}, "
                "press Start, run this again",
            )
            return 2
        env.write_text(set_env_value(env.read_text(), telegram.ENV_USER, str(found)))
        os.environ[telegram.ENV_USER] = str(found)
        log(JOB, f"{telegram.ENV_USER} written to {env.name}")

    result = telegram.from_env(env).send(
        "fpl-agent connected",
        "Alerts from here on: gameweek settled, deadline reminders, stalled jobs.",
        key="setup",
    )
    if not result.sent:
        log(JOB, f"test message failed: {result.reason}")
        return 1
    log(
        JOB,
        "test message sent — now run: "
        "launchctl kickstart -k gui/$(id -u)/agency.prokonsalt.fpl-deadman",
    )
    return 0


if __name__ == "__main__":
    sys.exit(run())
