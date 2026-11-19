"""Manual mode: the agent reads files you saved and never contacts FPL."""

from __future__ import annotations

import asyncio
import json
from pathlib import Path

import pytest

from fpl_agent.api.client import FPLAPIError, FPLClient, manual_filename, source_mode
from fpl_agent.cli import manual_paths


def _run(coro):
    return asyncio.run(coro)


def test_filename_matches_path():
    assert manual_filename("/bootstrap-static/") == "bootstrap-static.json"
    assert manual_filename("/entry/7/event/12/picks/") == "entry_7_event_12_picks.json"
    assert manual_filename("/fixtures/?event=3") == "fixtures__event-3.json"


def test_reads_saved_file_and_labels_it(tmp_path: Path):
    (tmp_path / "bootstrap-static.json").write_text(json.dumps({"events": []}))

    async def go():
        async with FPLClient(source="manual", manual_root=tmp_path, cache_dir=tmp_path / "c") as c:
            data = await c.get_json("/bootstrap-static/", ttl=0, stale_ok=False)
            return data, c.fetches["/bootstrap-static/"].origin, c.freshness_note()

    data, origin, note = _run(go())
    assert data == {"events": []}
    assert origin == "manual"
    assert note.startswith("MANUAL DATA")


def test_missing_file_names_url_and_file(tmp_path: Path):
    async def go():
        async with FPLClient(source="manual", manual_root=tmp_path, cache_dir=tmp_path / "c") as c:
            await c.get_json("/fixtures/")

    with pytest.raises(FPLAPIError) as err:
        _run(go())
    assert "fantasy.premierleague.com/api/fixtures/" in str(err.value)
    assert "fixtures.json" in str(err.value)


def test_never_falls_back_to_cache_or_network(tmp_path: Path):
    """A cached copy from api mode must not leak into manual mode."""

    async def seed():
        async with FPLClient(source="api", cache_dir=tmp_path / "c") as c:
            c._cache.write("/fixtures/", [{"id": 1}])

    _run(seed())

    async def go():
        async with FPLClient(source="manual", manual_root=tmp_path, cache_dir=tmp_path / "c") as c:
            await c.get_json("/fixtures/")

    with pytest.raises(FPLAPIError):
        _run(go())


def test_manual_mode_refuses_network(tmp_path: Path):
    async def go():
        async with FPLClient(source="manual", manual_root=tmp_path, cache_dir=tmp_path / "c") as c:
            await c._client.get("/bootstrap-static/")

    with pytest.raises(RuntimeError, match="refusing network"):
        _run(go())


def test_source_mode_env(monkeypatch):
    monkeypatch.delenv("FPL_SOURCE", raising=False)
    assert source_mode() == "api"
    monkeypatch.setenv("FPL_SOURCE", "Manual")
    assert source_mode() == "manual"
    monkeypatch.setenv("FPL_SOURCE", "scrape")
    with pytest.raises(ValueError):
        source_mode()


def test_manual_paths_cover_the_planner():
    paths = [p for p, _ in manual_paths(7, 12)]
    assert "/bootstrap-static/" in paths
    assert "/entry/7/event/12/picks/" in paths
    assert "/entry/7/transfers/" in paths
    assert [p for p, _ in manual_paths(None, None)] == ["/bootstrap-static/", "/fixtures/"]
