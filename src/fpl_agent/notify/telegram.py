"""Telegram alerts through a dedicated FPL bot.

WHY ITS OWN BOT
---------------
The alerts here are advisory nudges — "GW8 settled, recap ready", "deadline in
24h, Cherki now flagged" — and they must never be confused with anything that
moves money. So this bot is separate from any other bot on the same phone, with
its own token under its own names: ``FPL_TELEGRAM_BOT_TOKEN`` and
``FPL_TELEGRAM_USER_ID``. Both live in the repo ``.env`` (gitignored) or the
process environment, and nothing in this module prints either.

A BOT CAN ONLY SEND
-------------------
The Bot API lets this code send messages to the one user who pressed /start.
It cannot read channels it is not an admin of, so team-news channels are read
elsewhere, through the public ``t.me/s/`` preview (see ``data/tg_channels``).

UNCONFIGURED IS LOUD, NOT SILENT
--------------------------------
A missing token is a supported state — the jobs must still sync and export
without it. But "no alerts" looks identical to "nothing happened", and that
exact ambiguity hid a dead notifier for weeks on another system. So every
``send`` returns a :class:`SendResult` that says *why* nothing went out, and
callers are expected to log it.
"""

from __future__ import annotations

import json
import os
import time
from dataclasses import dataclass
from pathlib import Path

import httpx
from platformdirs import user_state_dir

API = "https://api.telegram.org"
SEND_TIMEOUT_SEC = 20.0
ENV_TOKEN = "FPL_TELEGRAM_BOT_TOKEN"
ENV_USER = "FPL_TELEGRAM_USER_ID"

#: MarkdownV2 rejects the whole message if any of these is unescaped. Escaping
#: beats falling back to plain text: a flag string containing a bracket would
#: otherwise swallow the one alert that mattered.
_MARKDOWN_V2_SPECIALS = r"_*[]()~`>#+-=|{}.!"


def escape(text: str) -> str:
    """Escape ``text`` for ``parse_mode=MarkdownV2``."""
    for ch in _MARKDOWN_V2_SPECIALS:
        text = text.replace(ch, f"\\{ch}")
    return text


def repo_env_path() -> Path:
    """The repo's ``.env``: this file is ``src/fpl_agent/notify/telegram.py``,
    so the repo root is four levels up. ``tests/test_notify.py`` pins this,
    because an off-by-one here silently reads a file that does not exist."""
    return Path(__file__).resolve().parents[3] / ".env"


def read_env_file(path: Path) -> dict[str, str]:
    """Parse ``KEY=value`` lines. Comments and blanks are skipped; surrounding
    quotes are stripped so ``TOKEN="123:abc"`` is not sent with the quotes."""
    values: dict[str, str] = {}
    if not path.exists():
        return values
    for line in path.read_text().splitlines():
        stripped = line.strip()
        if not stripped or stripped.startswith("#") or "=" not in stripped:
            continue
        key, value = stripped.split("=", 1)
        values[key.strip()] = value.strip().strip("\"'")
    return values


def record_path() -> Path:
    """Every attempt, sent or not, appended as one JSON line. Telegram keeps
    nothing once a message is read on the phone; this is the durable copy."""
    return Path(user_state_dir("fpl-agent")) / "telegram.jsonl"


@dataclass(frozen=True, slots=True)
class SendResult:
    sent: bool
    reason: str
    """``"ok"``, ``"not configured"``, or the error text (truncated)."""


@dataclass(frozen=True, slots=True)
class Notifier:
    token: str = ""
    user_id: str = ""

    @property
    def configured(self) -> bool:
        return bool(self.token and self.user_id)

    def send(
        self,
        title: str,
        body: str = "",
        *,
        key: str | None = None,
        client: httpx.Client | None = None,
    ) -> SendResult:
        """Send ``*title*`` and ``body`` as one MarkdownV2 message.

        ``key`` names the alert for :func:`sent_recently`, so a job that runs
        every ten minutes can say something once rather than every ten minutes.
        Never raises: alerting must not break the job that called it.
        """
        text = f"*{escape(title)}*"
        if body:
            text += f"\n{escape(body)}"

        if not self.configured:
            result = SendResult(False, "not configured")
            _record(title, body, key, result)
            return result

        try:
            own = client is None
            http = client or httpx.Client(timeout=SEND_TIMEOUT_SEC)
            try:
                response = http.post(
                    f"{API}/bot{self.token}/sendMessage",
                    json={
                        "chat_id": self.user_id,
                        "text": text,
                        "parse_mode": "MarkdownV2",
                        "disable_web_page_preview": True,
                    },
                )
            finally:
                if own:
                    http.close()
            if response.status_code == 200 and response.json().get("ok"):
                result = SendResult(True, "ok")
            else:
                result = SendResult(False, f"http {response.status_code}: {response.text[:200]}")
        except Exception as exc:  # noqa: BLE001 - alerting must never break the caller
            result = SendResult(False, str(exc)[:200])

        _record(title, body, key, result)
        return result


def from_env(env_path: Path | None = None) -> Notifier:
    """Real environment first, then the repo ``.env``. Safe to call when neither
    is set: the result simply reports ``not configured`` on send."""
    values = read_env_file(env_path or repo_env_path())
    return Notifier(
        token=os.environ.get(ENV_TOKEN) or values.get(ENV_TOKEN, ""),
        user_id=os.environ.get(ENV_USER) or values.get(ENV_USER, ""),
    )


def sent_recently(key: str, within_sec: float, *, path: Path | None = None) -> bool:
    """True if an alert with this ``key`` was *sent* in the last ``within_sec``.
    Reads the tail of the record file; unconfigured attempts do not count."""
    p = path or record_path()
    if not p.exists():
        return False
    cutoff = time.time() - within_sec
    for line in reversed(p.read_text().splitlines()[-500:]):
        try:
            row = json.loads(line)
        except json.JSONDecodeError:
            continue
        if row.get("ts", 0) < cutoff:
            break
        if row.get("key") == key and row.get("sent"):
            return True
    return False


def _record(
    title: str, body: str, key: str | None, result: SendResult, *, path: Path | None = None
) -> None:
    p = path or record_path()
    try:
        p.parent.mkdir(parents=True, exist_ok=True)
        with p.open("a") as fh:
            fh.write(
                json.dumps(
                    {
                        "ts": time.time(),
                        "key": key,
                        "title": title,
                        "body": body[:500],
                        "sent": result.sent,
                        "reason": result.reason,
                    }
                )
                + "\n"
            )
    except OSError:
        # The record is a convenience; losing a line must not lose the alert.
        pass
