"""Scoring and comparison for backtest runs.

Total points alone hides the interesting failures: a strategy can beat a
baseline overall while bleeding points to hits, wasting returns on the bench, or
getting the captaincy wrong every week. Those are separable, so they are
reported separately.
"""

from __future__ import annotations

from dataclasses import dataclass

from rich.console import Console
from rich.table import Table

from fpl_agent.api.models import HIT_COST, Element
from fpl_agent.backtest.history import HistoryStore
from fpl_agent.backtest.replay import BacktestResult


@dataclass(frozen=True, slots=True)
class BacktestReport:
    strategy: str
    total_points: int
    points_per_gameweek: float
    gameweeks: int
    hits_taken: int
    points_lost_to_hits: int
    bench_points: int
    captain_hit_rate: float
    captain_points: int
    best_possible_captain_points: int
    auto_subs: int

    @property
    def captain_efficiency(self) -> float:
        """Share of the achievable captain haul actually captured.

        Measured against the best pick from the XI that was fielded, so 100%
        means perfect armband selection given the squad, not clairvoyance.
        """
        if not self.best_possible_captain_points:
            return 0.0
        return self.captain_points / self.best_possible_captain_points


def compare(
    result: BacktestResult,
    store: HistoryStore,
    elements: dict[int, Element],
) -> BacktestReport:
    """Turn a replay into a report, including counterfactual captaincy."""
    captain_hits = 0
    captain_points = 0
    best_captain_points = 0

    for gw in result.gameweeks:
        captain_points += gw.captain_points
        # The counterfactual that matters is the best armband available *from
        # the eleven you actually fielded* — not the highest scorer in the game,
        # which nobody could have captained.
        candidates = gw.starting_xi or tuple(gw.squad)
        best = max((store.actual_points(pid, gw.event) for pid in candidates), default=0)
        best_captain_points += best
        if best and gw.captain_points >= best:
            captain_hits += 1

    count = len(result.gameweeks) or 1
    return BacktestReport(
        strategy=result.strategy,
        total_points=result.total_points,
        points_per_gameweek=round(result.points_per_gameweek, 1),
        gameweeks=len(result.gameweeks),
        hits_taken=result.total_hits,
        points_lost_to_hits=result.total_hits * HIT_COST,
        bench_points=result.total_bench_points,
        captain_hit_rate=round(captain_hits / count, 3),
        captain_points=captain_points,
        best_possible_captain_points=best_captain_points,
        auto_subs=sum(len(gw.auto_subs) for gw in result.gameweeks),
    )


def render_report(
    reports: list[BacktestReport],
    *,
    average_scores: dict[int, int] | None = None,
    console: Console | None = None,
) -> None:
    """Print a side-by-side comparison, with the manager average if available."""
    console = console or Console()

    table = Table(title="Backtest", header_style="bold")
    table.add_column("Strategy")
    table.add_column("Pts", justify="right")
    table.add_column("Per GW", justify="right")
    table.add_column("Hits", justify="right")
    table.add_column("−Hits", justify="right")
    table.add_column("Bench", justify="right")
    table.add_column("Capt %", justify="right")
    table.add_column("Capt eff", justify="right")
    table.add_column("Subs", justify="right")

    best = max((r.total_points for r in reports), default=0)
    for report in reports:
        style = "bold green" if report.total_points == best and len(reports) > 1 else ""
        table.add_row(
            report.strategy,
            str(report.total_points),
            f"{report.points_per_gameweek:.1f}",
            str(report.hits_taken),
            str(report.points_lost_to_hits),
            str(report.bench_points),
            f"{report.captain_hit_rate:.0%}",
            f"{report.captain_efficiency:.0%}",
            str(report.auto_subs),
            style=style,
        )

    console.print(table)

    if average_scores:
        total = sum(average_scores.values())
        console.print(
            f"[dim]Manager average over the same {len(average_scores)} gameweeks: "
            f"{total} pts ({total / len(average_scores):.1f}/GW). "
            "Beating this is the bar that matters.[/dim]"
        )

    if len(reports) > 1:
        ranked = sorted(reports, key=lambda r: r.total_points, reverse=True)
        margin = ranked[0].total_points - ranked[1].total_points
        per_gw = margin / (ranked[0].gameweeks or 1)
        console.print(
            f"[bold]{ranked[0].strategy}[/bold] beats [bold]{ranked[1].strategy}[/bold] "
            f"by {margin} pts ({per_gw:+.1f}/GW)."
        )
        if abs(per_gw) < 1.0:
            console.print(
                "[yellow]A margin under 1 point per gameweek is inside the noise "
                "for a sample this size — do not read it as an edge.[/yellow]"
            )
