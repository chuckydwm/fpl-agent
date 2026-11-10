"""Dated predictions, scored against settled outcomes.

Nothing in this project may claim the model got better without numbers that
show it. Every deadline run appends the coming gameweek's projections, with
the time they were made, to ``predictions.jsonl`` in the state directory.
Once the gameweek is settled, :func:`evaluate` compares the last capture made
before the deadline with what happened:

* points: mean absolute error and mean error (bias), over players who could
  play;
* minutes: mean absolute error of expected minutes;
* start and 60-minute probabilities: Brier scores (lower is better; 0.25 is
  what always saying 50% scores) and a calibration table: in each band of
  predicted probability, how often it actually happened.

Only captures made before the deadline count, so a prediction can never be
scored on information it did not have.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any

from fpl_agent.models.expected_points import ExpectedPoints

CALIBRATION_BANDS = (0.0, 0.2, 0.4, 0.6, 0.8, 1.0001)


def capture_rows(
    projections: dict[tuple[int, int], ExpectedPoints], event: int, captured_at: datetime
) -> list[dict[str, Any]]:
    """One row per player for ``event``. Pure."""
    stamp = captured_at.isoformat()
    return [
        {
            "event": event,
            "element": p.element_id,
            "captured_at": stamp,
            "xp": p.xp,
            "p_appear": p.p_appear,
            "p_start": p.p_start,
            "p_60": p.p_60,
            "expected_minutes": p.expected_minutes,
            "fixtures": p.fixtures,
        }
        for (_, e), p in sorted(projections.items())
        if e == event
    ]


def append(path: Path, rows: list[dict[str, Any]]) -> None:
    """shortcut: append-only JSONL, about 100 KB a gameweek; prune if it passes
    a few seasons."""
    if not rows:
        return
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as handle:
        for row in rows:
            handle.write(json.dumps(row) + "\n")


def latest_before(path: Path, event: int, deadline: datetime) -> dict[int, dict[str, Any]]:
    """The last capture of each player for ``event`` made before ``deadline``."""
    if not path.exists():
        return {}
    chosen: dict[int, dict[str, Any]] = {}
    for line in path.read_text(encoding="utf-8").splitlines():
        try:
            row = json.loads(line)
            if int(row["event"]) != event:
                continue
            when = datetime.fromisoformat(row["captured_at"])
        except (ValueError, KeyError, TypeError):
            continue
        if when >= deadline:
            continue
        current = chosen.get(int(row["element"]))
        if current is None or when >= datetime.fromisoformat(current["captured_at"]):
            chosen[int(row["element"])] = row
    return chosen


@dataclass(frozen=True, slots=True)
class Evaluation:
    event: int
    players: int
    captured_at: str
    points_mae: float
    points_bias: float
    minutes_mae: float
    start_brier: float
    sixty_brier: float
    #: ``(band low, band high, predicted mean, observed rate, players)`` for p_start.
    calibration: tuple[tuple[float, float, float, float, int], ...]

    def describe(self) -> list[str]:
        lines = [
            f"GW{self.event}: {self.players} players, predictions captured {self.captured_at[:16]}",
            f"  points: mean absolute error {self.points_mae:.2f}, bias {self.points_bias:+.2f}"
            " (positive = predicted too high)",
            f"  minutes: mean absolute error {self.minutes_mae:.1f}",
            f"  start: Brier {self.start_brier:.3f} · 60 minutes: Brier {self.sixty_brier:.3f}"
            " (0.25 = always guessing 50%)",
            "  start calibration (predicted -> happened):",
        ]
        for low, high, predicted, observed, n in self.calibration:
            lines.append(
                f"    {low:.1f}-{min(high, 1.0):.1f}: {predicted:.0%} -> {observed:.0%} ({n})"
            )
        return lines


def evaluate(
    event: int,
    captured: dict[int, dict[str, Any]],
    outcomes: dict[int, dict[str, Any]],
) -> Evaluation | None:
    """Score captures against live stats (``/event/{id}/live/`` elements'
    ``stats``: total_points, minutes, starts). Pure. ``None`` if nothing to score.

    Players predicted to have no chance of appearing are left out: they are
    not predictions about a match."""
    rows = [
        (captured[pid], outcomes[pid])
        for pid in sorted(captured)
        if pid in outcomes and float(captured[pid]["p_appear"]) > 0
    ]
    if not rows:
        return None
    n = len(rows)
    errors = [float(c["xp"]) - float(o.get("total_points", 0)) for c, o in rows]
    minutes = [abs(float(c["expected_minutes"]) - float(o.get("minutes", 0))) for c, o in rows]
    started = [1.0 if int(o.get("starts", 0)) > 0 else 0.0 for _, o in rows]
    sixty = [1.0 if int(o.get("minutes", 0)) >= 60 else 0.0 for _, o in rows]
    p_start = [float(c["p_start"]) for c, _ in rows]
    p_60 = [float(c["p_60"]) for c, _ in rows]

    bands: list[tuple[float, float, float, float, int]] = []
    for low, high in zip(CALIBRATION_BANDS, CALIBRATION_BANDS[1:], strict=False):
        members = [i for i, p in enumerate(p_start) if low <= p < high]
        if members:
            bands.append(
                (
                    low,
                    high,
                    sum(p_start[i] for i in members) / len(members),
                    sum(started[i] for i in members) / len(members),
                    len(members),
                )
            )
    return Evaluation(
        event=event,
        players=n,
        captured_at=max(str(c["captured_at"]) for c, _ in rows),
        points_mae=sum(abs(e) for e in errors) / n,
        points_bias=sum(errors) / n,
        minutes_mae=sum(minutes) / n,
        start_brier=sum((p - y) ** 2 for p, y in zip(p_start, started, strict=True)) / n,
        sixty_brier=sum((p - y) ** 2 for p, y in zip(p_60, sixty, strict=True)) / n,
        calibration=tuple(bands),
    )
