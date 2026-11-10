"""Async HTTP client for the public Fantasy Premier League API.

Design notes
------------
* **No authentication.** Every endpoint here is public. The client sends no
  cookies and has no login path, so it cannot mutate a team.
* **Deadline-day resilience.** The API is heavily loaded either side of a
  deadline and will rate-limit or 5xx. Requests retry with exponential backoff
  and jitter, and a disk cache lets a run fall back to the last good payload
  rather than aborting. ``stale_ok`` makes that fallback explicit, and
  ``max_stale_age`` bounds it.
* **Freshness is recorded, not assumed.** Every payload's origin and age is
  kept in :attr:`FPLClient.fetches`, so a caller can say "fetched 08:02,
  live" or "cached 3h old after a failed fetch" instead of presenting old
  data as current. Deadline windows and settlement decisions pass
  ``stale_ok=False``; advisory output may accept bounded stale data and
  must label it (see :func:`FPLClient.freshness_note`).
"""

from __future__ import annotations

import asyncio
import gzip
import json
import logging
import random
import time
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from types import TracebackType
from typing import Any, Literal, Self

import httpx
from platformdirs import user_cache_dir

log = logging.getLogger(__name__)

BASE_URL = "https://fantasy.premierleague.com/api"
USER_AGENT = "fpl-agent/0.1 (+https://github.com/chuckydwm/fpl-agent)"

RETRY_STATUS = frozenset({429, 500, 502, 503, 504})
MAX_ATTEMPTS = 5
BACKOFF_BASE = 1.6


class FPLAPIError(RuntimeError):
    """Raised when the API could not be reached and no cached copy exists."""


@dataclass(frozen=True, slots=True)
class FetchInfo:
    """Where one payload came from and how old it is."""

    path: str
    #: "network": fetched now; "cache": a fresh-enough saved copy;
    #: "stale": an expired copy served because the network failed.
    origin: Literal["network", "cache", "stale"]
    fetched_at: datetime

    def age_seconds(self, now: datetime | None = None) -> float:
        return ((now or datetime.now(UTC)) - self.fetched_at).total_seconds()

    @property
    def is_fallback(self) -> bool:
        return self.origin == "stale"


class _DiskCache:
    """Gzipped JSON cache keyed by request path."""

    def __init__(self, root: Path | None = None) -> None:
        self.root = root or Path(user_cache_dir("fpl-agent"))
        self.root.mkdir(parents=True, exist_ok=True)

    def _path(self, key: str) -> Path:
        safe = key.strip("/").replace("/", "_").replace("?", "_").replace("=", "-")
        return self.root / f"{safe or 'root'}.json.gz"

    def read(self, key: str, ttl: int) -> Any | None:
        path = self._path(key)
        if not path.exists():
            return None
        if ttl >= 0 and (time.time() - path.stat().st_mtime) > ttl:
            return None
        return self._read_ignoring_ttl(path)

    def read_stale(self, key: str) -> Any | None:
        path = self._path(key)
        return self._read_ignoring_ttl(path) if path.exists() else None

    def saved_at(self, key: str) -> datetime | None:
        path = self._path(key)
        if not path.exists():
            return None
        return datetime.fromtimestamp(path.stat().st_mtime, UTC)

    @staticmethod
    def _read_ignoring_ttl(path: Path) -> Any | None:
        try:
            with gzip.open(path, "rt", encoding="utf-8") as fh:
                return json.load(fh)
        except (OSError, json.JSONDecodeError):
            log.warning("Discarding unreadable cache entry %s", path)
            return None

    def write(self, key: str, payload: Any) -> None:
        # Unique per call, not per key: two launchd jobs that fire in the same
        # minute both refresh bootstrap-static, and a shared "<key>.tmp" meant
        # one process renamed the file out from under the other
        # (FileNotFoundError in settlewatch.log, 2026-10-25). The rename is
        # atomic; only the scratch name needed to be private.
        final = self._path(key)
        tmp = final.with_name(f"{final.name}.{time.time_ns()}.tmp")
        try:
            with gzip.open(tmp, "wt", encoding="utf-8") as fh:
                json.dump(payload, fh)
            tmp.replace(self._path(key))
        except OSError:
            log.warning("Could not write cache entry for %s", key, exc_info=True)


