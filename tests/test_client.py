"""Disk-cache behaviour that the launchd jobs depend on."""

from __future__ import annotations

import gzip
import logging
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import pytest

from fpl_agent.api import client as client_mod
from fpl_agent.api.client import _DiskCache


def test_round_trip(tmp_path: Path):
    cache = _DiskCache(tmp_path)
    cache.write("bootstrap-static/", {"a": 1})
    assert cache.read("bootstrap-static/", ttl=60) == {"a": 1}
    assert cache.read("bootstrap-static/", ttl=-1) == {"a": 1}  # negative ttl = ignore age


def test_scratch_name_is_private_to_each_call(tmp_path: Path, monkeypatch):
    """Regression: two jobs in the same minute both refreshed bootstrap-static
    through one shared ``<key>.tmp`` and one renamed it out from under the
    other (FileNotFoundError, settlewatch.log 2026-10-25)."""
    seen: list[Path] = []
    real_open = gzip.open

    def spy(path, *args, **kwargs):
        seen.append(Path(path))
        return real_open(path, *args, **kwargs)

    monkeypatch.setattr(client_mod.gzip, "open", spy)
    cache = _DiskCache(tmp_path)
    cache.write("bootstrap-static/", {"n": 1})
    cache.write("bootstrap-static/", {"n": 2})

    tmps = [p for p in seen if p.suffix == ".tmp"]
    assert len(tmps) == 2
    assert tmps[0] != tmps[1]
    assert all(p.name.startswith("bootstrap-static.json.gz.") for p in tmps)
    assert not list(tmp_path.glob("*.tmp")), "scratch files must not be left behind"


def test_concurrent_writers_never_lose_the_file(tmp_path: Path, caplog):
    cache = _DiskCache(tmp_path)
    caplog.set_level(logging.WARNING, logger=client_mod.__name__)

    def worker(n: int) -> None:
        for i in range(25):
            cache.write("bootstrap-static/", {"writer": n, "i": i})

    with ThreadPoolExecutor(max_workers=8) as pool:
        list(pool.map(worker, range(8)))

    assert "Could not write cache entry" not in caplog.text
    payload = cache.read("bootstrap-static/", ttl=-1)
    assert payload is not None and set(payload) == {"writer", "i"}
    assert not list(tmp_path.glob("*.tmp"))


class TestFreshness:
    """Every payload's origin and age is recorded (audit item 7)."""

    URL = "https://fantasy.premierleague.com/api/bootstrap-static/"

    @pytest.fixture(autouse=True)
    def no_backoff(self, monkeypatch):
        import fpl_agent.api.client as client_module

        async def instant(_delay):
            return None

        monkeypatch.setattr(client_module.asyncio, "sleep", instant)

    async def _get(self, tmp_path, **kw):
        from fpl_agent.api.client import FPLClient

        async with FPLClient(cache_dir=tmp_path, cache_ttl=900) as client:
            payload = await client.get_json("/bootstrap-static/", **kw)
            return payload, client.fetches["/bootstrap-static/"], client.freshness_note()

    async def test_network_then_cache(self, tmp_path):
        import respx
        from httpx import Response

        with respx.mock:
            respx.get(self.URL).mock(return_value=Response(200, json={"v": 1}))
            _, first, note = await self._get(tmp_path)
            _, second, _ = await self._get(tmp_path)
        assert first.origin == "network" and second.origin == "cache"
        assert "STALE" not in note

    async def test_a_failed_fetch_served_from_cache_is_labelled_stale(self, tmp_path):
        import respx
        from httpx import Response

        with respx.mock:
            respx.get(self.URL).mock(return_value=Response(200, json={"v": 1}))
            await self._get(tmp_path)
        with respx.mock:
            respx.get(self.URL).mock(return_value=Response(503))
            payload, info, note = await self._get(tmp_path, ttl=0)
        assert payload == {"v": 1}
        assert info.is_fallback
        assert note.startswith("STALE DATA")

    async def test_stale_fallback_can_be_refused_or_bounded(self, tmp_path):
        import os
        import time

        import respx
        from httpx import Response

        from fpl_agent.api.client import FPLAPIError

        with respx.mock:
            respx.get(self.URL).mock(return_value=Response(200, json={"v": 1}))
            await self._get(tmp_path)
        cache_file = next(tmp_path.glob("bootstrap-static*.json.gz"))
        two_hours_ago = time.time() - 7200
        os.utime(cache_file, (two_hours_ago, two_hours_ago))
        with respx.mock:
            respx.get(self.URL).mock(return_value=Response(503))
            with pytest.raises(FPLAPIError):
                await self._get(tmp_path, ttl=0, stale_ok=False)
            with pytest.raises(FPLAPIError):
                await self._get(tmp_path, ttl=0, max_stale_age=3600)
            _, info, _ = await self._get(tmp_path, ttl=0, max_stale_age=3 * 3600)
        assert info.is_fallback and info.age_seconds() > 7000
