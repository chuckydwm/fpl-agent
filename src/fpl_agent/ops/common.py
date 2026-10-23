"""Shared plumbing for the scheduled jobs.

Three things every job needs and none should reimplement: the environment
(repo ``.env`` first, real variables win), the LMS over HTTP, and the success
markers that :mod:`fpl_agent.ops.deadman` reads. Keep this file boring.
"""

from __future__ import annotations

import json
import os
import sys
import time
from contextlib import suppress
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

import httpx
from platformdirs import user_state_dir

from fpl_agent.config import Settings
from fpl_agent.notify import telegram

REPO_ROOT = Path(__file__).resolve().parents[3]
STATE_DIR = Path(user_state_dir("fpl-agent"))
MARKER_DIR = STATE_DIR / "markers"
LMS_TIMEOUT_SEC = 300.0


class LMSError(RuntimeError):
    """The LMS answered with an error, or did not answer."""


def log(job: str, message: str) -> None:
    stamp = datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")
    print(f"[{stamp}] {job}: {message}", flush=True)


def load_dotenv() -> None:
    """Repo ``.env`` into the environment, without overriding what is set."""
    for key, value in telegram.read_env_file(REPO_ROOT / ".env").items():
        os.environ.setdefault(key, value)


def settings() -> Settings:
    load_dotenv()
    return Settings.from_env()


def entry_id(job: str, s: Settings) -> int:
    """The entry to work on, or a clear exit — never a guessed id."""
    if s.entry_id is None:
        log(job, "FPL_ENTRY_ID is not set (repo .env or environment); nothing to do")
        sys.exit(2)
    return s.entry_id


def notifier(job: str) -> telegram.Notifier:
    """The FPL bot, with a loud log line when it is not configured — "no
    alerts" must never look like "nothing happened"."""
    load_dotenv()
    n = telegram.from_env()
    if not n.configured:
        log(job, "telegram: NOT CONFIGURED — alerts are logged here only")
    return n


def lms_url() -> str:
    load_dotenv()
    return os.environ.get("FPL_LMS_URL", "http://127.0.0.1:3003").rstrip("/")


def post_lms(path: str, payload: dict[str, Any], *, timeout: float = LMS_TIMEOUT_SEC) -> Any:
    """POST JSON to the LMS and return its JSON. Raises :class:`LMSError`
    with the server's message on anything but 2xx."""
    url = f"{lms_url()}{path}"
    try:
        response = httpx.post(url, json=payload, timeout=timeout)
    except httpx.HTTPError as exc:
        raise LMSError(f"{path}: {exc}") from exc
    if response.status_code >= 300:
        detail = response.text[:300]
        with suppress(ValueError):
            detail = str(response.json().get("error", detail))
        raise LMSError(f"{path}: HTTP {response.status_code}: {detail}")
    try:
        return response.json()
    except ValueError:
        return {}


def mark_success(job: str) -> None:
    """Write the marker ``deadman`` checks. Called only on a clean finish."""
    MARKER_DIR.mkdir(parents=True, exist_ok=True)
    (MARKER_DIR / job).write_text(f"{time.time():.0f}\n")


def marker_age_sec(job: str, *, now: float | None = None) -> float | None:
    """Seconds since ``job`` last finished cleanly; ``None`` if it never has."""
    path = MARKER_DIR / job
    if not path.exists():
        return None
    try:
        stamp = float(path.read_text().strip())
    except ValueError:
        stamp = path.stat().st_mtime
    return (now if now is not None else time.time()) - stamp


class JobState:
    """A small JSON file per job for "what did I already handle"."""

    def __init__(self, job: str) -> None:
        self.path = STATE_DIR / f"{job}.json"

    def load(self) -> dict[str, Any]:
        if not self.path.exists():
            return {}
        try:
            data = json.loads(self.path.read_text())
        except ValueError:
            return {}
        return data if isinstance(data, dict) else {}

    def save(self, data: dict[str, Any]) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.path.with_suffix(".tmp")
        tmp.write_text(json.dumps(data, indent=2, sort_keys=True) + "\n")
        tmp.replace(self.path)


def fmt_local(when: datetime, tz: ZoneInfo) -> str:
    """``Sat 24 Oct 01:30 Ulaanbaatar``-style, in the manager's timezone.

    Zones without an abbreviation report a bare offset (``+08``), which reads
    badly in a phone alert; the zone's city is clearer and just as true."""
    local = when.astimezone(tz)
    name = local.tzname() or ""
    if not name or name[0] in "+-":
        name = tz.key.rsplit("/", 1)[-1].replace("_", " ")
    return f"{local:%a %d %b %H:%M} {name}"


def parse_iso(stamp: str) -> datetime:
    return datetime.fromisoformat(stamp.replace("Z", "+00:00"))
