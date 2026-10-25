"""Disk-cache behaviour that the launchd jobs depend on."""

from __future__ import annotations

import gzip
import logging
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

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
