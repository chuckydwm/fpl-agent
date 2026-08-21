"""Run the backtest engine against a synthetic season.

The live season has no settled gameweeks yet, so this exists to demonstrate the
replay end to end and to sanity-check that the optimiser can actually find
signal when signal exists.

Players are given a hidden ``quality``. Their weekly returns are that quality
plus noise, so past performance is genuinely predictive — which is the
condition under which a projection model *should* beat the crowd. If the
optimiser cannot win here, it will certainly not win on real data.

    python examples/demo_backtest.py
"""

from __future__ import annotations

import random

from rich.console import Console

from fpl_agent.api.models import Element, Fixture, Team
from fpl_agent.backtest.history import HistoryStore, PlayerGameweek
from fpl_agent.backtest.metrics import compare, render_report
from fpl_agent.backtest.replay import replay
from fpl_agent.backtest.strategies import HoldStrategy, OptimiserStrategy, TemplateStrategy

CLUBS = 20
GAMEWEEKS = 12
PER_CLUB = {1: 2, 2: 5, 3: 5, 4: 3}
SEED = 20262027


def build_universe() -> tuple[list[Element], dict[int, Team], list[Fixture], HistoryStore]:
    rng = random.Random(SEED)

    teams = {
        i: Team(
            id=i,
            name=f"Club {i}",
            short_name=f"C{i:02d}",
            strength_attack_home=900 + rng.randint(0, 400),
            strength_attack_away=880 + rng.randint(0, 400),
            strength_defence_home=900 + rng.randint(0, 400),
            strength_defence_away=880 + rng.randint(0, 400),
        )
        for i in range(1, CLUBS + 1)
    }

    elements: list[Element] = []
    quality: dict[int, float] = {}
    element_id = 1
    for team in range(1, CLUBS + 1):
        for element_type, count in PER_CLUB.items():
            for _ in range(count):
                q = rng.betavariate(2, 5)  # most players ordinary, a few excellent
                # Price correlates with quality, as it does in the real game.
                price = 40 + int(q * 110) + rng.randint(-4, 4)
                # Ownership is a NOISY, lagging read on quality. Making it a
                # clean function of q would turn the template strategy into an
                # oracle and the comparison into nonsense.
                perceived = max(0.0, min(1.0, q + rng.gauss(0, 0.18)))
                elements.append(
                    Element(
                        id=element_id,
                        web_name=f"P{element_id:03d}",
                        team=team,
                        element_type=element_type,
                        now_cost=max(38, price),
                        selected_by_percent=f"{perceived * 60:.1f}",
                    )
                )
                quality[element_id] = q
                element_id += 1

    # A simple round-robin-ish fixture list: every club plays every gameweek.
    fixtures: list[Fixture] = []
    fixture_id = 1
    order = list(range(1, CLUBS + 1))
    for event in range(1, GAMEWEEKS + 1):
        rng.shuffle(order)
        for i in range(0, CLUBS, 2):
            home, away = order[i], order[i + 1]
            fixtures.append(
                Fixture(
                    id=fixture_id,
                    event=event,
                    team_h=home,
                    team_a=away,
                    team_h_difficulty=rng.randint(2, 4),
                    team_a_difficulty=rng.randint(2, 4),
                )
            )
            fixture_id += 1

    # Weekly returns: quality-driven signal plus heavy noise, and real rotation.
    rows: list[PlayerGameweek] = []
    for element in elements:
        q = quality[element.id]
        for event in range(1, GAMEWEEKS + 1):
            plays = rng.random() < (0.45 + 0.5 * q)
            minutes = rng.choice([90, 90, 78, 62]) if plays else 0
            points = max(0, int(rng.gauss(mu=1.5 + 7.0 * q, sigma=2.6))) if plays else 0
            rows.append(
                PlayerGameweek(
                    element=element.id,
                    round=event,
                    total_points=points,
                    minutes=minutes,
                    goals_scored=1 if points >= 8 else 0,
                    assists=1 if 5 <= points < 8 else 0,
                    clean_sheets=1 if points in (5, 6) else 0,
                    bonus=1 if points >= 9 else 0,
                    value=element.now_cost,
                )
            )

    store = HistoryStore(rows=rows, template={e.id: e for e in elements})
    return elements, teams, fixtures, store


def main() -> None:
    console = Console()
    elements, teams, fixtures, store = build_universe()
    events = list(range(1, GAMEWEEKS + 1))

    console.print(
        f"[bold]Synthetic season[/bold] — {len(elements)} players, {CLUBS} clubs, "
        f"{GAMEWEEKS} gameweeks, seed {SEED}\n"
    )

    reports = []
    for strategy in (
        OptimiserStrategy(horizon=1, max_transfers=2),
        HoldStrategy(),
        TemplateStrategy(),
    ):
        result = replay(store, strategy, events=events, fixtures=fixtures, teams=teams)
        reports.append(compare(result, store, {e.id: e for e in elements}))

    render_report(reports, console=console)


if __name__ == "__main__":
    main()