class FPLClient:
    """Thin async wrapper over the public API. Use as an async context manager."""

    def __init__(
        self,
        *,
        cache_ttl: int = 900,
        timeout: float = 20.0,
        cache_dir: Path | None = None,
        transport: httpx.AsyncBaseTransport | None = None,
    ) -> None:
        self.cache_ttl = cache_ttl
        self._cache = _DiskCache(cache_dir)
        #: Origin and age of every payload this client returned, by path.
        self.fetches: dict[str, FetchInfo] = {}
        self._client = httpx.AsyncClient(
            base_url=BASE_URL,
            timeout=timeout,
            headers={"User-Agent": USER_AGENT, "Accept": "application/json"},
            follow_redirects=True,
            transport=transport,
        )

    async def __aenter__(self) -> Self:
        return self

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: TracebackType | None,
    ) -> None:
        await self.aclose()

    async def aclose(self) -> None:
        await self._client.aclose()

    async def get_json(
        self,
        path: str,
        *,
        ttl: int | None = None,
        stale_ok: bool = True,
        max_stale_age: float | None = None,
    ) -> Any:
        """GET ``path``, returning parsed JSON.

        Order of preference: fresh cache -> network (with retries) -> stale
        cache. The stale fallback is used only when ``stale_ok`` and, if
        ``max_stale_age`` is set, only for a copy younger than that many
        seconds. Raises :class:`FPLAPIError` when nothing acceptable is left.
        The payload's origin and age are recorded in :attr:`fetches`.
        """
        ttl = self.cache_ttl if ttl is None else ttl

        cached = self._cache.read(path, ttl)
        if cached is not None:
            log.debug("cache hit %s", path)
            saved = self._cache.saved_at(path) or datetime.now(UTC)
            self.fetches[path] = FetchInfo(path, "cache", saved)
            return cached

        last_error: Exception | None = None
        for attempt in range(1, MAX_ATTEMPTS + 1):
            try:
                response = await self._client.get(path)
                if response.status_code in RETRY_STATUS:
                    raise httpx.HTTPStatusError(
                        f"retryable status {response.status_code}",
                        request=response.request,
                        response=response,
                    )
                response.raise_for_status()
                payload = response.json()
                self._cache.write(path, payload)
                self.fetches[path] = FetchInfo(path, "network", datetime.now(UTC))
                return payload
            except (httpx.HTTPError, json.JSONDecodeError) as exc:
                last_error = exc
                if attempt == MAX_ATTEMPTS:
                    break
                delay = (BACKOFF_BASE**attempt) + random.uniform(0, 0.5)
                log.warning(
                    "GET %s failed (attempt %d/%d): %s — retrying in %.1fs",
                    path,
                    attempt,
                    MAX_ATTEMPTS,
                    exc,
                    delay,
                )
                await asyncio.sleep(delay)

        if stale_ok:
            stale = self._cache.read_stale(path)
            stale_saved = self._cache.saved_at(path)
            too_old = (
                max_stale_age is not None
                and stale_saved is not None
                and (datetime.now(UTC) - stale_saved).total_seconds() > max_stale_age
            )
            if stale is not None and stale_saved is not None and not too_old:
                log.warning("Serving STALE cache for %s after %s", path, last_error)
                self.fetches[path] = FetchInfo(path, "stale", stale_saved)
                return stale

        raise FPLAPIError(f"GET {path} failed after {MAX_ATTEMPTS} attempts") from last_error

    def freshness_note(self, now: datetime | None = None) -> str:
        """One line on the oldest payload used, prominent when any was stale."""
        if not self.fetches:
            return "no data fetched"
        now = now or datetime.now(UTC)
        oldest = max(self.fetches.values(), key=lambda f: f.age_seconds(now))
        stale = [f.path for f in self.fetches.values() if f.is_fallback]
        age = _human_age(oldest.age_seconds(now))
        if stale:
            return (
                f"STALE DATA: {len(stale)} request(s) failed and old copies were used "
                f"(oldest {age} old: {oldest.path})"
            )
        return f"data fetched {oldest.fetched_at:%Y-%m-%d %H:%M} UTC or later (oldest {age} old)"


def _human_age(seconds: float) -> str:
    minutes = int(seconds // 60)
    if minutes < 60:
        return f"{minutes}m"
    hours, minutes = divmod(minutes, 60)
    return f"{hours}h{minutes:02d}m" if hours < 48 else f"{hours // 24}d"
