"""Runtime configuration, loaded from the environment.

Deliberately contains no credential fields. See ``.env.example``.
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from zoneinfo import ZoneInfo

DEFAULT_TIMEZONE = "Europe/London"


def _env_int(key: str, default: int) -> int:
    raw = os.getenv(key)
    if raw is None or raw.strip() == "":
        return default
    try:
        return int(raw)
    except ValueError as exc:
        raise ValueError(f"{key} must be an integer, got {raw!r}") from exc


def _env_float(key: str, default: float) -> float:
    raw = os.getenv(key)
    if raw is None or raw.strip() == "":
        return default
    try:
        return float(raw)
    except ValueError as exc:
        raise ValueError(f"{key} must be a number, got {raw!r}") from exc


@dataclass(frozen=True, slots=True)
class Settings:
    """Everything the agent needs to run. All of it is safe to log."""

    entry_id: int | None
    timezone: ZoneInfo
    horizon: int
    decay: float
    cache_ttl: int

    @classmethod
    def from_env(cls) -> Settings:
        raw_entry = os.getenv("FPL_ENTRY_ID", "").strip()
        entry_id = int(raw_entry) if raw_entry else None

        tz_name = os.getenv("FPL_TIMEZONE", DEFAULT_TIMEZONE).strip() or DEFAULT_TIMEZONE
        try:
            tz = ZoneInfo(tz_name)
        except Exception as exc:  # noqa: BLE001 - surface a clear message
            raise ValueError(
                f"FPL_TIMEZONE={tz_name!r} is not a valid IANA timezone "
                "(e.g. 'Asia/Ulaanbaatar', 'Europe/London')"
            ) from exc

        horizon = _env_int("FPL_HORIZON", 3)
        if horizon < 1:
            raise ValueError("FPL_HORIZON must be at least 1")

        decay = _env_float("FPL_DECAY", 0.85)
        if not 0.0 < decay <= 1.0:
            raise ValueError("FPL_DECAY must be in (0, 1]")

        return cls(
            entry_id=entry_id,
            timezone=tz,
            horizon=horizon,
            decay=decay,
            cache_ttl=_env_int("FPL_CACHE_TTL", 900),
        )
